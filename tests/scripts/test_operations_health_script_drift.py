"""Script-drift detection in scripts/operations-health.py.

``<HERMES_HOME>/scripts/`` is what cron actually executes and it is untracked;
the tracked copies live in the checkout at ``hermes-agent/scripts/``, and
nothing syncs the two. Both directions of drift happened for real on 2026-08-26:
live ran ahead of the repo for weeks with fixes that existed nowhere else, and
within an hour of that being corrected the repo ran ahead of live with merged
fixes that had not been deployed.

These pin the reporting behaviour, including the two things it must NOT do:
report repo-only scripts (most of the checkout's scripts are dev/CI tooling that
should never reach the VM), and modify anything.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "operations-health.py"
)


@pytest.fixture
def mod():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location("operations_health_test", SCRIPT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "hermes-agent" / "scripts").mkdir(parents=True)
    return tmp_path


def _put(home: Path, where: str, name: str, body: str):
    d = home / "scripts" if where == "live" else home / "hermes-agent" / "scripts"
    (d / name).write_text(body, encoding="utf-8")


def test_silent_when_the_two_copies_agree(mod, home):
    for side in ("live", "repo"):
        _put(home, side, "a.py", "print(1)\n")
        _put(home, side, "b.sh", "echo hi\n")

    assert mod.script_drift_alerts(home) == []


def test_reports_a_file_that_differs(mod, home):
    _put(home, "live", "a.py", "print(1)  # fixed live, never committed\n")
    _put(home, "repo", "a.py", "print(1)\n")

    alerts = mod.script_drift_alerts(home)

    assert len(alerts) == 1
    assert "a.py" in alerts[0] and "differ" in alerts[0]


def test_many_differing_files_are_one_alert_not_many(mod, home):
    """Run against the real system this reported eight separate "differs"
    lines whose single cause was a checkout eight commits behind. Accurate,
    but it read like eight problems."""
    for name in ("a.py", "b.py", "c.sh"):
        _put(home, "live", name, "live\n")
        _put(home, "repo", name, "repo\n")

    alerts = mod.script_drift_alerts(home)

    assert len(alerts) == 1
    assert "3 script(s) differ" in alerts[0]
    for name in ("a.py", "b.py", "c.sh"):
        assert name in alerts[0]
    assert "hermes-sync-fork" in alerts[0], "the lagging-checkout cause must be named"


def test_reports_a_live_only_script(mod, home):
    """hermes-config-auto-commit.sh existed on the VM and in no repository at
    all until it was found by a manual sweep."""
    _put(home, "live", "orphan.sh", "echo orphan\n")

    alerts = mod.script_drift_alerts(home)

    assert len(alerts) == 1
    assert "orphan.sh" in alerts[0] and "only" in alerts[0]


def test_repo_only_scripts_are_not_reported(mod, home):
    """32 of the checkout's scripts are dev/CI tooling that must never be
    deployed. Reporting them would make this check permanently noisy."""
    _put(home, "repo", "check-windows-footguns.py", "print(1)\n")
    _put(home, "repo", "build_skills_index.py", "print(1)\n")

    assert mod.script_drift_alerts(home) == []


def test_ignores_non_script_files(mod, home):
    _put(home, "live", "notes.md", "hello\n")
    _put(home, "repo", "notes.md", "different\n")

    assert mod.script_drift_alerts(home) == []


def test_missing_directories_are_reported_not_swallowed(mod, tmp_path):
    (tmp_path / "scripts").mkdir()
    alerts = mod.script_drift_alerts(tmp_path)
    assert len(alerts) == 1 and "cannot be checked" in alerts[0]

    empty = tmp_path / "other"
    (empty / "hermes-agent" / "scripts").mkdir(parents=True)
    alerts = mod.script_drift_alerts(empty)
    assert len(alerts) == 1 and "cannot be checked" in alerts[0]


def test_the_check_never_modifies_anything(mod, home):
    """It reports; it does not sync. An automatic repo -> live copy is exactly
    what would have destroyed the live-only work."""
    _put(home, "live", "a.py", "live version\n")
    _put(home, "repo", "a.py", "repo version\n")

    mod.script_drift_alerts(home)

    assert (home / "scripts" / "a.py").read_text(encoding="utf-8") == "live version\n"
    assert (home / "hermes-agent" / "scripts" / "a.py").read_text(encoding="utf-8") == "repo version\n"


def test_drift_reaches_inspect_health(mod, home, tmp_path):
    """The alert is only useful if the job actually surfaces it."""
    _put(home, "live", "a.py", "live\n")
    _put(home, "repo", "a.py", "repo\n")
    from datetime import datetime, timezone

    alerts, _ = mod.inspect_health(
        hermes_home=home,
        primary_backup_dir=tmp_path / "nope1",
        secondary_backup_dir=tmp_path / "nope2",
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    assert any("a.py" in a and "differ" in a for a in alerts)
