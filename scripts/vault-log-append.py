#!/usr/bin/env python3
"""Append a new entry to a vault log, self-healing any pending Syncthing
conflict for that exact file first.

SYNCGUARD Step 3. This is the actual prevention mechanism for the failure
mode `vault-conflict-scan.py` (Step 1-2) can only detect after the fact:
2026-09-16's incident lost a whole Execution Log entry because a *third*
write landed on top of an already-conflicted file before anyone happened to
check for a stray `*.sync-conflict-*` file. Wrapping every append in this
script closes that gap mechanically instead of depending on memory.

Only ever acts on the same two provably-safe classifications
`_vault_conflict_lib.reconcile()` always has: a redundant duplicate gets
deleted, a cleanly-missing whole entry gets restored into the right
position. An "ambiguous" conflict is left completely untouched -- the
append still proceeds normally (this script is not a gate), but the
ambiguous conflict file is reported so it doesn't go unnoticed.

Restricted, like the rest of SYNCGUARD, to `Execution Log.md` -- reconcile
is a no-op for any other file (see `_vault_conflict_lib.CLASSIFIABLE_
RELATIVE_PATHS`), so this script is safe to point at `Backlogs/Backlog.md`
or anything else too; it just won't self-heal a conflict there, same as
before this existed. Appending itself works for any file, always.
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


def _reconcile_for_file(target_path: Path, vault_root: Path) -> list[str]:
    """Reconcile only conflicts whose canonical target is target_path, not
    the whole vault -- an append to one file should never touch another."""
    messages: list[str] = []
    for conflict_path in _vault_conflict_lib.find_conflicts(vault_root):
        if _vault_conflict_lib.canonical_path_for(conflict_path) != target_path:
            continue
        outcome = _vault_conflict_lib.reconcile(conflict_path, vault_root)
        if outcome is not None:
            messages.append(outcome.message)
        else:
            # Either not classifiable, counterpart missing, or ambiguous.
            # Surface it either way -- silence here is exactly the gap
            # this script exists to close.
            try:
                canonical = _vault_conflict_lib.canonical_path_for(conflict_path)
                if canonical.is_file():
                    result = _vault_conflict_lib.classify_conflict(
                        conflict_path.read_text(encoding="utf-8"),
                        canonical.read_text(encoding="utf-8"),
                    )
                    messages.append(
                        f"left untouched (ambiguous): {conflict_path.name} -- "
                        f"{result.reason} -- reconcile by hand"
                    )
                else:
                    messages.append(
                        f"left untouched: {conflict_path.name} has no live counterpart"
                    )
            except OSError as exc:
                messages.append(f"left untouched: could not read {conflict_path.name} ({exc})")
    return messages


def append_entry(target_path: Path, vault_root: Path, entry_text: str) -> list[str]:
    """Reconcile any pending conflict for target_path, then append
    entry_text (a whole '### '-headed entry) to it. Returns the list of
    reconcile messages (empty if nothing was pending)."""
    messages = _reconcile_for_file(target_path, vault_root)

    current = target_path.read_text(encoding="utf-8") if target_path.is_file() else ""
    entry_text = entry_text.strip("\n") + "\n"
    if current and not current.endswith("\n"):
        current += "\n"
    separator = "\n" if current else ""
    target_path.write_text(current + separator + entry_text, encoding="utf-8")

    return messages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    parser.add_argument(
        "--file",
        default="Hermes/Execution Logs/Execution Log.md",
        help="Path relative to --vault-root.",
    )
    entry_source = parser.add_mutually_exclusive_group(required=True)
    entry_source.add_argument("--entry-file", type=Path, help="Read the new entry from this file.")
    entry_source.add_argument("--entry-text", help="The new entry text, inline.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_path = args.vault_root / args.file

    if args.entry_file is not None:
        entry_text = args.entry_file.read_text(encoding="utf-8")
    else:
        entry_text = args.entry_text

    messages = append_entry(target_path, args.vault_root, entry_text)

    for message in messages:
        print(message)
    print(f"Appended to {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
