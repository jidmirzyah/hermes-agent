"""jid-update-guard plugin — blocks agent tool calls and gateway commands that would mutate
Hermes's own live checkout, or trigger an update outside JID's approval flow.

L2 of the MOORING self-update-gate replacement (see the MOORING plan, Step 1.2). The fork's
existing in-code approval gate (``hermes update``'s own staging/approval flow) stays the primary
authorization surface and is untouched by this plugin; this is a second, deployment-layer
backstop that blocks the agent from mutating the checkout through paths the in-code gate does
not cover: raw git commands, package installs into the live venv, and file-tool writes under the
checkout path. It also intercepts a chat ``/update`` request with an explanatory refusal, since
that path has no in-code approval gate to fall back on.

L3 (drift detection, a separate cron job) is the intended backstop for whatever obfuscation slips
past this plugin's pattern matching -- by design this plugin does not need to be adversarial-proof,
only good enough to catch ordinary agent behavior (see the plan's L2/L3 split).
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# tool name -> path-bearing argument name, for file-mutating tools. Mirrors the
# plugins/security-guidance _TARGET_TOOLS convention (that plugin scans content at these paths;
# this one blocks outright when the path itself resolves under the live checkout).
_FILE_WRITE_TOOLS: Dict[str, str] = {
    "write_file": "path",
    "patch": "path",
    "skill_manage": "file_path",
}

_REFUSAL_MESSAGE = (
    "Updates are approved through a JID-merged PR to the `deploy` branch, not from chat "
    "(MOORING L1). Ask JID to merge the next `stable` tag when he's ready."
)


def _live_checkout_root() -> Optional[Path]:
    """The checkout backing this running process, if any.

    Reuses ``tools.self_repo_guard``'s own detection rather than hardcoding a path: this plugin
    only runs loaded into the live gateway process, so ``get_running_source_root()`` naturally
    resolves to the live checkout at runtime (and, in tests, to wherever this clone lives).
    """
    from tools.self_repo_guard import get_running_source_root
    return get_running_source_root()


def _resolve_under_root(path_str: str, cwd: Optional[str], root: Path) -> bool:
    if not path_str:
        return False
    base = Path(cwd) if cwd else Path.cwd()
    try:
        candidate = (base / Path(path_str).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _tokenize(command: str) -> list[str]:
    try:
        return [t.lower() for t in shlex.split(command, posix=True)]
    except ValueError:
        return command.lower().split()


def _looks_like_hermes_update(command: str) -> bool:
    """True for a command line that would run ``hermes update`` in any recognized invocation
    form (``hermes update``, ``hermes update --gateway``, ``python -m hermes_cli.main update``,
    an absolute ``.venv/bin/hermes update``).

    Deliberately simpler than ``gateway/status.py``'s spawn-intent matcher (no inline-source-spawn
    peeling): L3 is the backstop for obfuscated evasions, so this only needs to catch ordinary
    invocations, not be adversarial-proof (see module docstring).
    """
    tokens = _tokenize(command)
    if "update" not in tokens:
        return False
    joined = " ".join(tokens)
    basenames = [t.rsplit("/", 1)[-1] for t in tokens]
    return (
        any(b in ("hermes", "hermes.exe") for b in basenames)
        or "hermes_cli.main" in joined
        or "hermes_cli/main.py" in joined
    )


def _looks_like_env_install(command: str, root: Path) -> bool:
    """True for a package-install command (pip/uv) that names the live checkout's own venv or
    path explicitly (e.g. ``<root>/.venv/bin/pip install ...``, ``uv sync`` run with ``--python``
    pointed at it). Plain ``pip install <pkg>`` with no reference to the live root is left alone —
    that installs into whatever environment is already active, not necessarily this one.
    """
    tokens = _tokenize(command)
    if not tokens:
        return False
    basenames = [t.rsplit("/", 1)[-1] for t in tokens]
    is_pip = "pip" in basenames and "install" in tokens
    is_uv = "uv" in basenames and ("sync" in tokens or ("pip" in tokens and "install" in tokens))
    if not (is_pip or is_uv):
        return False
    return str(root).lower() in " ".join(tokens)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    root = _live_checkout_root()
    if root is None or not isinstance(args, dict):
        return None

    if tool_name == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        from tools.self_repo_guard import detect_self_repo_git_mutation
        cwd = args.get("workdir") or str(root)
        hit, msg = detect_self_repo_git_mutation(command, cwd, source_root=root)
        if hit:
            logger.warning("jid-update-guard: blocked self-repo git mutation: %s", command[:200])
            return {"action": "block", "message": msg}
        if _looks_like_hermes_update(command):
            logger.warning("jid-update-guard: blocked hermes update: %s", command[:200])
            return {
                "action": "block",
                "message": (
                    "Blocked: `hermes update` mutates Hermes's live checkout directly, bypassing "
                    "JID's deploy-branch approval (MOORING L1/L2). Updates land only via a "
                    "JID-merged PR to the `deploy` branch, applied by deploy-advance.sh."
                ),
            }
        if _looks_like_env_install(command, root):
            logger.warning("jid-update-guard: blocked env install: %s", command[:200])
            return {
                "action": "block",
                "message": (
                    f"Blocked: this would install packages into Hermes's live environment "
                    f"({root}). Use a separate clone with its own venv instead."
                ),
            }
        return None

    path_key = _FILE_WRITE_TOOLS.get(tool_name)
    if path_key is None:
        return None
    path_val = args.get(path_key)
    if isinstance(path_val, str) and _resolve_under_root(path_val, args.get("workdir"), root):
        logger.warning("jid-update-guard: blocked live-checkout write: %s %s", tool_name, path_val)
        return {
            "action": "block",
            "message": (
                f"Blocked: this would write into Hermes's live checkout ({root}) directly. "
                "Work in a separate clone and open a PR instead."
            ),
        }
    return None


async def _on_pre_gateway_dispatch(
    event: Any = None, gateway: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    text = getattr(event, "text", None)
    if not isinstance(text, str) or not text.strip().lower().startswith("/update"):
        return None
    try:
        source = event.source
        await gateway.adapters[source.platform].send(
            source.chat_id, _REFUSAL_MESSAGE, reply_to=getattr(event, "message_id", None))
    except Exception:
        logger.exception("jid-update-guard: failed to send /update refusal reply")
    return {"action": "skip", "reason": "update-command-refused"}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
