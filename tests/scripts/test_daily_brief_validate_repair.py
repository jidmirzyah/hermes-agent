"""Repair path for scripts/daily-brief-validate.py.

The daily brief is composed by an agent that is *told* to save it with
``write_file``. On 4 of the 14 runs that completed between 2026-08-11 and
2026-08-26 it did not, in three different shapes, and the file was simply
missing while Telegram still received the brief. The validator now rebuilds the
saved file from the response cron already delivered instead of re-running the
model, which is a coin flip on the same odds.

Every guard here is a failure that actually happened. The fixtures are built
from the real shapes, not invented ones:

* 2026-08-13 -- the delivered response was an unrelated ad-hoc verification
  report while the vault held a genuine 3,064-char brief.
* 2026-08-17 -- a complete brief followed by a fabricated refusal naming a
  path that does not exist.
* 2026-08-18 -- the entire brief twice, split by ``Final response:``, then
  ``[[DONE]]``.
* 2026-08-11 -- curly apostrophes throughout; every run since uses straight
  ones. Matching only U+0027 would silently refuse to repair that shape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "daily-brief-validate.py"

JOB_ID = "af002841b069"
NOW = datetime(2026, 8, 26, 4, 15, 0, tzinfo=timezone(timedelta(hours=-4)))
LAST_RUN = datetime(2026, 8, 26, 4, 1, 2, tzinfo=timezone(timedelta(hours=-4)))


@pytest.fixture
def mod():
    """Load the script by path; it imports a sibling in scripts/."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "daily_brief_validate_test", SCRIPT_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def _brief(apostrophe: str = "'") -> str:
    return (
        f"Good morning — here{apostrophe}s where things stand.\n\n"
        "\U0001F4C5 Today\nNothing on the calendar today.\n\n"
        "\U0001F4E7 Email\nNothing new that needs attention.\n\n"
        "\U0001F5C2️ Vault\n13 recent changes.\n\n"
        "\U0001F4E5 Mailbox\n11 items waiting.\n\n"
        "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466 Family\nNothing new.\n\n"
        "\U0001F527 System\nBackup is 1 hour old.\n\n"
        f"That{apostrophe}s everything since August 25, 2026 at 4:01 AM EDT."
    )


def _audit(response: str) -> str:
    return f"# Cron Job: hermes-daily-brief\n\n## Prompt\n\nx\n\n## Response\n\n{response}\n"


@pytest.fixture
def home(tmp_path):
    """A HERMES_HOME with the job registered and an output directory."""
    h = tmp_path / ".hermes"
    (h / "cron" / "output" / JOB_ID).mkdir(parents=True)
    (h / "cron" / "jobs.json").write_text(
        json.dumps(
            {"jobs": [{"id": JOB_ID, "name": "hermes-daily-brief",
                       "last_run_at": LAST_RUN.isoformat()}]}
        ),
        encoding="utf-8",
    )
    return h


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "Obsidian Core"
    (v / "Hermes" / "Briefs").mkdir(parents=True)
    return v


def _write_output(home: Path, response: str, name: str = "2026-08-26_04-01-01.md"):
    path = home / "cron" / "output" / JOB_ID / name
    path.write_text(_audit(response), encoding="utf-8")
    return path


def _saved(vault: Path) -> Path:
    return vault / "Hermes" / "Briefs" / "2026-08-26.md"


# --- extract_brief -----------------------------------------------------


def test_extracts_a_clean_brief(mod):
    assert mod.extract_brief(_audit(_brief())) == _brief()


def test_accepts_curly_apostrophes(mod):
    """2026-08-11's shape. Matching only U+0027 would reject it silently."""
    curly = _brief("’")
    assert mod.extract_brief(_audit(curly)) == curly


def test_truncates_a_fabricated_trailing_refusal(mod):
    """2026-08-17: the model appended a refusal about a file that did not exist."""
    noisy = _brief() + (
        "\n\n[Failure: Cannot overwrite existing file "
        "`HOME/obsidian_core/hermes/Briefs/2026-08-17.md` as it exists but "
        "differs from current report.]"
    )
    assert mod.extract_brief(_audit(noisy)) == _brief()


def test_truncates_a_duplicated_body(mod):
    """2026-08-18: the whole brief twice, then a stray token."""
    doubled = _brief() + "\nFinal response:\n\n" + _brief() + "\n`[[DONE]]`"
    assert mod.extract_brief(_audit(doubled)) == _brief()


def test_rejects_a_response_that_is_not_a_brief(mod):
    """2026-08-13: an ad-hoc verification report delivered in the brief's slot."""
    assert mod.extract_brief(_audit("Ad-hoc verification is now complete.")) == ""


def test_rejects_silent_empty_and_unclosed(mod):
    assert mod.extract_brief(_audit("[SILENT]")) == ""
    assert mod.extract_brief(_audit("")) == ""
    assert mod.extract_brief("no response marker at all") == ""
    unclosed = _brief().rsplit("\n", 1)[0]
    assert mod.extract_brief(_audit(unclosed)) == ""


def test_rejects_too_few_sections(mod):
    thin = (
        "Good morning — here's where things stand.\n\n"
        "\U0001F4C5 Today\nNothing.\n\n"
        "That's everything since August 25, 2026 at 4:01 AM EDT."
    )
    assert mod.extract_brief(_audit(thin)) == ""


# --- attempt_repair ----------------------------------------------------


def test_rebuilds_the_missing_brief(mod, home, vault):
    _write_output(home, _brief())

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert "repaired" in result
    assert _saved(vault).read_text(encoding="utf-8") == _brief()


def test_never_overwrites_a_non_empty_saved_brief(mod, home, vault):
    """2026-08-13's lesson: a rebuild that trusted the response would have
    destroyed a real brief."""
    _saved(vault).write_text("a real brief already here", encoding="utf-8")
    _write_output(home, "Ad-hoc verification is now complete.")

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert "skipped" in result
    assert _saved(vault).read_text(encoding="utf-8") == "a real brief already here"


def test_falls_back_to_retry_when_the_response_is_unusable(mod, home, vault):
    """A rebuild needs a composed brief. 2026-08-12 and 2026-08-15 both 429'd,
    leaving nothing to rebuild from, so re-running is the only repair."""
    _write_output(home, "[SILENT]")
    mod._record_self_heal_triggered(
        home / "cron/notepad.db", JOB_ID, NOW.date().isoformat(), NOW
    )

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert "not a usable brief" in result
    assert not _saved(vault).exists()


def test_falls_back_to_retry_when_there_is_no_output(mod, home, vault):
    mod._record_self_heal_triggered(
        home / "cron/notepad.db", JOB_ID, NOW.date().isoformat(), NOW
    )

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert "no cron output" in result
    assert not _saved(vault).exists()


def test_stale_output_is_not_written_as_todays_brief(mod, home, vault):
    """An output file from before today's run window must not be recycled."""
    import os

    path = _write_output(home, _brief())
    stale = (LAST_RUN - timedelta(days=3)).timestamp()
    os.utime(path, (stale, stale))
    mod._record_self_heal_triggered(
        home / "cron/notepad.db", JOB_ID, NOW.date().isoformat(), NOW
    )

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert "predates" in result
    assert not _saved(vault).exists()


def test_repair_never_raises(mod, tmp_path, vault):
    """A repair failure must not mask the alert that triggered it."""
    result = mod.attempt_repair(
        hermes_home=tmp_path / "nonexistent", vault_root=vault, now=NOW
    )
    assert isinstance(result, str) and result.startswith("(")
    assert not _saved(vault).exists()


def test_retry_calls_trigger_job(mod, home, vault, monkeypatch):
    """The fallback must actually reach cron.jobs.trigger_job."""
    import types

    called = {}
    fake = types.ModuleType("cron.jobs")
    fake.trigger_job = lambda job_id: called.setdefault("id", job_id) or {"id": job_id}
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", fake)
    _write_output(home, "[SILENT]")

    result = mod.attempt_repair(hermes_home=home, vault_root=vault, now=NOW)

    assert called["id"] == JOB_ID
    assert "re-triggered" in result
