"""Tests for interrupted-install self-heal (the ``.update-incomplete`` marker).

Covers the breadcrumb lifecycle. The launch-time recovery itself is now owned
by PM: ``hermes_cli/_early_recovery.recover_if_needed`` asks ``pm.recovery``
to restore dependencies and clears markers only on success — see
tests/hermes_cli/test_early_recovery.py and tests/pm/test_recovery.py.
"""

from __future__ import annotations

import hermes_cli.main as m


def test_marker_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    marker = m._update_marker_path()
    assert marker == tmp_path / ".update-incomplete"
    assert not marker.exists()

    m._write_update_incomplete_marker()
    assert marker.exists()
    body = marker.read_text()
    assert "started=" in body
    assert "pid=" in body

    m._clear_update_incomplete_marker()
    assert not marker.exists()
