#!/usr/bin/env python3
"""Notify when a Syncthing sync-conflict file is sitting in the live vault.

Jarvis (VM copy) and Claude Code (Mac copy) writing the same vault file
concurrently produces a Syncthing conflict. Syncthing syncs whole files and
does not merge text: it keeps one version and parks the other as
`*.sync-conflict-*`, which nothing reads. Demonstrated, not theoretical --
2026-08-31 23:27 silently orphaned an Execution Log entry and a Backlog
item, and 2026-09-16 09:45 silently dropped a full Execution Log entry from
canonical; both were recovered only because a manual check happened to run.

Read-only, count-and-notify only. Never merges, deletes, or picks a side --
that's `_vault_conflict_lib.reconcile()`'s job now (SYNCGUARD Step 3, wired
into `vault-log-append.py` and `execution-log-rotate.py`, not this script).
`.stversions/` is Syncthing's own version-history archive and is expected to
contain old conflict-file names permanently; it is excluded so this never
fires on history, only on a live, unresolved conflict sitting in the actual
vault tree.

Added 2026-09-16 (SYNCGUARD Step 2): for `Hermes/Execution Logs/
Execution Log.md` specifically -- the one shared file with a structure
reliable enough to trust -- classify each conflict instead of just counting
it: "safe, nothing lost" vs. "real loss, here's exactly what's missing" vs.
"ambiguous, reconcile by hand". Still never writes anything itself; see
`_vault_conflict_lib.py` for the shared classification logic and (Step 3)
the actual reconcile/restore action, both used by multiple callers now.
Every other file (including `Backlogs/Backlog.md`) stays count-only and
unclassified, same as before any of this existed.

Silent when nothing is found (no_agent cron convention: empty stdout = no
notification), matching sync-fork.sh and pending-approval-nudge.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "_vault_conflict_lib", _SCRIPTS_DIR / "_vault_conflict_lib.py"
)
assert _spec is not None and _spec.loader is not None
_vault_conflict_lib = importlib.util.module_from_spec(_spec)
sys.modules["_vault_conflict_lib"] = _vault_conflict_lib
_spec.loader.exec_module(_vault_conflict_lib)

find_conflicts = _vault_conflict_lib.find_conflicts
describe = _vault_conflict_lib.describe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conflicts = find_conflicts(args.vault_root)
    if not conflicts:
        return 0

    conflicts.sort()
    plural = "s" if len(conflicts) != 1 else ""
    print(f"{len(conflicts)} unresolved sync-conflict file{plural} in the live vault:\n")
    for path in conflicts:
        print(describe(path, args.vault_root))
    print(
        "\nEach one parked content Syncthing couldn't merge -- read both sides "
        "and reconcile manually before it's forgotten."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
