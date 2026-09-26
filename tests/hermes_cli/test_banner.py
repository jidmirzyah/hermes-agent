"""Tests for banner toolset name normalization and skin color usage."""

from unittest.mock import patch

from rich.console import Console

import hermes_cli.banner as banner
import model_tools
import tools.mcp_tool_discovery


def test_banner_snapshot_accepts_bom_without_weakening_freshness(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(banner, "get_git_banner_state", lambda: None)
    monkeypatch.setattr(banner, "get_available_skills", lambda: {"notes": ["café"]})
    tools = [{"function": {"name": "read_file"}}]
    banner.save_banner_snapshot(tools, ["file"], {}, {"read_file": "file"})
    path = banner._banner_snapshot_path()
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    expected = json.loads(raw)
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8-sig")
    assert banner.load_banner_snapshot(["file"]) == expected
    assert banner.load_banner_snapshot(["web"]) is None
    (tmp_path / "config.yaml").write_text("display: {skin: mono}", encoding="utf-8")
    assert banner.load_banner_snapshot(["file"]) is None


def test_cprint_falls_back_to_plain_print_when_prompt_toolkit_has_no_console(capsys):
    with patch(
        "prompt_toolkit.print_formatted_text",
        side_effect=RuntimeError("no console screen buffer"),
    ):
        banner.cprint("fallback text")

    assert capsys.readouterr().out == "fallback text\n"
















def test_empty_model_shows_the_free_tier_route_when_it_carries_inference(tmp_path, monkeypatch):
    """The banner prints before credentials resolve, so ``model`` is empty on a fresh install. On the
    free tier the route is known locally (identity on disk + tier on): the banner shows its model.
    When nothing resolves the red "no model configured" line stays."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    import hermes_cli.anon_auth as anon_auth

    def render(carries: bool) -> str:
        with (
            patch.object(model_tools, "check_tool_availability", return_value=([], [])),
            patch.object(banner, "get_available_skills", return_value={}),
            patch.object(banner, "get_update_result", return_value=None),
            patch.object(tools.mcp_tool_discovery, "get_mcp_status", return_value=[]),
            patch.object(anon_auth, "guest_carries_inference", return_value=carries),
        ):
            console = Console(record=True, force_terminal=False, color_system=None, width=160)
            banner.build_welcome_banner(console=console, model="", cwd="/tmp/project", tools=[],
                                        enabled_toolsets=[], provider="auto")
        return console.export_text()

    assert "welcome" in render(True) and "no model configured" not in render(True)
    assert "no model configured" in render(False)


