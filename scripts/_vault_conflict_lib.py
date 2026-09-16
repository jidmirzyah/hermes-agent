"""Shared library for detecting, classifying, and (for the provably-safe
cases only) reconciling Syncthing `*.sync-conflict-*` files in the vault.

Extracted from `vault-conflict-scan.py` (SYNCGUARD Step 2) when Step 3 added
a second consumer (`vault-log-append.py`) and a third (`execution-log-
rotate.py`'s pre-read reconcile hook) that all need the exact same
classification logic, not a re-implementation of it.

Classification is restricted to `Hermes/Execution Logs/Execution Log.md` --
the one shared file with a structure reliable enough to trust (an enforced
`## Entries` marker, the same one `execution-log-rotate.py` already depends
on and refuses to guess without, plus whole `### `-bounded entries).
`Backlogs/Backlog.md` and everything else stays unclassified.

`reconcile()` is the only function here that writes or deletes anything.
It only ever acts on the two provably-safe classifications:
  - safe_duplicate  -- deletes the now-redundant conflict file, nothing
                        needed from canonical.
  - real_loss_clean -- inserts the specific missing whole entries into
                        canonical at the correct position, then deletes the
                        conflict file.
"ambiguous" is never touched -- same manual-reconcile-by-hand fallback this
whole mechanism has always had.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIR_NAMES = (".stversions", ".obsidian", ".git")

# Relative to the vault root. The only file this library will attempt to
# classify or reconcile a conflict against -- see the module docstring.
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
                            canonical (by text AND by title, so a same-
                            titled entry with an edited body is never
                            mistaken for a clean addition). missing_entries
                            names and carries the full text of each.
      "ambiguous"       -- anything else. Never touched, never guessed at;
                            reconcile by hand, same as before this existed.
    """

    category: str
    missing_entries: tuple[str, ...] = field(default_factory=tuple)
    common_len: int = 0
    reason: str = ""

    @property
    def missing_entry_titles(self) -> tuple[str, ...]:
        return tuple(_entry_title(e) for e in self.missing_entries)


@dataclass(frozen=True)
class ReconcileResult:
    """What `reconcile()` did for one conflict file."""

    conflict_path: Path
    category: str
    message: str


def find_conflicts(vault_root: Path) -> list[Path]:
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


def canonical_path_for(conflict_path: Path) -> Path:
    """The live file a conflict copy was parked from, by stripping the
    Syncthing-inserted `.sync-conflict-<date>-<time>-<deviceid>` marker out
    of the filename. Does not check the result exists -- caller's job."""
    new_name = _CONFLICT_MARKER_RE.sub("", conflict_path.name)
    return conflict_path.with_name(new_name)


def is_classifiable(conflict_path: Path, vault_root: Path) -> bool:
    canonical = canonical_path_for(conflict_path)
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
            missing_entries=tuple(conflict_tail),
            common_len=common_len,
            reason="clean common prefix; conflict has whole entries missing from canonical",
        )

    return Classification(
        "ambiguous", reason="entries diverge but not in a cleanly restorable shape"
    )


def describe(conflict_path: Path, vault_root: Path) -> str:
    """Human-readable one-line (or so) report for a single conflict path,
    used by `vault-conflict-scan.py`. Never writes anything."""
    rel = conflict_path.relative_to(vault_root)
    if not is_classifiable(conflict_path, vault_root):
        return f"  {rel}"

    canonical_path = canonical_path_for(conflict_path)
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


def reconcile(conflict_path: Path, vault_root: Path) -> ReconcileResult | None:
    """Act on ONE conflict file for a classifiable canonical target.

    Only ever performs the two provably-safe operations described in the
    module docstring. Returns None (does nothing) if the conflict isn't
    classifiable, its counterpart is missing/unreadable, or classification
    comes back "ambiguous" -- in every one of those cases this function is
    a pure no-op, exactly as before this mechanism existed.
    """
    if not is_classifiable(conflict_path, vault_root):
        return None

    canonical_path = canonical_path_for(conflict_path)
    if not canonical_path.is_file():
        return None

    try:
        conflict_text = conflict_path.read_text(encoding="utf-8")
        canonical_text = canonical_path.read_text(encoding="utf-8")
    except OSError:
        return None

    result = classify_conflict(conflict_text, canonical_text)

    if result.category == "safe_duplicate":
        conflict_path.unlink()
        return ReconcileResult(
            conflict_path,
            result.category,
            f"removed redundant conflict file {conflict_path.name} ({result.reason})",
        )

    if result.category == "real_loss_clean":
        canonical_split = _split_entries(canonical_text)
        assert canonical_split is not None  # classify_conflict already proved this
        canonical_preamble, canonical_entries = canonical_split

        insert_at = result.common_len
        restored_entries = (
            canonical_entries[:insert_at]
            + list(result.missing_entries)
            + canonical_entries[insert_at:]
        )
        new_canonical_text = canonical_preamble + "\n".join(restored_entries)
        canonical_path.write_text(new_canonical_text, encoding="utf-8")
        conflict_path.unlink()

        titles = "; ".join(result.missing_entry_titles)
        return ReconcileResult(
            conflict_path,
            result.category,
            f"restored {len(result.missing_entries)} missing entr"
            f"{'y' if len(result.missing_entries) == 1 else 'ies'} from "
            f"{conflict_path.name}: {titles}",
        )

    return None


def reconcile_all(vault_root: Path) -> list[ReconcileResult]:
    """Reconcile every classifiable conflict found under vault_root. Safe
    to call unconditionally (e.g. before every append or every rotation
    run) -- a no-op when nothing is pending."""
    results: list[ReconcileResult] = []
    for conflict_path in find_conflicts(vault_root):
        outcome = reconcile(conflict_path, vault_root)
        if outcome is not None:
            results.append(outcome)
    return results
