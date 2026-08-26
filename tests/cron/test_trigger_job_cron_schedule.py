"""Force-running a cron-scheduled job (regression for the #93049 interaction).

``trigger_job`` re-runs a job by setting ``next_run_at`` to now. For a
``kind: cron`` job that instant is off-schedule by definition, so the
stale-schedule guard added for #93049 re-anchored it *without firing* — every
force-run of a cron job silently did nothing. That covers ``hermes cron run``
and the daily-brief validator's self-heal, whose entire purpose is retrying a
brief that failed to save.

The pre-existing coverage (``test_trigger_job_unwedges_persisted_state``) builds
its fixture with ``schedule="every 10m"`` -> ``kind: interval``, which the guard
does not inspect, so the regression merged green. These tests pin the cron path.

Real store against a temp ``HERMES_HOME``, no mocks beyond the clock, matching
``test_due_stale_cron_edit.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


# A Saturday, and deliberately not on any "0 7 * * *" occurrence.
_OFF_SCHEDULE = datetime.fromisoformat("2026-08-22T09:13:07+00:00")


def _make_cron_job(expr: str = "0 7 * * *", **overrides) -> str:
    """Persist a job carrying a real cron schedule."""
    from cron.jobs import create_job, load_jobs, save_jobs

    job = create_job(prompt="x", schedule="every 5m", name="t")
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["schedule"] = {"kind": "cron", "expr": expr}
            j["schedule_display"] = expr
            j.update(overrides)
    save_jobs(jobs)
    return job["id"]


def test_force_run_of_cron_job_becomes_due(temp_home, monkeypatch):
    """The regression: trigger_job on a cron job must actually make it due."""
    from cron.jobs import get_due_jobs, trigger_job

    jid = _make_cron_job("0 7 * * *")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: _OFF_SCHEDULE)
    assert trigger_job(jid) is not None

    monkeypatch.setattr(
        "cron.jobs._hermes_now", lambda: _OFF_SCHEDULE + timedelta(seconds=1)
    )
    assert jid in [j["id"] for j in get_due_jobs()]


def test_force_run_stamps_marker_matching_next_run_at(temp_home, monkeypatch):
    """The marker is what distinguishes a force-run, so it must equal the
    instant written to next_run_at exactly."""
    from cron.jobs import get_job, trigger_job

    jid = _make_cron_job("0 7 * * *")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: _OFF_SCHEDULE)
    trigger_job(jid)

    stored = get_job(jid)
    assert stored["manual_trigger_at"] == stored["next_run_at"]
    assert stored["next_run_at"] == _OFF_SCHEDULE.isoformat()


def test_hand_edited_off_schedule_instant_still_re_anchors(temp_home, monkeypatch):
    """Negative control: the guard must keep doing its job. The same
    off-schedule instant, arrived at WITHOUT a force-run, still re-anchors."""
    from cron.jobs import get_due_jobs, get_job

    jid = _make_cron_job("0 7 * * *", next_run_at=_OFF_SCHEDULE.isoformat())
    monkeypatch.setattr(
        "cron.jobs._hermes_now", lambda: _OFF_SCHEDULE + timedelta(seconds=1)
    )

    assert [j["id"] for j in get_due_jobs() if j["id"] == jid] == []
    assert get_job(jid)["next_run_at"] != _OFF_SCHEDULE.isoformat()


def test_marker_not_matching_next_run_at_is_ignored(temp_home, monkeypatch):
    """A leftover marker must not license a later off-schedule instant —
    equality is the whole safety property."""
    from cron.jobs import get_due_jobs, get_job

    jid = _make_cron_job(
        "0 7 * * *",
        next_run_at=_OFF_SCHEDULE.isoformat(),
        manual_trigger_at=(_OFF_SCHEDULE - timedelta(days=1)).isoformat(),
    )
    monkeypatch.setattr(
        "cron.jobs._hermes_now", lambda: _OFF_SCHEDULE + timedelta(seconds=1)
    )

    assert [j["id"] for j in get_due_jobs() if j["id"] == jid] == []
    assert get_job(jid)["next_run_at"] != _OFF_SCHEDULE.isoformat()


def test_is_manual_trigger_requires_exact_equality(temp_home):
    """Unit-level: absent, empty, and mismatched markers all report False."""
    from cron.jobs import _is_manual_trigger

    stamp = _OFF_SCHEDULE.isoformat()
    assert _is_manual_trigger({"manual_trigger_at": stamp}, stamp) is True
    assert _is_manual_trigger({}, stamp) is False
    assert _is_manual_trigger({"manual_trigger_at": None}, stamp) is False
    assert _is_manual_trigger({"manual_trigger_at": ""}, stamp) is False
    assert _is_manual_trigger({"manual_trigger_at": stamp}, None) is False
    assert _is_manual_trigger({"manual_trigger_at": stamp}, "2026-01-01T00:00:00") is False


def test_interval_jobs_are_unaffected(temp_home, monkeypatch):
    """The guard never inspected interval kinds; force-run there is unchanged."""
    from cron.jobs import create_job, get_due_jobs, trigger_job

    job = create_job(prompt="x", schedule="every 10m", name="i")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: _OFF_SCHEDULE)
    assert trigger_job(job["id"]) is not None

    monkeypatch.setattr(
        "cron.jobs._hermes_now", lambda: _OFF_SCHEDULE + timedelta(seconds=1)
    )
    assert job["id"] in [j["id"] for j in get_due_jobs()]
