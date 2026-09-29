"""Tests for the jid-update-guard plugin (plugins/jid-update-guard/).

Covers:
  * ``pre_tool_call`` — blocks git commands that mutate the live checkout (delegated to
    ``tools.self_repo_guard.detect_self_repo_git_mutation``), blocks recognized ``hermes update``
    invocation forms, blocks pip/uv installs naming the live checkout's venv, blocks direct
    file-tool writes under the checkout path — and leaves everything else (safe git subcommands,
    unrelated hermes subcommands, installs elsewhere, writes elsewhere) alone.
  * ``pre_gateway_dispatch`` — skips a chat ``/update`` request with a reply, ignores other text.

``_live_checkout_root()`` resolves via ``tools.self_repo_guard.get_running_source_root()``, which
walks up from wherever ``tools/self_repo_guard.py`` physically lives — in this test run that is
the repo root under test, so it stands in for "the live checkout" without any path mocking.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_plugin_init():
    """Import the plugin's __init__.py directly, matching plugins/security-guidance's own test
    convention (no PluginManager/ctx machinery needed to unit-test the hook functions)."""
    plugin_dir = _repo_root() / "plugins" / "jid-update-guard"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.jid_update_guard",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.jid_update_guard"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.jid_update_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_plugin_init()


@pytest.fixture
def root(mod) -> Path:
    r = mod._live_checkout_root()
    assert r is not None, "test must run from a git checkout for get_running_source_root() to resolve"
    return r


# ---------------------------------------------------------------------------
# pre_tool_call — git mutation (delegated to tools.self_repo_guard)
# ---------------------------------------------------------------------------

class TestGitMutation:
    def test_git_pull_in_live_checkout_blocked(self, mod, root):
        result = mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "git pull", "workdir": str(root)})
        assert result is not None and result["action"] == "block"
        assert result["message"]

    def test_git_reset_hard_in_live_checkout_blocked(self, mod, root):
        result = mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "git reset --hard origin/main", "workdir": str(root)})
        assert result is not None and result["action"] == "block"

    def test_git_status_in_live_checkout_allowed(self, mod, root):
        assert mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "git status", "workdir": str(root)}) is None

    def test_git_pull_elsewhere_allowed(self, mod, root, tmp_path):
        assert mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "git pull", "workdir": str(tmp_path)}) is None


# ---------------------------------------------------------------------------
# pre_tool_call — hermes update
# ---------------------------------------------------------------------------

class TestHermesUpdate:
    @pytest.mark.parametrize("command", [
        "hermes update",
        "hermes update --gateway",
        "python -m hermes_cli.main update",
        "/home/jiddy/.hermes/hermes-agent/.venv/bin/hermes update",
    ])
    def test_recognized_update_invocations_blocked(self, mod, root, command):
        result = mod._on_pre_tool_call(
            tool_name="terminal", args={"command": command, "workdir": str(root)})
        assert result is not None and result["action"] == "block"
        assert "hermes update" in result["message"].lower() or "deploy" in result["message"].lower()

    def test_unrelated_hermes_subcommand_allowed(self, mod, root):
        assert mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "hermes cron list", "workdir": str(root)}) is None

    def test_update_as_a_plain_word_elsewhere_allowed(self, mod, root):
        """'update' appearing outside a hermes invocation must not false-positive."""
        assert mod._on_pre_tool_call(
            tool_name="terminal",
            args={"command": "echo update the README please", "workdir": str(root)}) is None


# ---------------------------------------------------------------------------
# pre_tool_call — package installs into the live venv
# ---------------------------------------------------------------------------

class TestEnvInstall:
    def test_pip_install_naming_live_venv_blocked(self, mod, root):
        command = f"{root}/.venv/bin/pip install requests"
        result = mod._on_pre_tool_call(tool_name="terminal", args={"command": command})
        assert result is not None and result["action"] == "block"

    def test_uv_sync_naming_live_root_blocked(self, mod, root):
        command = f"uv sync --python {root}/.venv/bin/python"
        result = mod._on_pre_tool_call(tool_name="terminal", args={"command": command})
        assert result is not None and result["action"] == "block"

    def test_plain_pip_install_elsewhere_allowed(self, mod, root):
        assert mod._on_pre_tool_call(
            tool_name="terminal", args={"command": "pip install requests"}) is None


# ---------------------------------------------------------------------------
# pre_tool_call — file-tool writes under the live checkout
# ---------------------------------------------------------------------------

class TestFileWrites:
    def test_write_file_under_live_checkout_blocked(self, mod, root):
        result = mod._on_pre_tool_call(
            tool_name="write_file", args={"path": str(root / "hermes_logging.py"), "content": "x"})
        assert result is not None and result["action"] == "block"

    def test_patch_under_live_checkout_via_relative_path_blocked(self, mod, root):
        result = mod._on_pre_tool_call(
            tool_name="patch",
            args={"path": "hermes_logging.py", "workdir": str(root), "new_string": "x"})
        assert result is not None and result["action"] == "block"

    def test_write_file_elsewhere_allowed(self, mod, tmp_path):
        assert mod._on_pre_tool_call(
            tool_name="write_file", args={"path": str(tmp_path / "scratch.py"), "content": "x"}) is None

    def test_unrelated_tool_ignored(self, mod, root):
        assert mod._on_pre_tool_call(tool_name="read_file", args={"path": str(root / "README.md")}) is None


# ---------------------------------------------------------------------------
# pre_gateway_dispatch — /update refusal
# ---------------------------------------------------------------------------

class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to))


class _FakeSource:
    platform = "telegram"
    chat_id = "12345"


class _FakeEvent:
    def __init__(self, text):
        self.text = text
        self.source = _FakeSource()
        self.message_id = "msg-1"


class TestPluginManifest:
    def test_manifest_declares_registered_hooks(self, mod):
        """Manifest metadata must use the field consumed by plugin discovery."""
        import hermes_yaml as yaml

        plugin_dir = _repo_root() / "plugins" / "jid-update-guard"
        manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
        registered = []

        class HookContext:
            def register_hook(self, name, _callback):
                registered.append(name)

        mod.register(HookContext())
        assert set(manifest["provides_hooks"]) == set(registered)


class TestGatewayDispatch:
    def test_update_command_skipped_with_reply(self, mod):
        adapter = _FakeAdapter()
        gateway = types.SimpleNamespace(adapters={"telegram": adapter})
        result = asyncio.run(mod._on_pre_gateway_dispatch(event=_FakeEvent("/update"), gateway=gateway))
        assert result == {"action": "skip", "reason": "update-command-refused"}
        assert len(adapter.sent) == 1
        assert adapter.sent[0][0] == "12345"
        assert "deploy" in adapter.sent[0][1].lower()

    def test_update_command_case_and_whitespace_insensitive(self, mod):
        adapter = _FakeAdapter()
        gateway = types.SimpleNamespace(adapters={"telegram": adapter})
        result = asyncio.run(mod._on_pre_gateway_dispatch(event=_FakeEvent("  /UPDATE now"), gateway=gateway))
        assert result is not None and result["action"] == "skip"

    def test_other_text_passes_through(self, mod):
        gateway = types.SimpleNamespace(adapters={"telegram": _FakeAdapter()})
        result = asyncio.run(
            mod._on_pre_gateway_dispatch(event=_FakeEvent("hey, what's the weather?"), gateway=gateway))
        assert result is None

    def test_adapter_send_failure_still_skips(self, mod):
        """A broken adapter must not turn the refusal into a fall-through to real update handling."""
        class _BrokenAdapter:
            async def send(self, *a, **kw):
                raise RuntimeError("boom")

        gateway = types.SimpleNamespace(adapters={"telegram": _BrokenAdapter()})
        result = asyncio.run(mod._on_pre_gateway_dispatch(event=_FakeEvent("/update"), gateway=gateway))
        assert result == {"action": "skip", "reason": "update-command-refused"}
