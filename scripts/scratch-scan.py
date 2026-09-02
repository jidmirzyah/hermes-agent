#!/usr/bin/env python3
"""Weekly inventory of ~/reconcile-work/. Report only, no judgment, no delete.

Lists every top-level directory with its size and age. Deciding what's safe
to remove needs checking each directory's git state against live main (see
the 2026-09-01 SCRATCHGC cleanup for the method) -- real judgment, not
something this script attempts. It only makes sure accumulation doesn't sit
unnoticed the way it did before that cleanup (13 GB, 11 abandoned clones).

Silent below the size threshold. Never deletes, moves, or modifies anything.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from foundation_cron_common import age_hours, local_now

ALERT_THRESHOLD_GB = 5.0


def _dir_size_mb(path: Path) -> float:
    result = subprocess.run(
        ["du", "-sm", str(path)], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0.0
    return float(result.stdout.split()[0])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scratch-dir", type=Path, default=Path.home() / "reconcile-work"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.scratch_dir.is_dir():
        return 0

    now = local_now()
    entries: list[tuple[str, float, float]] = []
    for path in sorted(args.scratch_dir.iterdir()):
        if not path.is_dir():
            continue
        size_mb = _dir_size_mb(path)
        age_days = age_hours(path, now) / 24
        entries.append((path.name, size_mb, age_days))

    total_gb = sum(size for _, size, _ in entries) / 1024
    if total_gb < ALERT_THRESHOLD_GB:
        return 0

    entries.sort(key=lambda e: -e[1])
    plural = "y" if len(entries) == 1 else "ies"
    print(
        f"~/reconcile-work/ holds {len(entries)} director{plural}, "
        f"{total_gb:.1f} GB total (alert threshold {ALERT_THRESHOLD_GB:g} GB):\n"
    )
    for name, size_mb, age_days in entries:
        print(f"  {size_mb / 1024:6.2f} GB  {age_days:6.1f}d old  {name}")
    print(
        "\nInventory only -- nothing here has been judged safe to delete. "
        "Check each directory's git state against live main before removing anything."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
