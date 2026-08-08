"""Regression guard: Hermes must never pipe a stored sudo password again.

F1 (hardening/f1-f9-preharden) deliberately removed the mechanism that let
Hermes read ``SUDO_PASSWORD`` (from the environment or a secret store) and
pipe it into ``sudo -S``, along with the whole interactive-prompt/session-
cache apparatus around it (``set_sudo_password_callback``,
``_prompt_for_sudo_password``, ``_get_cached_sudo_password``, etc.). The
threat model: a compromised or prompt-injected agent could run arbitrary
sudo commands without ever needing to ask a human, if a password were
sitting anywhere reachable from agent-spawned processes. This is also a
standing operator decision, not just a code default — sudo commands on
this deployment are run manually over SSH, never through Hermes' own
prompt (see infra notes, 2026-08-01).

An upstream NousResearch commit reintroduced this exact mechanism (a
"sudo over messaging" feature: read ``SUDO_PASSWORD`` from config/env,
construct ``sudo -S -p ''`` with the password piped via stdin, plus a full
callback/cache/prompt apparatus wired into ``cli.py``,
``hermes_cli/cli_commands_mixin.py``, ``tools/thread_context.py``, and
``tui_gateway/server.py``) — discovered and deliberately excluded during
the 2026-08-08 upstream-sync merge. This module exists so a *future*
upstream sync that reintroduces the same mechanism fails loudly and
specifically here, instead of silently landing and needing another manual
full re-diff of ``tools/terminal_tool.py``.

Each check runs twice: source-level (the mechanism doesn't exist in the
code at all) and behavioral (calling the real functions with
``SUDO_PASSWORD`` set produces no password-piping, no exemption). Source
checks catch a reintroduction before it can even execute; behavioral
checks catch a same-named-but-differently-shaped reintroduction that a
literal string search might miss.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from tools import terminal_tool
from tools.approval import _check_sudo_stdin_guard

# Function names that made up upstream's reintroduced sudo-password
# callback/cache/prompt apparatus. None of these should exist in
# tools.terminal_tool — their presence means the mechanism came back,
# even if nothing currently calls them yet.
_REMOVED_SUDO_PASSWORD_CALLBACK_NAMES = (
    "set_sudo_password_callback",
    "_get_sudo_password_callback",
    "_get_sudo_password_cache_scope",
    "_get_cached_sudo_password",
    "_set_cached_sudo_password",
    "_reset_cached_sudo_passwords",
    "_invalidate_cached_sudo_on_auth_failure",
    "_prompt_for_sudo_password",
    "_rewrite_real_sudo_invocations",
    "_count_real_sudo_invocations",
)


def test_terminal_tool_has_no_sudo_password_callback_apparatus():
    """None of upstream's callback/cache/prompt functions exist in this module.

    Their reappearance — even unused, even before anything calls them —
    is the reintroduction of the removed mechanism. Catch it here, not
    after something starts wiring them up.
    """
    present = [
        name
        for name in _REMOVED_SUDO_PASSWORD_CALLBACK_NAMES
        if hasattr(terminal_tool, name)
    ]
    assert not present, (
        "tools/terminal_tool.py has regained sudo-password-callback "
        f"function(s) that F1 removed: {present}. This is the exact "
        "mechanism (stored/cached sudo password, agent-triggerable "
        "without a human in the loop) that was deliberately excluded "
        "from every upstream sync — see this file's module docstring."
    )


def _executable_source(func) -> str:
    """Source of *func* with its docstring stripped, so a static check only
    sees real code — the docstrings here deliberately explain the removed
    mechanism by name (for future readers), which would otherwise false-
    positive a naive substring search."""
    tree = ast.parse(inspect.getsource(func))
    fn_node = tree.body[0]
    body = fn_node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_transform_sudo_command_source_has_no_sudo_password_reference():
    """Static check: SUDO_PASSWORD does not appear anywhere in the executable body.

    A literal substring check on the real code (docstring excluded — it
    deliberately explains the removed mechanism by name), not just one
    behavioral sample — catches the reintroduction even if it reads the
    value under a different code path than the one the behavioral test
    below exercises.
    """
    code = _executable_source(terminal_tool._transform_sudo_command)
    assert "SUDO_PASSWORD" not in code, (
        "_transform_sudo_command's code references SUDO_PASSWORD again — "
        "this function must remain a pass-through that never reads a "
        "stored/env sudo password. See this file's module docstring."
    )
    assert "get_secret" not in code, (
        "_transform_sudo_command's code reads from a secret store again — "
        "it must remain a pass-through with no credential lookup of any kind."
    )


def test_transform_sudo_command_ignores_sudo_password_env(monkeypatch):
    """Behavioral check: even with SUDO_PASSWORD set, nothing gets piped.

    This is the actual runtime guarantee, not just a source-text check —
    confirms the current implementation, not just its wording.
    """
    monkeypatch.setenv("SUDO_PASSWORD", "definitely-not-used")
    command = "sudo apt-get update"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)
    assert transformed == command, (
        "_transform_sudo_command rewrote the command when SUDO_PASSWORD "
        "was set — it must always return the command unchanged."
    )
    assert sudo_stdin is None, (
        "_transform_sudo_command produced stdin to pipe into the process "
        "when SUDO_PASSWORD was set — no password may ever be piped."
    )


def test_check_sudo_stdin_guard_source_has_no_exemption():
    """Static check: no SUDO_PASSWORD-conditioned exemption in the guard's
    executable body (docstring excluded — it deliberately explains the
    removed exemption by name)."""
    code = _executable_source(_check_sudo_stdin_guard)
    assert "SUDO_PASSWORD" not in code, (
        "_check_sudo_stdin_guard's code references SUDO_PASSWORD — this "
        "guard must be unconditional, with no exemption for any "
        "environment/config state. See this file's module docstring."
    )


@pytest.mark.parametrize("sudo_password_configured", [True, False])
def test_check_sudo_stdin_guard_blocks_regardless_of_sudo_password(
    monkeypatch, sudo_password_configured
):
    """Behavioral check: sudo -S is blocked identically whether or not
    SUDO_PASSWORD is set — there is no legitimate exemption case."""
    if sudo_password_configured:
        monkeypatch.setenv("SUDO_PASSWORD", "irrelevant-must-not-matter")
    else:
        monkeypatch.delenv("SUDO_PASSWORD", raising=False)

    is_blocked, description = _check_sudo_stdin_guard("sudo -S apt-get update")
    assert is_blocked is True, (
        "_check_sudo_stdin_guard failed to block 'sudo -S' with "
        f"SUDO_PASSWORD {'set' if sudo_password_configured else 'unset'} — "
        "this guard must be unconditional."
    )
    assert description


# --- Combined/separate short-flag coverage (2026-08-08) --------------------
#
# sudo follows standard getopt short-flag combining: -S (read password from
# stdin) is a boolean flag with no argument, so it can be bundled with other
# boolean short flags in either order and either combined into one token or
# given as separate arguments. A regex anchored to a literal, standalone
# "-S" as the very first argument misses all of these — verified against
# the actual sudo binary during the 2026-08-08 upstream-sync merge:
# "sudo -Sk -l" and "sudo -S -k -l" both prompt for a stdin password
# identically. This is a live bypass of the guard's unconditional-block
# guarantee, not a hypothetical — these parametrized cases pin the fix so
# a future regex change can't reopen it silently.
_SUDO_STDIN_SHOULD_BLOCK = [
    ("sudo -S apt-get update", "bare -S, start of string"),
    ("true; sudo -S whoami", "bare -S after ;"),
    ("true && sudo -S whoami", "bare -S after &&"),
    ("true || sudo -S whoami", "bare -S after ||"),
    ("echo $(sudo -S whoami)", "bare -S after $("),
    ("sudo -Sk whoami", "combined -S -k, S first"),
    ("sudo -nS whoami", "combined -n -S, S last"),
    ("sudo -kS whoami", "combined -k -S, S last"),
    ("sudo -SkE whoami", "combined -S -k -E, S first"),
    ("sudo -S -k whoami", "separate flags, S first"),
    ("sudo -k -S whoami", "separate flags, S second (not first)"),
    ("sudo -n -S whoami", "separate flags, S second (not first)"),
    ("sudo -n -k -S whoami", "three separate flag groups, S last"),
]

_SUDO_STDIN_SHOULD_NOT_BLOCK = [
    ("grep -n 'sudo -S' README.md", "mention inside a quoted grep pattern"),
    ("echo 'you should never run sudo -S in scripts'", "mention inside echoed prose"),
    ("printf '%s\\n' 'sudo -S is dangerous'", "mention inside printf string"),
    ("rg --line-number 'sudo -S' .", "mention inside rg pattern"),
    ("sudo -k whoami", "single flag, no S at all"),
    ("sudo -n whoami", "single flag, no S at all"),
    ("sudo -k -n whoami", "two flags, neither contains S"),
    ("sudo whoami", "plain sudo, no flags"),
]


@pytest.mark.parametrize("command,label", _SUDO_STDIN_SHOULD_BLOCK)
def test_check_sudo_stdin_guard_catches_combined_and_reordered_s_flags(command, label):
    """Every real stdin-password invocation blocks — combined short flags
    (-Sk, -kS, -SkE, ...) and S given as a later, separate flag (-k -S),
    not just a bare leading -S."""
    is_blocked, description = _check_sudo_stdin_guard(command)
    assert is_blocked is True, (
        f"_check_sudo_stdin_guard failed to block a real stdin-password "
        f"invocation ({label}): {command!r}. This is a live bypass of the "
        "unconditional-block guarantee — see this section's module comment."
    )
    assert description


@pytest.mark.parametrize("command,label", _SUDO_STDIN_SHOULD_NOT_BLOCK)
def test_check_sudo_stdin_guard_does_not_false_positive_on_flags_or_mentions(command, label):
    """Widening the guard to catch combined/reordered -S flags must not
    start blocking flags that don't contain S, or text that merely
    mentions "sudo -S" without it being a real command start."""
    is_blocked, _description = _check_sudo_stdin_guard(command)
    assert is_blocked is False, (
        f"_check_sudo_stdin_guard incorrectly blocked a safe command "
        f"({label}): {command!r}. The widened combined-flag pattern must "
        "not introduce new false positives."
    )
