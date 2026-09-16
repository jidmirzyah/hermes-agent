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
that stays a manual reconcile-on-sight decision, same as the incidents this
exists to catch earlier. `.stversions/` is Syncthing's own version-history
archive and is expected to contain old conflict-file names permanently; it
is excluded so this never fires on history, only on a live, unresolved
conflict sitting in the actual vault tree.

Added 2026-09-16 (SYNCGUARD Step 2): for `Hermes/Execution Logs/
Execution Log.md` specifically -- the one shared file with a structure
reliable enough to trust (an enforced `## Entries` marker, the same one
`execution-log-rotate.py` already depends on and refuses to guess without,
plus whole `### `-bounded entries) -- classify each conflict instead of
just counting it: "safe, nothing lost" vs. "real loss, here's exactly what's
missing" vs. "ambiguous, reconcile by hand". Still never writes anything;
classification is reporting only. Every other file (including
`Backlogs/Backlog.md`, whose bullet-list structure doesn't earn the same
parsing confidence) stays count-only and unclassified, same as before this
existed.

Silent when nothing is found (no_agent cron convention: empty stdout = no
notification), matching sync-fork.sh and pending-approval-nudge.py.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIR_NAMES = (".stversions", ".obsidian", ".git")

# Relative to the vault root. The only file this script will attempt to
# classify a conflict against -- see the module docstring for why.
CLASSIFIABLE_RELATIVE_PATHS = ("Hermes/Execution Logs/Execution Log.md",)

ENTRIES_MARKER = "## Entries\n"
ENTRY_HEADER_PREFIX = "### "

_CONFLICT_MARKER_RE = re.compile(r"\.sync-conflict-\d{8}-\d{6}-[A-Za-z0-9]+")


@dataclass(frozen=True)
class Classification:
    """Result of comparing a conflict file against its live counterpart.

    category is one of:
      "safe_duplicate"  -- every entry in the conflict file already exists
                            in canonical (verbatim, anywhere). Nothing lost.
      "real_loss_clean" -- a clean, provable case: conflict and canonical
                            share a byte-identical whole-entry prefix, and
                            the conflict file has one or more whole entries
                            after that point that are entirely absent from
                            canonical. missing_entry_titles names them.
      "ambiguous"       -- anything else. Never touched, never guessed at;
                            reconcile by hand, same as before this existed.
    """

    category: str
    missing_entry_titles: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


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


def _canonical_path_for(conflict_path: Path) -> Path:
    """The live file a conflict copy was parked from, by stripping the
    Syncthing-inserted `.sync-conflict-<date>-<time>-<deviceid>` marker out
    of the filename. Does not check the result exists -- caller's job."""
    new_name = _CONFLICT_MARKER_RE.sub("", conflict_path.name)
    return conflict_path.with_name(new_name)


def _is_classifiable(conflict_path: Path, vault_root: Path) -> bool:
    canonical = _canonical_path_for(conflict_path)
    try:
        rel = canonical.relative_to(vault_root).as_posix()
    except ValueError:
        return False
    return rel in CLASSIFIABLE_RELATIVE_PATHS


def _normalize_entry(lines: list[str]) -> str:
    """Collapse trailing blank-line noise to exactly one trailing newline.

    Without this, the same entry's text differs depending on whether
    something follows it in the file (a "\\n\\n" separator before the next
    '### ' line becomes part of the preceding entry) or it's the last entry
    (no separator at all) -- breaking exact-text comparison for no reason
    that reflects an actual content difference.
    """
    return "".join(lines).rstrip("\n") + "\n"


def _split_entries(text: str) -> tuple[str, list[str]] | None:
    """Split text into (preamble through '## Entries' plus any immediate
    blank lines, [whole entry texts]).

    Returns None if the '## Entries' marker is missing -- the exact same
    marker `execution-log-rotate.py` already depends on and refuses to
    guess without. Each entry starts at a line beginning with '### ' and
    runs to just before the next one (or end of file).
    """
    idx = text.find(ENTRIES_MARKER)
    if idx == -1:
        return None
    body = text[idx + len(ENTRIES_MARKER) :]

    lines = body.splitlines(keepends=True)
    preamble_extra: list[str] = []
    entries: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith(ENTRY_HEADER_PREFIX):
            if current is not None:
                entries.append(_normalize_entry(current))
            current = [line]
        elif current is None:
            preamble_extra.append(line)
        else:
            current.append(line)
    if current is not None:
        entries.append(_normalize_entry(current))

    preamble = text[: idx + len(ENTRIES_MARKER)] + "".join(preamble_extra)
    return preamble, entries


def _entry_title(entry_text: str) -> str:
    first_line = entry_text.splitlines()[0] if entry_text else ""
    if first_line.startswith(ENTRY_HEADER_PREFIX):
        return first_line[len(ENTRY_HEADER_PREFIX) :].strip()
    return first_line.strip()


def classify_conflict(conflict_text: str, canonical_text: str) -> Classification:
    """Classify a sync-conflict file's content against its live counterpart.

    Pure function, no I/O, never guesses: anything short of a clean,
    provable case comes back "ambiguous".
    """
    conflict_split = _split_entries(conflict_text)
    canonical_split = _split_entries(canonical_text)

    if conflict_split is None or canonical_split is None:
        return Classification(
            "ambiguous", reason="'## Entries' marker missing in one or both files"
        )

    conflict_preamble, conflict_entries = conflict_split
    canonical_preamble, canonical_entries = canonical_split

    if not conflict_entries:
        return Classification("safe_duplicate", reason="conflict file has no entries")

    if all(entry in canonical_entries for entry in conflict_entries):
        return Classification(
            "safe_duplicate", reason="every conflict entry already exists in canonical"
        )

    if conflict_preamble != canonical_preamble:
        return Classification(
            "ambiguous", reason="content before '## Entries' differs between files"
        )

    common_len = 0
    for a, b in zip(conflict_entries, canonical_entries):
        if a != b:
            break
        common_len += 1

    conflict_tail = conflict_entries[common_len:]
    canonical_titles = {_entry_title(e) for e in canonical_entries}

    tail_is_clean = conflict_tail and all(
        entry not in canonical_entries and _entry_title(entry) not in canonical_titles
        for entry in conflict_tail
    )
    if tail_is_clean:
        return Classification(
            "real_loss_clean",
            missing_entry_titles=tuple(_entry_title(e) for e in conflict_tail),
            reason="clean common prefix; conflict has whole entries missing from canonical",
        )

    return Classification(
        "ambiguous", reason="entries diverge but not in a cleanly restorable shape"
    )


def _describe(conflict_path: Path, vault_root: Path) -> str:
    rel = conflict_path.relative_to(vault_root)
    if not _is_classifiable(conflict_path, vault_root):
        return f"  {rel}"

    canonical_path = _canonical_path_for(conflict_path)
    if not canonical_path.is_file():
        return f"  {rel}  [ambiguous: no live counterpart found at {canonical_path.name}]"

    try:
        conflict_text = conflict_path.read_text(encoding="utf-8")
        canonical_text = canonical_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"  {rel}  [ambiguous: could not read a file -- {exc}]"

    result = classify_conflict(conflict_text, canonical_text)

    if result.category == "safe_duplicate":
        return f"  {rel}  [safe: {result.reason} -- nothing lost, fine to delete]"
    if result.category == "real_loss_clean":
        titles = "; ".join(result.missing_entry_titles)
        return f"  {rel}  [REAL LOSS: missing from canonical -- {titles}]"
    return f"  {rel}  [ambiguous: {result.reason} -- reconcile by hand]"


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
        print(_describe(path, args.vault_root))
    print(
        "\nEach one parked content Syncthing couldn't merge -- read both sides "
        "and reconcile manually before it's forgotten."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
