"""Deploy-drift / pin-staleness detection in scripts/operations-health.py.

MOORING Step 1.4 (L3 drift detection). Replaces the old behind-`main` commit
count, which is meaningless once the live checkout tracks a pinned `deploy`
branch instead of continuously reconciled upstream history. Covers: live HEAD
vs origin/deploy, days since deploy-advance.sh's last recorded advance
(reading its receipt), the newest upstream stable-pattern tag and whether
origin/deploy already has it, and the security-keyword tag-message check.

Builds real fixture git repos (no mocking of git itself), matching the
approach used for ops/deploy/deploy-advance.sh's own tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "operations-health.py"


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


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )


@pytest.fixture
def home(tmp_path):
    """hermes_home with hermes-agent/ as a real git repo, cloned from an
    origin that carries a `deploy` branch -- mirrors deploy-advance.sh's own
    fixture topology."""
    origin = tmp_path / "origin_repo"
    repo_dir = tmp_path / "hermes-agent"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    (origin / "README.md").write_text("root\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "root")
    _git(origin, "checkout", "--quiet", "-b", "deploy")

    subprocess.run(["git", "clone", "--quiet", str(origin), str(repo_dir)], check=True, capture_output=True, text=True)
    (tmp_path / "cron").mkdir()
    return tmp_path


def test_missing_checkout_reports_one_alert(mod, tmp_path):
    alerts, facts = mod.deploy_drift_alerts(tmp_path, datetime.now(timezone.utc))
    assert len(alerts) == 1 and "is missing" in alerts[0]
    assert facts == {}


def test_silent_when_live_head_matches_origin_deploy(mod, home):
    alerts, facts = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
    assert alerts == []
    assert facts["live_head"] == facts["origin_deploy_head"]


def test_reports_commits_behind_origin_deploy(mod, home):
    origin = home / "origin_repo"
    _git(origin, "checkout", "--quiet", "deploy")
    (origin / "new.txt").write_text("x\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "advance")
    _git(home / "hermes-agent", "fetch", "origin", "--quiet")

    alerts, facts = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
    assert any("behind origin/deploy" in a for a in alerts)
    assert facts["commits_behind_deploy"] == 1


def test_no_origin_deploy_branch_is_not_an_alert(mod, tmp_path):
    origin = tmp_path / "origin_repo"
    repo_dir = tmp_path / "hermes-agent"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    (origin / "f.txt").write_text("x\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "c")
    subprocess.run(["git", "clone", "--quiet", str(origin), str(repo_dir)], check=True, capture_output=True, text=True)

    alerts, facts = mod.deploy_drift_alerts(tmp_path, datetime.now(timezone.utc))
    assert alerts == []
    assert facts["origin_deploy_head"] is None


class TestReceipt:
    def _write_receipt(self, home: Path, **fields):
        (home / "cron").mkdir(exist_ok=True)
        (home / "cron" / "deploy_advance_state.json").write_text(json.dumps(fields))

    def test_no_receipt_is_not_an_alert(self, mod, home):
        alerts, facts = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert alerts == []
        assert facts["days_since_last_advance"] is None

    def test_recent_healthy_advance_is_silent(self, mod, home):
        now = datetime.now(timezone.utc)
        self._write_receipt(home, state="healthy", scheduled_at=(now - timedelta(days=1)).isoformat().replace("+00:00", "Z"))
        alerts, facts = mod.deploy_drift_alerts(home, now)
        assert alerts == []
        assert facts["days_since_last_advance"] == pytest.approx(1.0, abs=0.1)

    def test_stale_advance_past_urgent_days_alerts(self, mod, home):
        now = datetime.now(timezone.utc)
        self._write_receipt(home, state="healthy", scheduled_at=(now - timedelta(days=10)).isoformat().replace("+00:00", "Z"))
        alerts, facts = mod.deploy_drift_alerts(home, now, urgent_days=7)
        assert any(a.startswith("URGENT:") and "days since the last deploy-advance" in a for a in alerts)

    def test_failed_advance_always_alerts_regardless_of_age(self, mod, home):
        now = datetime.now(timezone.utc)
        self._write_receipt(home, state="failed", reason="gateway restart failed",
                             scheduled_at=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        alerts, _ = mod.deploy_drift_alerts(home, now)
        assert any("Last deploy-advance run failed" in a and "gateway restart failed" in a for a in alerts)

    def test_corrupt_receipt_reports_alert_not_crash(self, mod, home):
        (home / "cron" / "deploy_advance_state.json").write_text("not json")
        alerts, _ = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert any("receipt could not be read" in a for a in alerts)


class TestUpstreamTag:
    def test_newest_tag_and_origin_deploy_already_includes_it_is_silent(self, mod, home):
        repo_dir = home / "hermes-agent"
        _git(repo_dir, "tag", "v1.0.0")
        alerts, facts = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert facts["newest_upstream_tag"] == "v1.0.0"
        assert not any("behind the newest upstream tag" in a for a in alerts)

    def test_origin_deploy_behind_newest_tag_alerts(self, mod, home):
        # The tag must land on a commit that is NOT also merged into
        # `deploy` -- tagging deploy's own tip would make deploy trivially
        # "at" the tag (a commit is its own ancestor), not behind it.
        origin = home / "origin_repo"
        repo_dir = home / "hermes-agent"
        _git(origin, "checkout", "--quiet", "-b", "release", "deploy")
        (origin / "new.txt").write_text("x\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "--quiet", "-m", "release commit")
        _git(origin, "tag", "v2.0.0")
        _git(repo_dir, "fetch", "origin", "--quiet", "--tags")

        alerts, facts = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert facts["newest_upstream_tag"] == "v2.0.0"
        assert any("behind the newest upstream tag v2.0.0" in a for a in alerts)

    def test_security_keyword_in_tag_message_is_urgent(self, mod, home):
        repo_dir = home / "hermes-agent"
        _git(repo_dir, "tag", "-a", "v1.0.1", "-m", "Fixes CVE-2026-12345 in the auth flow")
        alerts, _ = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert any(a.startswith("URGENT:") and "security-relevant" in a for a in alerts)

    def test_ordinary_tag_message_is_not_flagged_urgent(self, mod, home):
        repo_dir = home / "hermes-agent"
        _git(repo_dir, "tag", "-a", "v1.0.1", "-m", "Routine feature release")
        alerts, _ = mod.deploy_drift_alerts(home, datetime.now(timezone.utc))
        assert not any("security-relevant" in a for a in alerts)


def test_wired_into_inspect_health(mod, home):
    """The new check's alerts and facts surface through inspect_health(),
    not just when called directly."""
    alerts, facts = mod.inspect_health(
        hermes_home=home,
        primary_backup_dir=home / "backup1",
        secondary_backup_dir=home / "backup2",
        now=datetime.now(timezone.utc),
    )
    assert "live_head" in facts
    assert "origin_deploy_head" in facts
