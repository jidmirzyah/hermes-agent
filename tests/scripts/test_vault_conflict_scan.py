"""Tests for scripts/vault-conflict-scan.py.

Covers the original count-and-notify behavior (conflict discovery,
`.stversions`/`.obsidian`/`.git` exclusion) plus SYNCGUARD Step 2's
diff-and-classify addition for `Execution Log.md` specifically: the three
classification outcomes (safe/duplicate, real-loss/clean, ambiguous), the
edge cases found during the SYNCGUARD investigation (missing `## Entries`
marker, an empty conflict file, a conflict file identical to canonical),
and that classification never fires for any other file (e.g. `Backlog.md`),
which must stay count-only exactly as before this existed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "vault-conflict-scan.py"

spec = importlib.util.spec_from_file_location("vault_conflict_scan", SCRIPT_PATH)
vault_conflict_scan = importlib.util.module_from_spec(spec)
# dataclasses under 'from __future__ import annotations' needs the module
# registered in sys.modules before exec, or it AttributeErrors resolving field types.
sys.modules["vault_conflict_scan"] = vault_conflict_scan
spec.loader.exec_module(vault_conflict_scan)

classify_conflict = vault_conflict_scan.classify_conflict
_find_conflicts = vault_conflict_scan._find_conflicts
_canonical_path_for = vault_conflict_scan._canonical_path_for
_is_classifiable = vault_conflict_scan._is_classifiable
_describe = vault_conflict_scan._describe

HEADER = (
    "# Execution Log\n\nSome preamble text.\n\n## PROTOCOL\n\nRules here.\n\n## Entries\n"
)


def _log(*entries: str) -> str:
    return HEADER + "\n" + "\n".join(entries)


ENTRY_A = "### Entry A (2026-09-01)\n\n- did a thing\n"
ENTRY_B = "### Entry B (2026-09-02)\n\n- did another thing\n"
ENTRY_C = "### Entry C (2026-09-03)\n\n- a third thing\n"


class TestClassifyConflict:
    def test_safe_duplicate_when_conflict_is_subset_of_canonical(self):
        conflict_text = _log(ENTRY_A)
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"
        assert result.missing_entry_titles == ()

    def test_safe_duplicate_when_files_are_identical(self):
        text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(text, text)
        assert result.category == "safe_duplicate"

    def test_safe_duplicate_when_conflict_has_no_entries(self):
        conflict_text = HEADER
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"
        assert "no entries" in result.reason

    def test_real_loss_clean_matches_the_2026_09_16_incident_shape(self):
        # Conflict and canonical share a prefix (ENTRY_A), then each grew a
        # different entry independently -- canonical's own new entry (B)
        # must not block detecting the conflict's missing one (the real
        # shape of the incident this classifier exists to catch).
        conflict_text = _log(ENTRY_A, ENTRY_C)
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == ("Entry C (2026-09-03)",)

    def test_real_loss_clean_with_no_common_entries_at_all(self):
        conflict_text = _log(ENTRY_C)
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == ("Entry C (2026-09-03)",)

    def test_real_loss_clean_reports_multiple_missing_entries(self):
        conflict_text = _log(ENTRY_A, ENTRY_B, ENTRY_C)
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == (
            "Entry B (2026-09-02)",
            "Entry C (2026-09-03)",
        )

    def test_safe_duplicate_even_when_entries_are_reordered(self):
        # Every conflict entry's exact text still exists somewhere in
        # canonical -- reordered, but nothing is actually missing. Per the
        # plan's own definition, "safe/duplicate" is order-independent:
        # nothing needs restoring either way, so nothing is at risk.
        conflict_text = _log(ENTRY_A, ENTRY_C)
        canonical_text = _log(ENTRY_C, ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"

    def test_safe_duplicate_ignores_preamble_difference_when_nothing_is_missing(self):
        # The preamble check only needs to gate the *restore* path (where
        # insertion-position math depends on a byte-identical prefix) -- if
        # every conflict entry is already present in canonical, nothing
        # needs restoring, so a preamble difference elsewhere is moot here.
        conflict_text = "# Execution Log\n\nDifferent preamble.\n\n## Entries\n\n" + ENTRY_A
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"

    def test_ambiguous_when_preamble_differs_and_content_is_genuinely_missing(self):
        # Now the preamble check's actual job: a real missing entry, but the
        # prefix it would be inserted relative to isn't provably identical --
        # must not guess at insertion position, fall through to ambiguous.
        conflict_text = "# Execution Log\n\nDifferent preamble.\n\n## Entries\n\n" + ENTRY_C
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "ambiguous"
        assert "preamble" in result.reason or "before" in result.reason

    def test_ambiguous_when_marker_missing_from_conflict(self):
        conflict_text = "# Execution Log\n\nNo marker here at all.\n"
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "ambiguous"
        assert "marker" in result.reason

    def test_ambiguous_when_marker_missing_from_canonical(self):
        conflict_text = _log(ENTRY_A)
        canonical_text = "# Execution Log\n\nNo marker here at all.\n"
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "ambiguous"
        assert "marker" in result.reason

    def test_ambiguous_when_both_files_lack_marker(self):
        result = classify_conflict("no marker\n", "also no marker\n")
        assert result.category == "ambiguous"

    def test_partial_entry_divergence_is_ambiguous(self):
        # Same entry title, different body -- not a whole-entry-boundary
        # difference. Must not be treated as safe or as a clean loss.
        conflict_text = _log("### Entry A (2026-09-01)\n\n- a DIFFERENT thing\n")
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "ambiguous"


class TestCanonicalPathFor:
    def test_strips_the_syncthing_conflict_marker(self):
        conflict = Path("/vault/Hermes/Execution Logs/Execution Log.sync-conflict-20260916-094549-KXKW46Z.md")
        assert _canonical_path_for(conflict).name == "Execution Log.md"

    def test_preserves_directory(self):
        conflict = Path("/vault/Hermes/Execution Logs/Execution Log.sync-conflict-20260831-232717-KXKW46Z.md")
        result = _canonical_path_for(conflict)
        assert result.parent == conflict.parent


class TestIsClassifiable:
    def test_execution_log_conflict_is_classifiable(self, tmp_path: Path):
        conflict = (
            tmp_path
            / "Hermes"
            / "Execution Logs"
            / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        )
        assert _is_classifiable(conflict, tmp_path) is True

    def test_backlog_conflict_is_not_classifiable(self, tmp_path: Path):
        conflict = (
            tmp_path
            / "Hermes"
            / "Execution Logs"
            / "Backlogs"
            / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        )
        assert _is_classifiable(conflict, tmp_path) is False

    def test_unrelated_file_is_not_classifiable(self, tmp_path: Path):
        conflict = tmp_path / "Hermes" / "Reflections" / "2026-09-01.sync-conflict-20260901-000000-ABC.md"
        assert _is_classifiable(conflict, tmp_path) is False


class TestFindConflicts:
    def test_excludes_stversions_obsidian_and_git(self, tmp_path: Path):
        (tmp_path / ".stversions").mkdir()
        (tmp_path / ".stversions" / "old.sync-conflict-x.md").write_text("x")
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".obsidian" / "y.sync-conflict-x.md").write_text("x")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "z.sync-conflict-x.md").write_text("x")
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        (real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md").write_text("x")

        found = _find_conflicts(tmp_path)
        assert len(found) == 1
        assert found[0].name == "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"

    def test_returns_empty_list_for_missing_vault_root(self, tmp_path: Path):
        assert _find_conflicts(tmp_path / "does-not-exist") == []

    def test_silent_when_nothing_found(self, tmp_path: Path):
        (tmp_path / "Hermes").mkdir()
        assert _find_conflicts(tmp_path) == []


class TestDescribeIntegration:
    def test_execution_log_real_loss_is_reported_with_missing_titles(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        canonical.write_text(_log(ENTRY_A, ENTRY_B))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        line = _describe(conflict, tmp_path)
        assert "REAL LOSS" in line
        assert "Entry C (2026-09-03)" in line

    def test_execution_log_safe_duplicate_is_reported_as_safe(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        (real / "Execution Log.md").write_text(_log(ENTRY_A, ENTRY_B))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))

        line = _describe(conflict, tmp_path)
        assert "safe" in line
        assert "REAL LOSS" not in line

    def test_backlog_conflict_stays_unclassified(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        (real / "Backlog.md").write_text(_log(ENTRY_A))
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        line = _describe(conflict, tmp_path)
        assert "safe" not in line
        assert "REAL LOSS" not in line
        assert "ambiguous" not in line
        assert line.strip() == str(conflict.relative_to(tmp_path))

    def test_missing_live_counterpart_is_reported_as_ambiguous(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))
        # No Execution Log.md written -- canonical file genuinely absent.

        line = _describe(conflict, tmp_path)
        assert "ambiguous" in line


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
