"""Tests for the jid-family-gateway plugin (F1/F2/F3).

F1: injects the verified sender ID for ordinary platforms, correctly
skips Slack (the one genuinely-shared-session platform in this deployment
-- see the plugin's own module docstring for why), and never crashes or
injects when there's no sender_id at all (e.g. a CLI/local session with no
gateway source).

F2/F3: pre_gateway_dispatch gating. Both features require (a) a genuine
quote-reply carrying a live pending-decision ref tag, confirmed by
check_reply_for_pending_ref, AND (b) the plugin's OWN authorization check
to pass -- pre_gateway_dispatch fires before the gateway's normal auth
gate, so this plugin cannot rely on already being authorized the way the
core implementation it replaces does. Every positive-path test has a
matching unauthorized-sender negative test, per the plan's explicit
requirement to add unauthorized-sender cases to every feature.
"""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_plugin_init():
    plugin_dir = _repo_root() / "plugins" / "jid-family-gateway"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.jid_family_gateway",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.jid_family_gateway"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.jid_family_gateway"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_plugin_init()


class TestF1VerifiedSenderId:
    def test_telegram_dm_injects_verified_id(self, mod):
        result = mod._on_pre_llm_call(sender_id="5542989100", platform="telegram", is_first_turn=True)
        assert result is not None
        assert "5542989100" in result["context"]
        assert "Verified platform user ID" in result["context"]

    def test_discord_injects_verified_id(self, mod):
        result = mod._on_pre_llm_call(sender_id="U0BLH3KCWTW", platform="discord")
        assert result is not None
        assert "U0BLH3KCWTW" in result["context"]

    def test_signal_injects_verified_id_no_platform_special_casing(self, mod):
        """Must generalize to any platform with a sender_id, not just the ones named above."""
        result = mod._on_pre_llm_call(sender_id="+15559998888", platform="signal")
        assert result is not None
        assert "+15559998888" in result["context"]

    def test_slack_is_excluded_the_one_shared_platform(self, mod):
        result = mod._on_pre_llm_call(sender_id="U0BLH3KCWTW", platform="slack")
        assert result is None

    def test_slack_excluded_case_insensitive(self, mod):
        result = mod._on_pre_llm_call(sender_id="U0BLH3KCWTW", platform="Slack")
        assert result is None

    def test_no_sender_id_injects_nothing(self, mod):
        result = mod._on_pre_llm_call(sender_id="", platform="telegram")
        assert result is None

    def test_no_sender_id_at_all_does_not_crash(self, mod):
        """CLI/local sessions have no gateway source at all -- must not raise."""
        result = mod._on_pre_llm_call(platform="cli")
        assert result is None

    def test_extra_undocumented_kwargs_are_tolerated(self, mod):
        """Real call site passes session_id, task_id, turn_id, user_message,
        conversation_history, model, parent_session_id too -- must not choke on them."""
        result = mod._on_pre_llm_call(
            sender_id="123", platform="telegram", session_id="s1", task_id="t1",
            turn_id="tu1", user_message="hi", conversation_history=[], model="x",
            parent_session_id="", is_first_turn=False,
        )
        assert result is not None

    def test_redact_pii_suppresses_injection(self, mod, monkeypatch):
        """Mirrors the real core fix's redact_pii suppression (test_session.py's
        test_redact_pii_suppresses_verified_id), ported via a direct config read
        since pre_llm_call's kwargs don't carry this flag. Inert in JID's real
        deployment today (redact_pii is off), but must not silently regress if
        it's ever turned on."""
        import gateway.run as gr

        monkeypatch.setattr(gr, "_load_gateway_config", lambda: {"privacy": {"redact_pii": True}})
        result = mod._on_pre_llm_call(sender_id="5542989100", platform="telegram")
        assert result is None

    def test_redact_pii_disabled_still_injects(self, mod, monkeypatch):
        import gateway.run as gr

        monkeypatch.setattr(gr, "_load_gateway_config", lambda: {"privacy": {"redact_pii": False}})
        result = mod._on_pre_llm_call(sender_id="5542989100", platform="telegram")
        assert result is not None

    def test_redact_pii_read_failure_fails_open_matching_core_behavior(self, mod, monkeypatch):
        """Matches the core caller's own `with suppress(Exception): _redact_pii = False`
        fallback (gateway/run_turn.py's _hmwa_prepare_turn) exactly -- a broken
        config read must not silently block every platform's identity injection."""
        import gateway.run as gr

        def _boom():
            raise RuntimeError("config disk read failed")

        monkeypatch.setattr(gr, "_load_gateway_config", _boom)
        result = mod._on_pre_llm_call(sender_id="5542989100", platform="telegram")
        assert result is not None


def _fake_gateway(*, authorized: bool) -> MagicMock:
    gw = MagicMock()
    gw._is_user_authorized.return_value = authorized
    gw._handle_oauth_reauth_reply = AsyncMock()
    return gw


class TestPreGatewayDispatchGating:
    """Cases where routing must not even attempt the tag/pending check."""

    @pytest.mark.asyncio
    async def test_no_reply_to_text_returns_none(self, mod):
        event = SimpleNamespace(text="hi", reply_to_text=None, reply_to_message_id="5", source=object())
        gateway = _fake_gateway(authorized=True)
        result = await mod._on_pre_gateway_dispatch(event, gateway)
        assert result is None
        gateway._is_user_authorized.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_reply_to_message_id_returns_none(self, mod):
        event = SimpleNamespace(
            text="hi", reply_to_text="[ref:upstream_fix:abc]", reply_to_message_id=None, source=object(),
        )
        gateway = _fake_gateway(authorized=True)
        result = await mod._on_pre_gateway_dispatch(event, gateway)
        assert result is None
        gateway._is_user_authorized.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tag_found_returns_none_without_checking_auth(self, mod, monkeypatch):
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        monkeypatch.setattr(
            gr, "check_reply_for_pending_ref",
            lambda t: ReplyRefCheck(tag_found=False, pending_exists=False),
        )
        event = SimpleNamespace(
            text="ordinary message", reply_to_text="just chat, no tag", reply_to_message_id="5", source=object(),
        )
        gateway = _fake_gateway(authorized=True)
        result = await mod._on_pre_gateway_dispatch(event, gateway)
        assert result is None
        gateway._is_user_authorized.assert_not_called()

    @pytest.mark.asyncio
    async def test_tag_found_but_no_pending_record_returns_none(self, mod, monkeypatch):
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        monkeypatch.setattr(
            gr, "check_reply_for_pending_ref",
            lambda t: ReplyRefCheck(tag_found=True, pending_exists=False, subsystem="upstream_fix", pending_id="deadbeef"),
        )
        event = SimpleNamespace(
            text="1", reply_to_text="[ref:upstream_fix:deadbeef]", reply_to_message_id="5", source=object(),
        )
        gateway = _fake_gateway(authorized=True)
        result = await mod._on_pre_gateway_dispatch(event, gateway)
        assert result is None
        gateway._is_user_authorized.assert_not_called()


class TestF2OauthReauthRouting:
    @pytest.mark.asyncio
    async def test_authorized_oauth_reply_delegates_to_core_handler_and_skips(self, mod, monkeypatch):
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="oauth_reauth", pending_id="abc123")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="12345", platform="telegram")
        event = SimpleNamespace(
            text="4/0AVGzR1abc", reply_to_text="[ref:oauth_reauth:abc123]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=True)

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        gateway._is_user_authorized.assert_called_once_with(source)
        gateway._handle_oauth_reauth_reply.assert_awaited_once_with(event, source, ref_check)
        assert result == {"action": "skip", "reason": "oauth-reauth-handled"}

    @pytest.mark.asyncio
    async def test_unauthorized_oauth_reply_is_left_completely_alone(self, mod, monkeypatch):
        """The security-critical case: pre_gateway_dispatch runs before auth, so an
        unauthorized sender replying to a real pending oauth_reauth reminder must
        never trigger the token-exchange handler."""
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="oauth_reauth", pending_id="abc123")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="attacker", platform="telegram")
        event = SimpleNamespace(
            text="4/0AVGzR1abc", reply_to_text="[ref:oauth_reauth:abc123]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=False)

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result is None
        gateway._handle_oauth_reauth_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorization_check_raising_fails_closed(self, mod, monkeypatch):
        """A broken auth check must never fail open into handling a sensitive
        token exchange."""
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="oauth_reauth", pending_id="abc123")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="12345", platform="telegram")
        event = SimpleNamespace(
            text="code", reply_to_text="[ref:oauth_reauth:abc123]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=True)
        gateway._is_user_authorized.side_effect = RuntimeError("boom")

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result is None
        gateway._handle_oauth_reauth_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_core_handler_exception_is_caught_and_still_skips(self, mod, monkeypatch):
        """If the real core handler blows up, this plugin must not crash gateway
        dispatch -- it already committed to handling this message (an authorized,
        confirmed-pending oauth reply), so falling through to the agent would be
        worse than a logged, swallowed failure."""
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="oauth_reauth", pending_id="abc123")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="12345", platform="telegram")
        event = SimpleNamespace(
            text="code", reply_to_text="[ref:oauth_reauth:abc123]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=True)
        gateway._handle_oauth_reauth_reply = AsyncMock(side_effect=RuntimeError("subprocess exploded"))

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result == {"action": "skip", "reason": "oauth-reauth-handled"}


class TestF3RefTagRouting:
    @pytest.mark.asyncio
    async def test_authorized_reply_to_other_subsystem_gets_rewritten(self, mod, monkeypatch):
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="upstream_fix", pending_id="28e9858f")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="12345", platform="telegram")
        event = SimpleNamespace(
            text="1", reply_to_text="[ref:upstream_fix:28e9858f]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=True)

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result is not None
        assert result["action"] == "rewrite"
        assert "upstream_fix" in result["text"]
        assert "28e9858f" in result["text"]
        assert result["text"].endswith("1")  # original message text preserved verbatim at the end
        gateway._handle_oauth_reauth_reply.assert_not_awaited()  # only the oauth branch calls this

    @pytest.mark.asyncio
    async def test_unauthorized_reply_to_other_subsystem_is_left_alone(self, mod, monkeypatch):
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="upstream_fix", pending_id="28e9858f")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="attacker", platform="telegram")
        event = SimpleNamespace(
            text="1", reply_to_text="[ref:upstream_fix:28e9858f]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=False)

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result is None

    @pytest.mark.asyncio
    async def test_upstream_pr_fix_subsystem_also_routes_not_just_upstream_fix(self, mod, monkeypatch):
        """Must generalize to any subsystem name, not special-case just one."""
        import gateway.run as gr
        from gateway.run import ReplyRefCheck

        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="upstream_pr_fix", pending_id="f00dcafe")
        monkeypatch.setattr(gr, "check_reply_for_pending_ref", lambda t: ref_check)

        source = SimpleNamespace(user_id="12345", platform="telegram")
        event = SimpleNamespace(
            text="apply a,c", reply_to_text="[ref:upstream_pr_fix:f00dcafe]", reply_to_message_id="9", source=source,
        )
        gateway = _fake_gateway(authorized=True)

        result = await mod._on_pre_gateway_dispatch(event, gateway)

        assert result["action"] == "rewrite"
        assert "upstream_pr_fix" in result["text"]
        assert "apply a,c" in result["text"]


class TestPluginManifest:
    def test_manifest_declares_registered_hooks(self, mod):
        import hermes_yaml as yaml

        plugin_dir = _repo_root() / "plugins" / "jid-family-gateway"
        manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
        registered = []

        class HookContext:
            def register_hook(self, name, _callback):
                registered.append(name)

        mod.register(HookContext())
        assert set(manifest["provides_hooks"]) == set(registered)
