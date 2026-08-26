"""Skill-registry drift detection in scripts/operations-health.py.

``skills/.usage.json`` is telemetry, and ``bump_use`` deliberately records any
name handed to it — its docstring calls usage tracking "pure observability ...
orthogonal to whether a skill is ever curated". The cost is that a name nothing
can resolve still accumulates a record.

That happened. ``vault-governance`` held 51 recorded uses with no SKILL.md
anywhere, and its record was ``curator_managed: True``, so the one entry the
curator would eventually act on was the one that did not exist. In the other
direction, 18 skills installed in one batch on 2026-08-10 had no record at all.
Both were invisible until someone compared the two by hand.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "operations-health.py"


@pytest.fixture
def mod():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location("operations_health_reg_test", SCRIPT_PATH)
        assert spec is not None and spec.loader is not None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.pop(0)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "skills").mkdir()
    return tmp_path


def _skill(home: Path, name: str, category: str = "note-taking"):
    d = home / "skills" / category / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: %s\n---\n" % name, encoding="utf-8")


def _usage(home: Path, names):
    (home / "skills" / ".usage.json").write_text(
        json.dumps({n: {"use_count": 1, "state": "active"} for n in names}), encoding="utf-8"
    )


def test_silent_when_registry_matches_disk(mod, home):
    _skill(home, "alpha"); _skill(home, "beta")
    _usage(home, ["alpha", "beta"])

    assert mod.skill_registry_alerts(home) == []


def test_reports_a_record_with_no_skill(mod, home):
    """vault-governance: 51 recorded uses, no SKILL.md anywhere."""
    _skill(home, "alpha")
    _usage(home, ["alpha", "vault-governance"])

    alerts = mod.skill_registry_alerts(home)

    assert len(alerts) == 1
    assert "vault-governance" in alerts[0]
    assert "no SKILL.md on disk" in alerts[0]
    assert "forget()" in alerts[0], "the alert must say how to repair it"


def test_reports_a_skill_with_no_record(mod, home):
    """The 2026-08-10 batch: on disk, invisible to the curator."""
    _skill(home, "alpha"); _skill(home, "client-agreements", "productivity")
    _usage(home, ["alpha"])

    alerts = mod.skill_registry_alerts(home)

    assert len(alerts) == 1
    assert "client-agreements" in alerts[0]
    assert "no usage record" in alerts[0]
    assert "seed_record_if_missing()" in alerts[0]


def test_reports_both_directions_separately(mod, home):
    """Drift ran both ways at once, which is what made every count wrong."""
    _skill(home, "alpha"); _skill(home, "untracked")
    _usage(home, ["alpha", "ghost"])

    alerts = mod.skill_registry_alerts(home)

    assert len(alerts) == 2
    assert any("ghost" in a and "no SKILL.md" in a for a in alerts)
    assert any("untracked" in a and "no usage record" in a for a in alerts)


def test_missing_or_unreadable_registry_is_reported_not_swallowed(mod, home, tmp_path):
    _skill(home, "alpha")
    alerts = mod.skill_registry_alerts(home)
    assert len(alerts) == 1 and "cannot be checked" in alerts[0]

    (home / "skills" / ".usage.json").write_text("{not json", encoding="utf-8")
    alerts = mod.skill_registry_alerts(home)
    assert len(alerts) == 1 and "cannot be read" in alerts[0]

    empty = tmp_path / "nohome"
    empty.mkdir()
    alerts = mod.skill_registry_alerts(empty)
    assert len(alerts) == 1 and "cannot be checked" in alerts[0]


def test_the_check_never_modifies_anything(mod, home):
    """Repair depends on why the drift appeared, so this reports only."""
    _skill(home, "alpha")
    _usage(home, ["alpha", "ghost"])
    before = (home / "skills" / ".usage.json").read_text(encoding="utf-8")

    mod.skill_registry_alerts(home)

    assert (home / "skills" / ".usage.json").read_text(encoding="utf-8") == before
    assert (home / "skills" / "note-taking" / "alpha" / "SKILL.md").exists()


def test_drift_reaches_inspect_health(mod, home, tmp_path):
    from datetime import datetime, timezone
    _skill(home, "alpha")
    _usage(home, ["alpha", "ghost"])
    (home / "hermes-agent" / "scripts").mkdir(parents=True)

    alerts, _ = mod.inspect_health(
        hermes_home=home,
        primary_backup_dir=tmp_path / "n1",
        secondary_backup_dir=tmp_path / "n2",
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    assert any("ghost" in a for a in alerts)
