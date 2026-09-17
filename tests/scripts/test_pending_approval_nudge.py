"""Tests for scripts/pending-approval-nudge.py, including the UPREVIEWFIX
stranded-upstream-review check added 2026-09-17."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "pending-approval-nudge.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("pending_approval_nudge", SCRIPT_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["pending_approval_nudge"] = mod
spec.loader.exec_module(mod)

JOB_ID = "d157d8f45391"
JOB_NAME = "hermes-upstream-main-check"


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes_home"
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": JOB_ID, "name": JOB_NAME}]})
    )
    return home


def _executions_db(hermes_home: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (id, status, claimed_at)"""
    db = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, claimed_at TEXT)"
    )
    for exec_id, status, claimed_at in rows:
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?)", (exec_id, JOB_ID, status, claimed_at)
        )
    conn.commit()
    conn.close()


def _write_pending(pending_dir: Path, subsystem: str, item_id: str, age_hours: float = 0.0):
    directory = pending_dir / subsystem
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{item_id}.json"
    path.write_text("{}")
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
    return path


def _set_mtime_minutes_ago(path: Path, minutes: float) -> None:
    ts = time.time() - minutes * 60
    os.utime(path, (ts, ts))


class TestOrdinaryStaleness:
    def test_silent_when_nothing_stale(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        _write_pending(pending_dir, "memory", "fresh", age_hours=1)
        home = _hermes_home(tmp_path)
        now = mod.local_now()
        assert mod._stale_items(pending_dir, now) == []

    def test_flags_after_three_days(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        _write_pending(pending_dir, "skills", "old", age_hours=80)
        now = mod.local_now()
        result = mod._stale_items(pending_dir, now)
        assert len(result) == 1
        assert result[0][:2] == ("skills", "old")


class TestStrandedUpstreamReview:
    def test_flags_fresh_record_matching_unknown_execution(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        path = _write_pending(pending_dir, "upstream_fix", "177e3d8d")
        _set_mtime_minutes_ago(path, 2)  # record written 2 minutes ago
        claimed = mod.local_now() - timedelta(minutes=9)  # claimed 9m ago
        _executions_db(home, [("exec1", "unknown", claimed.replace(microsecond=0).isoformat())])

        now = mod.local_now()
        result = mod._stranded_upstream_fix_items(pending_dir, home, now)
        assert len(result) == 1
        assert result[0][0] == "177e3d8d"

    def test_no_flag_when_no_unknown_execution_exists(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        _write_pending(pending_dir, "upstream_fix", "abc", age_hours=1)
        _executions_db(home, [])

        now = mod.local_now()
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []

    def test_no_flag_when_execution_completed_not_unknown(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        _write_pending(pending_dir, "upstream_fix", "abc", age_hours=1)
        claimed = mod.local_now().replace(microsecond=0)
        _executions_db(home, [("exec1", "completed", claimed.isoformat())])

        now = mod.local_now()
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []

    def test_no_flag_when_claimed_time_outside_correlation_window(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        _write_pending(pending_dir, "upstream_fix", "abc", age_hours=1)
        # claimed 2 hours before the record was written -- outside the window
        claimed = (mod.local_now().replace(microsecond=0))
        from datetime import timedelta

        claimed = claimed - timedelta(hours=2)
        _executions_db(home, [("exec1", "unknown", claimed.isoformat())])

        now = mod.local_now()
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []

    def test_already_stale_record_not_double_flagged(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        path = _write_pending(pending_dir, "upstream_fix", "old", age_hours=80)
        claimed = mod.local_now().replace(microsecond=0)
        _executions_db(home, [("exec1", "unknown", claimed.isoformat())])

        now = mod.local_now()
        # ordinary staleness already covers this one -- stranded check skips it
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []

    def test_no_crash_when_job_missing_from_jobs_json(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = tmp_path / "hermes_home"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        _write_pending(pending_dir, "upstream_fix", "abc", age_hours=1)

        now = mod.local_now()
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []

    def test_no_crash_when_executions_db_missing(self, tmp_path: Path):
        pending_dir = tmp_path / "pending"
        home = tmp_path / "hermes_home"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": JOB_ID, "name": JOB_NAME}]})
        )
        _write_pending(pending_dir, "upstream_fix", "abc", age_hours=1)

        now = mod.local_now()
        assert mod._stranded_upstream_fix_items(pending_dir, home, now) == []


class TestMainOutput:
    def test_main_reports_stranded_and_stale_separately(self, tmp_path: Path, capsys, monkeypatch):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        stranded_path = _write_pending(pending_dir, "upstream_fix", "fresh_stranded")
        _set_mtime_minutes_ago(stranded_path, 2)
        claimed = mod.local_now() - timedelta(minutes=9)
        _executions_db(home, [("exec1", "unknown", claimed.replace(microsecond=0).isoformat())])
        _write_pending(pending_dir, "memory", "old_one", age_hours=80)

        monkeypatch.setattr(
            sys, "argv",
            ["pending-approval-nudge.py", "--pending-dir", str(pending_dir), "--hermes-home", str(home)],
        )
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "fresh_stranded" in out
        assert "never delivered" in out
        assert "old_one" in out
        assert "3+ days" in out

    def test_main_silent_when_nothing_to_report(self, tmp_path: Path, capsys, monkeypatch):
        pending_dir = tmp_path / "pending"
        home = _hermes_home(tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["pending-approval-nudge.py", "--pending-dir", str(pending_dir), "--hermes-home", str(home)],
        )
        rc = mod.main()
        assert rc == 0
        assert capsys.readouterr().out == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
