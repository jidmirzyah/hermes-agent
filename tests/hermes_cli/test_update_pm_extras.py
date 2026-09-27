from __future__ import annotations

import pm

from hermes_cli import update_cmd_deps


def test_capture_active_lazy_features_reads_pm_selection(monkeypatch):
    monkeypatch.setattr(pm, "enabled_extras", lambda: ["all", "bedrock"])

    assert update_cmd_deps._capture_active_lazy_features() == ["all", "bedrock"]


def test_refresh_active_lazy_features_syncs_captured_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pm,
        "sync_venv",
        lambda extras, *, explicit: calls.append((extras, explicit)),
    )

    assert update_cmd_deps._refresh_active_lazy_features(
        ["legacy", "pip"], env={"VIRTUAL_ENV": "venv"}, features=["all", "bedrock"]
    )
    assert calls == [(["all", "bedrock"], True)]


def test_refresh_active_lazy_features_keeps_marker_on_failure(monkeypatch, capsys):
    def fail(extras, *, explicit):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(pm, "sync_venv", fail)

    assert not update_cmd_deps._refresh_active_lazy_features(features=["bedrock"])
    assert "resolver unavailable" in capsys.readouterr().out
