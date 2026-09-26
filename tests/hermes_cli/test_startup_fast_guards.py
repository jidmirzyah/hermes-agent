"""Guards for hermes_cli._startup_fast — the pre-import version fast path.

Two invariants, each of which has been broken before:

1. IMPORT WEIGHT: _startup_fast must stay stdlib-only. The whole point of
   the module is to run before main.py's heavy import wall; one careless
   ``from hermes_cli.config import ...`` silently makes `hermes --version`
   slow again for everyone (the regression would be invisible — everything
   still works, just 40x slower).

2. OUTPUT PARITY / LIVENESS: the fast path must actually produce version
   output and exit 0 in a real subprocess. This is the test that would have
   caught eb4040242, which changed the canonical version output to reference
   the PROJECT_ROOT module constant inside the fast function — a name that
   doesn't exist yet at the fast exit point — NameError-ing the fast path in
   production for weeks.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules that must NEVER be imported by the fast path. Each one either
# pulls YAML/argparse/logging config or is itself a god-module.
_FORBIDDEN_MODULES = (
    "hermes_cli.config",
    "hermes_cli.main",
    "hermes_yaml",
    "ruamel.yaml",
    "argparse",
    "cli",
    "run_agent",
    "model_tools",
    "httpx",
    "openai",
)


@pytest.mark.linux_only
def test_cli_starts_from_a_deleted_cwd(tmp_path):
    """A child spawned into a directory that was removed since (a cron delivery from a reaped
    kanban workspace) must still reach argv parsing: a relative ``sys.path`` entry made
    ``ensure_project_root_on_path`` die in ``realpath`` → ``getcwd`` (#102941)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    gone = tmp_path / "scratch"
    gone.mkdir()
    # The child needs the checkout on sys.path by absolute name; the cwd is exactly what is gone.
    env = {**os.environ, "HERMES_HOME": str(home), "TERMUX_VERSION": "",
           "PYTHONPATH": os.pathsep.join(p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH")) if p)}
    env.pop("HERMES_DEV", None)
    fd = os.open(gone, os.O_RDONLY)
    try:
        gone.rmdir()
        # ``cwd=`` of a removed path is refused by Popen, so start in the dead dir via a
        # preexec fchdir onto its still-open handle — the shape a reaped workspace leaves behind.
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "--version"],
            capture_output=True, text=True, timeout=60, env=env,
            cwd=REPO_ROOT, preexec_fn=lambda: os.fchdir(fd))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stderr
    assert "Hermes Agent v" in result.stdout
    assert "FileNotFoundError" not in result.stderr


def test_startup_fast_import_weight():
    """Importing _startup_fast must not drag in any heavy module."""
    probe = (
        "import sys, json\n"
        "import hermes_cli._startup_fast\n"
        "print(json.dumps(sorted(sys.modules.keys())))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    offenders = [m for m in _FORBIDDEN_MODULES if m in loaded]
    assert not offenders, (
        f"hermes_cli._startup_fast imported heavy modules: {offenders} — "
        "the fast path must stay stdlib-only (see module docstring)."
    )


def _run_version(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    env.pop("HERMES_DEV", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env=env,
    )


def test_fast_version_parity(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    result = _run_version({"HERMES_HOME": str(home)})
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for field in ("Hermes Agent v", "Install directory:", "Python:", "OpenAI SDK:"):
        assert field in out, f"fast --version output missing {field!r}:\n{out}"
    assert "Traceback" not in result.stderr


def test_fast_version_reports_install_method_stamp(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".install_method").write_text("git\n", encoding="utf-8")
    result = _run_version({"HERMES_HOME": str(home)})
    assert result.returncode == 0, result.stderr
    assert "Install method: git" in result.stdout


@pytest.mark.parametrize("argv", [["update"], ["pm", "doctor"], ["gateway", "status"]])
def test_termux_chat_shortcut_leaves_subcommands_to_dispatch(monkeypatch, argv):
    from hermes_cli import main

    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.delenv("HERMES_TERMUX_DISABLE_FAST_CLI", raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    assert main._try_termux_fast_cli_launch() is False
