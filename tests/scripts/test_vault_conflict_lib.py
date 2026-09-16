"""Tests for scripts/_vault_conflict_lib.py.

Covers conflict discovery, path resolution, the three-way classification
(safe/duplicate, real-loss/clean, ambiguous) including the edge cases found
during the SYNCGUARD investigation (missing `## Entries` marker, an empty
conflict file, entries identical to canonical, a same-titled edited entry),
and (SYNCGUARD Step 3) `reconcile()`/`reconcile_all()` -- the only
functions in this codebase that actually write or delete anything for a
sync-conflict, and only for the two provably-safe classifications.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LIB_PATH = Path(__file__).resolve().parents[2] / "scripts" / "_vault_conflict_lib.py"

spec = importlib.util.spec_from_file_location("_vault_conflict_lib", LIB_PATH)
lib = importlib.util.module_from_spec(spec)
# dataclasses under 'from __future__ import annotations' needs the module
# registered in sys.modules before exec, or it AttributeErrors resolving
# field types.
sys.modules["_vault_conflict_lib"] = lib
spec.loader.exec_module(lib)

classify_conflict = lib.classify_conflict
find_conflicts = lib.find_conflicts
canonical_path_for = lib.canonical_path_for
is_classifiable = lib.is_classifiable
describe = lib.describe
reconcile = lib.reconcile
reconcile_all = lib.reconcile_all

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
        conflict_text = _log(ENTRY_A, ENTRY_C)
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == ("Entry C (2026-09-03)",)
        assert result.common_len == 1

    def test_real_loss_clean_with_no_common_entries_at_all(self):
        conflict_text = _log(ENTRY_C)
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == ("Entry C (2026-09-03)",)
        assert result.common_len == 0

    def test_real_loss_clean_reports_multiple_missing_entries(self):
        conflict_text = _log(ENTRY_A, ENTRY_B, ENTRY_C)
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "real_loss_clean"
        assert result.missing_entry_titles == (
            "Entry B (2026-09-02)",
            "Entry C (2026-09-03)",
        )
        assert result.common_len == 1

    def test_safe_duplicate_even_when_entries_are_reordered(self):
        conflict_text = _log(ENTRY_A, ENTRY_C)
        canonical_text = _log(ENTRY_C, ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"

    def test_safe_duplicate_ignores_preamble_difference_when_nothing_is_missing(self):
        conflict_text = "# Execution Log\n\nDifferent preamble.\n\n## Entries\n\n" + ENTRY_A
        canonical_text = _log(ENTRY_A, ENTRY_B)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "safe_duplicate"

    def test_ambiguous_when_preamble_differs_and_content_is_genuinely_missing(self):
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
        conflict_text = _log("### Entry A (2026-09-01)\n\n- a DIFFERENT thing\n")
        canonical_text = _log(ENTRY_A)
        result = classify_conflict(conflict_text, canonical_text)
        assert result.category == "ambiguous"


class TestCanonicalPathFor:
    def test_strips_the_syncthing_conflict_marker(self):
        conflict = Path(
            "/vault/Hermes/Execution Logs/"
            "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        )
        assert canonical_path_for(conflict).name == "Execution Log.md"

    def test_preserves_directory(self):
        conflict = Path(
            "/vault/Hermes/Execution Logs/"
            "Execution Log.sync-conflict-20260831-232717-KXKW46Z.md"
        )
        result = canonical_path_for(conflict)
        assert result.parent == conflict.parent


class TestIsClassifiable:
    def test_execution_log_conflict_is_classifiable(self, tmp_path: Path):
        conflict = (
            tmp_path
            / "Hermes"
            / "Execution Logs"
            / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        )
        assert is_classifiable(conflict, tmp_path) is True

    def test_backlog_conflict_is_not_classifiable(self, tmp_path: Path):
        conflict = (
            tmp_path
            / "Hermes"
            / "Execution Logs"
            / "Backlogs"
            / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        )
        assert is_classifiable(conflict, tmp_path) is False


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

        found = find_conflicts(tmp_path)
        assert len(found) == 1
        assert found[0].name == "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"

    def test_returns_empty_list_for_missing_vault_root(self, tmp_path: Path):
        assert find_conflicts(tmp_path / "does-not-exist") == []


class TestDescribeIntegration:
    def test_execution_log_real_loss_is_reported_with_missing_titles(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        (real / "Execution Log.md").write_text(_log(ENTRY_A, ENTRY_B))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        line = describe(conflict, tmp_path)
        assert "REAL LOSS" in line
        assert "Entry C (2026-09-03)" in line

    def test_backlog_conflict_stays_unclassified(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        (real / "Backlog.md").write_text(_log(ENTRY_A))
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        line = describe(conflict, tmp_path)
        assert line.strip() == str(conflict.relative_to(tmp_path))


class TestReconcile:
    def test_safe_duplicate_deletes_the_conflict_file_only(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        canonical.write_text(_log(ENTRY_A, ENTRY_B))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))

        result = reconcile(conflict, tmp_path)

        assert result is not None
        assert result.category == "safe_duplicate"
        assert not conflict.exists()
        assert canonical.read_text(encoding="utf-8") == _log(ENTRY_A, ENTRY_B)

    def test_real_loss_clean_restores_at_the_correct_position(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        # Matches the actual 2026-09-16 shape: shared prefix (A), then each
        # side grew a different entry independently.
        canonical.write_text(_log(ENTRY_A, ENTRY_B))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        result = reconcile(conflict, tmp_path)

        assert result is not None
        assert result.category == "real_loss_clean"
        assert not conflict.exists()
        restored = canonical.read_text(encoding="utf-8")
        # C must land between A and B, not appended after B, matching the
        # real chronological position it was actually written in.
        assert restored == _log(ENTRY_A, ENTRY_C, ENTRY_B)

    def test_real_loss_clean_matches_the_actual_2026_09_16_recovery(self, tmp_path: Path):
        """Reproduces the exact recovery performed by hand earlier the same
        day this mechanism was built: conflict has A then the missing
        entry; canonical (post-incident, pre-fix) has A then two later,
        unrelated entries that were written after the loss. Reconcile must
        insert the missing entry between them, not disturb the rest."""
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        later_entry_1 = "### PR merged (2026-09-15)\n\n- merged three PRs\n"
        later_entry_2 = "### APT check (2026-09-16)\n\n- nothing to install\n"
        canonical.write_text(_log(ENTRY_A, later_entry_1, later_entry_2))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_B))  # ENTRY_B stands in for the lost entry

        result = reconcile(conflict, tmp_path)

        assert result.category == "real_loss_clean"
        restored = canonical.read_text(encoding="utf-8")
        assert restored == _log(ENTRY_A, ENTRY_B, later_entry_1, later_entry_2)

    def test_ambiguous_conflict_is_left_completely_untouched(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        original_canonical_text = _log(ENTRY_A)
        canonical.write_text(original_canonical_text)
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        original_conflict_text = _log("### Entry A (2026-09-01)\n\n- a DIFFERENT thing\n")
        conflict.write_text(original_conflict_text)

        result = reconcile(conflict, tmp_path)

        assert result is None
        assert conflict.exists()
        assert conflict.read_text(encoding="utf-8") == original_conflict_text
        assert canonical.read_text(encoding="utf-8") == original_canonical_text

    def test_non_classifiable_file_is_left_untouched(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        canonical = real / "Backlog.md"
        canonical.write_text(_log(ENTRY_A))
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        result = reconcile(conflict, tmp_path)

        assert result is None
        assert conflict.exists()

    def test_missing_counterpart_is_left_untouched(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))

        result = reconcile(conflict, tmp_path)

        assert result is None
        assert conflict.exists()


class TestReconcileAll:
    def test_noop_when_nothing_pending(self, tmp_path: Path):
        (tmp_path / "Hermes").mkdir()
        assert reconcile_all(tmp_path) == []

    def test_reconciles_every_classifiable_conflict_found(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        canonical = real / "Execution Log.md"
        canonical.write_text(_log(ENTRY_A))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))  # safe duplicate

        results = reconcile_all(tmp_path)

        assert len(results) == 1
        assert results[0].category == "safe_duplicate"
        assert not conflict.exists()

    def test_does_not_touch_backlog_conflicts(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        canonical = real / "Backlog.md"
        canonical.write_text(_log(ENTRY_A))
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        results = reconcile_all(tmp_path)

        assert results == []
        assert conflict.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
