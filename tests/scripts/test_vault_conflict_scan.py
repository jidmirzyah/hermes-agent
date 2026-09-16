"""Tests for scripts/vault-conflict-scan.py.

The classification/reconcile logic itself lives in `_vault_conflict_lib.py`
now (SYNCGUARD Step 3) and is tested in `test_vault_conflict_lib.py`. This
file covers only what's specific to this script: that it correctly
re-exports `find_conflicts`/`describe` from the shared library, and that
`main()` produces the right end-to-end output (silent on nothing found,
one-line-per-conflict, classified for Execution Log.md, unclassified for
everything else) -- it never writes or deletes anything itself.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "vault-conflict-scan.py"

spec = importlib.util.spec_from_file_location("vault_conflict_scan", SCRIPT_PATH)
vault_conflict_scan = importlib.util.module_from_spec(spec)
sys.modules["vault_conflict_scan"] = vault_conflict_scan
spec.loader.exec_module(vault_conflict_scan)


HEADER = (
    "# Execution Log\n\nSome preamble text.\n\n## PROTOCOL\n\nRules here.\n\n## Entries\n"
)


def _log(*entries: str) -> str:
    return HEADER + "\n" + "\n".join(entries)


ENTRY_A = "### Entry A (2026-09-01)\n\n- did a thing\n"
ENTRY_C = "### Entry C (2026-09-03)\n\n- a third thing\n"


class TestReExports:
    # Each script that loads _vault_conflict_lib.py via spec_from_file_location
    # gets its own independent module object -- fine in production (every
    # script always runs as its own standalone process), but it means an
    # identity check (`is`) between two separately-loaded test harnesses in
    # the same pytest process is inherently fragile and not what actually
    # matters. What matters is that the re-export behaves identically.

    def test_find_conflicts_behaves_like_the_shared_library_function(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text("x")

        assert vault_conflict_scan.find_conflicts(tmp_path) == [conflict]

    def test_describe_behaves_like_the_shared_library_function(self, tmp_path: Path):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text("x")

        assert vault_conflict_scan.describe(conflict, tmp_path).strip() == str(
            conflict.relative_to(tmp_path)
        )


class TestMainEndToEnd:
    def test_silent_when_nothing_found(self, tmp_path, capsys):
        (tmp_path / "Hermes").mkdir()
        rc = _run_main(vault_conflict_scan, tmp_path)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""

    def test_reports_real_loss_for_execution_log_conflict(self, tmp_path, capsys):
        real = tmp_path / "Hermes" / "Execution Logs"
        real.mkdir(parents=True)
        (real / "Execution Log.md").write_text(_log(ENTRY_A))
        conflict = real / "Execution Log.sync-conflict-20260916-094549-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        rc = _run_main(vault_conflict_scan, tmp_path)
        captured = capsys.readouterr()
        assert rc == 0
        assert "1 unresolved sync-conflict file" in captured.out
        assert "REAL LOSS" in captured.out
        assert "Entry C (2026-09-03)" in captured.out
        # Never writes or deletes anything.
        assert conflict.exists()
        assert (real / "Execution Log.md").read_text(encoding="utf-8") == _log(ENTRY_A)

    def test_stays_unclassified_for_backlog_conflict(self, tmp_path, capsys):
        real = tmp_path / "Hermes" / "Execution Logs" / "Backlogs"
        real.mkdir(parents=True)
        (real / "Backlog.md").write_text(_log(ENTRY_A))
        conflict = real / "Backlog.sync-conflict-20260831-232717-KXKW46Z.md"
        conflict.write_text(_log(ENTRY_A, ENTRY_C))

        rc = _run_main(vault_conflict_scan, tmp_path)
        captured = capsys.readouterr()
        assert rc == 0
        assert "REAL LOSS" not in captured.out
        assert "safe" not in captured.out


def _run_main(module, vault_root: Path) -> int:
    import sys as _sys

    original_argv = _sys.argv
    _sys.argv = ["vault-conflict-scan.py", "--vault-root", str(vault_root)]
    try:
        return module.main()
    finally:
        _sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
