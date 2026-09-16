"""Tests for the SYNCGUARD Step 3 reconcile hook added to
scripts/execution-log-rotate.py.

Scope: only the new behavior (self-healing a pending conflict for the
primary file before reading it). The pre-existing rotation/merge mechanics
had no test coverage before this change and adding a full suite for them is
out of scope here -- this covers exactly what this change added, end to
end, against a real invocation of main().
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "execution-log-rotate.py"
)

HEADER = (
    "# Execution Log\n\nSome preamble text.\n\n## PROTOCOL\n\nRules here.\n\n## Entries\n"
)


def _log(*entries: str) -> str:
    return HEADER + "\n" + "\n".join(entries)


ENTRY_A = "### Entry A (2026-09-01)\n\n- did a thing\n"
ENTRY_B = "### Entry B (2026-09-02)\n\n- did another thing\n"


def _load_module(base_dir: Path):
    """Fresh import with EXECUTION_LOG_BASE pointed at base_dir -- BASE is
    computed at module-import time from the env var, so it must be set
    before exec_module runs, and sys.modules cleared so a stale copy from
    an earlier test isn't reused."""
    os.environ["EXECUTION_LOG_BASE"] = str(base_dir)
    for stale in ("execution_log_rotate", "_vault_conflict_lib"):
        sys.modules.pop(stale, None)
    spec = importlib.util.spec_from_file_location("execution_log_rotate", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["execution_log_rotate"] = module
    spec.loader.exec_module(module)
    return module


class TestReconcileHook:
    def test_reconciles_a_real_loss_before_rotating(self, tmp_path: Path):
        # base_dir mirrors .../Hermes/Execution Logs; vault root is two
        # levels up, matching VAULT_ROOT = BASE.parent.parent.
        base_dir = tmp_path / "Hermes" / "Execution Logs"
        base_dir.mkdir(parents=True)
        primary = base_dir / "Execution Log.md"
        primary.write_text(_log(ENTRY_A))
        conflict = base_dir / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_B))

        module = _load_module(base_dir)
        rc = module.main()

        assert rc == 0
        assert not conflict.exists()
        # Rotation moved entries out and reset primary to header-only --
        # the reconciled entry (B) must be in the WEEKLY file, not lost.
        assert primary.read_text(encoding="utf-8").strip() == HEADER.strip()
        weekly_files = list((base_dir / "Weekly").glob("*.md"))
        assert len(weekly_files) == 1
        weekly_content = weekly_files[0].read_text(encoding="utf-8")
        assert "Entry A" in weekly_content
        assert "Entry B" in weekly_content

        event_log = (base_dir / "rotation-log.md").read_text(encoding="utf-8")
        assert "PRE-ROTATION RECONCILE" in event_log
        assert "restored" in event_log

    def test_removes_a_redundant_conflict_before_rotating(self, tmp_path: Path):
        base_dir = tmp_path / "Hermes" / "Execution Logs"
        base_dir.mkdir(parents=True)
        primary = base_dir / "Execution Log.md"
        primary.write_text(_log(ENTRY_A))
        conflict = base_dir / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A))  # fully redundant

        module = _load_module(base_dir)
        rc = module.main()

        assert rc == 0
        assert not conflict.exists()
        event_log = (base_dir / "rotation-log.md").read_text(encoding="utf-8")
        assert "PRE-ROTATION RECONCILE" in event_log
        assert "removed redundant" in event_log

    def test_ambiguous_conflict_does_not_block_rotation(self, tmp_path: Path):
        base_dir = tmp_path / "Hermes" / "Execution Logs"
        base_dir.mkdir(parents=True)
        primary = base_dir / "Execution Log.md"
        primary.write_text(_log(ENTRY_A))
        conflict = base_dir / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        edited_a = "### Entry A (2026-09-01)\n\n- a DIFFERENT thing\n"
        conflict.write_text(_log(edited_a))

        module = _load_module(base_dir)
        rc = module.main()

        assert rc == 0
        assert conflict.exists()  # left alone, never deleted or guessed at
        # Rotation still proceeded normally on canonical's own content.
        weekly_files = list((base_dir / "Weekly").glob("*.md"))
        assert len(weekly_files) == 1
        assert "Entry A" in weekly_files[0].read_text(encoding="utf-8")

    def test_no_op_when_nothing_pending(self, tmp_path: Path):
        base_dir = tmp_path / "Hermes" / "Execution Logs"
        base_dir.mkdir(parents=True)
        primary = base_dir / "Execution Log.md"
        primary.write_text(_log(ENTRY_A))

        module = _load_module(base_dir)
        rc = module.main()

        assert rc == 0
        event_log = (base_dir / "rotation-log.md").read_text(encoding="utf-8")
        assert "PRE-ROTATION RECONCILE" not in event_log


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
