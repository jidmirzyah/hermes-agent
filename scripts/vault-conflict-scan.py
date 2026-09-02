#!/usr/bin/env python3
"""Notify when a Syncthing sync-conflict file is sitting in the live vault.

Jarvis (VM copy) and Claude Code (Mac copy) writing the same vault file
concurrently produces a Syncthing conflict. Syncthing syncs whole files and
does not merge text: it keeps one version and parks the other as
`*.sync-conflict-*`, which nothing reads. Demonstrated, not theoretical --
2026-08-31 23:27 silently orphaned an Execution Log entry and a Backlog
item; both were recovered only because a manual check happened to run.

Read-only, count-and-notify only. Never merges, deletes, or picks a side --
that stays a manual reconcile-on-sight decision, same as the incident this
exists to catch earlier. `.stversions/` is Syncthing's own version-history
archive and is expected to contain old conflict-file names permanently; it
is excluded so this never fires on history, only on a live, unresolved
conflict sitting in the actual vault tree.

Silent when nothing is found (no_agent cron convention: empty stdout = no
notification), matching sync-fork.sh and pending-approval-nudge.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path

EXCLUDED_DIR_NAMES = (".stversions", ".obsidian", ".git")


def _find_conflicts(vault_root: Path) -> list[Path]:
    if not vault_root.is_dir():
        return []
    found: list[Path] = []
    for path in vault_root.rglob("*sync-conflict*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(vault_root).parts):
            continue
        found.append(path)
    return found


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conflicts = _find_conflicts(args.vault_root)
    if not conflicts:
        return 0

    conflicts.sort()
    plural = "s" if len(conflicts) != 1 else ""
    print(f"{len(conflicts)} unresolved sync-conflict file{plural} in the live vault:\n")
    for path in conflicts:
        print(f"  {path.relative_to(args.vault_root)}")
    print(
        "\nEach one parked content Syncthing couldn't merge -- read both sides "
        "and reconcile manually before it's forgotten."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
