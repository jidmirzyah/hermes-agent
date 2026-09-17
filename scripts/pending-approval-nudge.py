#!/usr/bin/env python3
"""Notify when a pending memory/skill approval has sat unreviewed too long.

Read-only, count-and-notify only. Never modifies, approves, or discards a
pending record -- that stays with `/memory pending` and `/skills pending`.
Root cause of a real gap: 12 records sat 7-12 days unnoticed because nothing
surfaced them until someone thought to check.

Added 2026-09-17 (UPREVIEWFIX): a second check for `upstream_fix` records that
correlates a fresh record's write time against an `unknown`-status execution
of the same job claimed shortly before it -- flags immediately, independent
of the 3-day threshold above.

Silent when nothing is stale (no_agent cron convention: empty stdout = no
notification), matching sync-fork.sh and backlog-rotate.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from foundation_cron_common import age_hours, local_now, parse_datetime

STALE_AGE_DAYS = 3.0
SUBSYSTEMS = ("memory", "skills", "upstream_fix")

STRANDED_CHECK_JOB_NAME = "hermes-upstream-main-check"
STRANDED_CORRELATION_WINDOW_SECONDS = 600.0


def _stale_items(pending_dir: Path, now) -> list[tuple[str, str, float]]:
    stale: list[tuple[str, str, float]] = []
    for subsystem in SUBSYSTEMS:
        directory = pending_dir / subsystem
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if not path.is_file():
                continue
            age_days = age_hours(path, now) / 24
            if age_days >= STALE_AGE_DAYS:
                stale.append((subsystem, path.stem, age_days))
    return stale


def _job_id_for_name(hermes_home: Path, job_name: str) -> str | None:
    jobs_path = hermes_home / "cron" / "jobs.json"
    if not jobs_path.is_file():
        return None
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    for job in jobs:
        if job.get("name") == job_name:
            return job.get("id")
    return None


def _stranded_upstream_fix_items(
    pending_dir: Path, hermes_home: Path, now
) -> list[tuple[str, float]]:
    """Fresh `upstream_fix` record whose write time lines up with an
    `unknown`-status execution claimed shortly before it -- likely finished
    but never delivered."""
    directory = pending_dir / "upstream_fix"
    if not directory.is_dir():
        return []

    job_id = _job_id_for_name(hermes_home, STRANDED_CHECK_JOB_NAME)
    if job_id is None:
        return []

    executions_db = hermes_home / "cron" / "executions.db"
    if not executions_db.is_file():
        return []

    conn = sqlite3.connect(f"file:{executions_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT claimed_at FROM executions WHERE job_id = ? AND status = 'unknown'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    claimed_times = []
    for (claimed_at,) in rows:
        if not claimed_at:
            continue
        try:
            claimed_times.append(parse_datetime(claimed_at))
        except ValueError:
            continue
    if not claimed_times:
        return []

    found: list[tuple[str, float]] = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        age_days = age_hours(path, now) / 24
        if age_days >= STALE_AGE_DAYS:
            continue  # already surfaced by the ordinary staleness check
        created = parse_datetime(datetime.fromtimestamp(path.stat().st_mtime).isoformat())
        for claimed in claimed_times:
            delta = (created - claimed).total_seconds()
            if 0 <= delta <= STRANDED_CORRELATION_WINDOW_SECONDS:
                found.append((path.stem, age_days * 24))
                break
    return found


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pending-dir", type=Path, default=Path.home() / ".hermes/pending"
    )
    parser.add_argument(
        "--hermes-home", type=Path, default=Path.home() / ".hermes"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = local_now()
    stale = _stale_items(args.pending_dir, now)
    stranded = _stranded_upstream_fix_items(args.pending_dir, args.hermes_home, now)

    if not stale and not stranded:
        return 0

    if stranded:
        plural = "s" if len(stranded) != 1 else ""
        print(
            f"{len(stranded)} upstream review{plural} likely finished but never "
            "delivered (owning process died before it could report):\n"
        )
        for item_id, age_h in stranded:
            print(f"  [upstream_fix] {item_id} — completed {age_h:.1f}h ago, undelivered")
        print(
            "\nNo [ref:...] message was ever delivered for these, so there is nothing to "
            "quote-reply to. Ask Jarvis directly to review the pending record by ID."
        )
        if stale:
            print()

    if stale:
        stale.sort(key=lambda item: -item[2])
        plural = "s" if len(stale) != 1 else ""
        print(
            f"{len(stale)} pending approval{plural} unreviewed for "
            f"{STALE_AGE_DAYS:.0f}+ days:\n"
        )
        for subsystem, item_id, age_days in stale:
            print(f"  [{subsystem}] {item_id} — {age_days:.1f} days old")
        print("\nReview with: /memory pending   or   /skills pending")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
