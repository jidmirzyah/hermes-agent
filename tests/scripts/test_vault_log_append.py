"""Tests for scripts/vault-log-append.py.

Covers the actual SYNCGUARD Step 3 prevention mechanism: appending through
this script must reconcile any pending conflict for the target file first,
then append -- so a third write can never again silently build on top of an
already-lossy file the way the 2026-09-16 incident did.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "vault-log-append.py"

spec = importlib.util.spec_from_file_location("vault_log_append", SCRIPT_PATH)
vault_log_append = importlib.util.module_from_spec(spec)
sys.modules["vault_log_append"] = vault_log_append
spec.loader.exec_module(vault_log_append)

append_entry = vault_log_append.append_entry

HEADER = (
    "# Execution Log\n\nSome preamble text.\n\n## PROTOCOL\n\nRules here.\n\n## Entries\n"
)


def _log(*entries: str) -> str:
    return HEADER + "\n" + "\n".join(entries)


ENTRY_A = "### Entry A (2026-09-01)\n\n- did a thing\n"
ENTRY_B = "### Entry B (2026-09-02)\n\n- did another thing\n"
NEW_ENTRY = "### New Entry (2026-09-17)\n\n- the entry being appended right now\n"


class TestAppendEntry:
    def test_appends_to_a_clean_file_with_no_pending_conflict(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"
        target.write_text(_log(ENTRY_A))

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert messages == []
        assert target.read_text(encoding="utf-8") == _log(ENTRY_A, NEW_ENTRY)

    def test_reproduces_the_actual_2026_09_16_recovery_automatically(self, tmp_path: Path):
        """The exact scenario this whole mechanism exists to prevent: a
        conflict is sitting unreconciled (canonical already lost an entry
        to it), and a new write is about to land on top of it. Appending
        through this script must self-heal the loss first, THEN add the
        new entry after everything -- reproducing, mechanically, what was
        done by hand earlier the same day this was built."""
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"
        # canonical is missing ENTRY_B (the "lost" entry)
        target.write_text(_log(ENTRY_A))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_B))

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert not conflict.exists()
        assert any("restored" in m for m in messages)
        final = target.read_text(encoding="utf-8")
        assert final == _log(ENTRY_A, ENTRY_B, NEW_ENTRY)

    def test_removes_a_redundant_conflict_before_appending(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"
        target.write_text(_log(ENTRY_A))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))  # fully redundant

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert not conflict.exists()
        assert any("removed redundant" in m for m in messages)
        assert target.read_text(encoding="utf-8") == _log(ENTRY_A, NEW_ENTRY)

    def test_ambiguous_conflict_is_left_alone_but_append_still_proceeds(self, tmp_path: Path):
        """An ambiguous conflict must never block a legitimate append (this
        script is not a gate) -- but must also never be silently
        discarded. It's reported and left on disk for manual reconciliation."""
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"
        target.write_text(_log(ENTRY_A))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        edited_a = "### Entry A (2026-09-01)\n\n- a DIFFERENT thing\n"
        conflict.write_text(_log(edited_a))

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert conflict.exists()  # never deleted
        assert any("ambiguous" in m for m in messages)
        final = target.read_text(encoding="utf-8")
        # The new entry was still appended -- the ambiguous conflict didn't
        # block real work, and canonical's own content is untouched aside
        # from the append itself.
        assert final == _log(ENTRY_A, NEW_ENTRY)

    def test_only_reconciles_conflicts_for_the_target_file_not_others(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"
        target.write_text(_log(ENTRY_A))
        backlog_dir = real / "Backlogs"
        backlog_dir.mkdir()
        (backlog_dir / "Backlog.md").write_text(_log(ENTRY_A))
        unrelated_conflict = backlog_dir / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        unrelated_conflict.write_text(_log(ENTRY_A, ENTRY_B))

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert messages == []
        assert unrelated_conflict.exists()  # untouched -- not this file's conflict

    def test_creates_the_file_if_it_does_not_exist_yet(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        target = real / "Execution Log.md"

        messages = append_entry(target, tmp_path, NEW_ENTRY)

        assert messages == []
        assert target.read_text(encoding="utf-8") == NEW_ENTRY


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
