"""Post-PM installer stages use the real installation-bound publication."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from tests.installation_launcher_fixture import publish_fixture_launcher

if os.name == 'posix':
    import fcntl
    import pty
    import termios

ROOT = Path(__file__).resolve().parents[2]

def _with_controlling_terminal(slave: int):
    """preexec_fn: make ``slave`` this child's controlling terminal, so /dev/tty opens."""
    def attach() -> None:
        os.setsid()  # windows-footgun: ok (posix-only test)
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    return attach


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage, expected', [('setup', ['setup']), ('gateway', ['gateway', 'install'])])
def test_installer_post_pm_stages(tmp_path: Path, stage: str, expected: list[str]) -> None:
    install = tmp_path / 'source tree'
    calls = tmp_path / 'calls.json'
    publish_fixture_launcher(install, "import json, os, sys\nfrom pathlib import Path\ndef main():\n    Path(os.environ['CALLS']).write_text(json.dumps(sys.argv[1:])); return int(os.environ['STAGE_EXIT'])\n")
    command = ['bash', '-c', 'source "$1" --manifest >/dev/null; INSTALL_DIR="$2"; NON_INTERACTIVE=false; "stage_$3"', 'test', str(ROOT / 'scripts/install.sh'), str(install), stage]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'), 'HERMES_RUNTIME_DIR': str(tmp_path / 'store'), 'CALLS': str(calls)}
    # `curl | bash` and Docker builds have no terminal: the interactive stage is skipped, not failed.
    result = subprocess.run(command, cwd=tmp_path, env={**env, 'STAGE_EXIT': '9'}, capture_output=True, text=True,
                            encoding='utf-8', timeout=20, start_new_session=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()
    master, slave = pty.openpty()
    try:
        for code in [0, 9]:
            result = subprocess.run(command, cwd=tmp_path, env={**env, 'STAGE_EXIT': str(code)}, capture_output=True,
                                    text=True, encoding='utf-8', timeout=20, stdin=subprocess.DEVNULL, preexec_fn=_with_controlling_terminal(slave),
                                    pass_fds=(slave,))
            assert (result.returncode == 0) == (code == 0), result.stdout + result.stderr
            assert json.loads(calls.read_text()) == expected
    finally:
        os.close(master)
        os.close(slave)
    assert not (install / 'venv').exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage, include_desktop, expected_desktop', [
    ('products', False, False), ('products', True, True), ('desktop', False, True),
])
def test_products_and_desktop_stages_share_the_completion_tail(tmp_path: Path, stage: str,
                                                                include_desktop: bool, expected_desktop: bool) -> None:
    """Both stages hand the checkout to hermes_cli/source_completion.py; --include-desktop
    (or the external `desktop` stage) only adds --desktop to that one call."""
    install = tmp_path / 'source tree'
    calls = tmp_path / 'calls.json'
    (install / 'hermes_cli').mkdir(parents=True)
    (install / 'hermes_cli' / 'source_completion.py').write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['CALLS']).write_text(json.dumps(sys.argv[1:]))\n", encoding='utf-8')
    flag = 'true' if include_desktop else 'false'
    command = ['bash', '-c',
               f'source "$1" --manifest >/dev/null; INSTALL_DIR="$2"; INCLUDE_DESKTOP={flag}; '
               'bootstrap_python() { boot_py="$PYTHON_FOR_TEST"; }; "stage_$3"',
               'test', str(ROOT / 'scripts/install.sh'), str(install), stage]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'),
           'HERMES_RUNTIME_DIR': str(tmp_path / 'store'), 'CALLS': str(calls), 'PYTHON_FOR_TEST': sys.executable}
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    argv = json.loads(calls.read_text())
    assert argv[:2] == ['--source', str(install)]
    assert ('--desktop' in argv) is expected_desktop


@pytest.mark.platforms("posix")
def test_desktop_flag_does_not_add_a_stage(tmp_path):
    """--include-desktop selects the desktop product inside `products`; the manifest is one ladder."""
    def manifest(*flags):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/install.sh"), "--manifest", *flags],
            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True,
        )
        return [row["name"] for row in json.loads(result.stdout)["stages"]]

    plain = manifest()
    assert "desktop" not in plain
    assert "products" in plain
    assert plain[-1] == "complete"
    assert manifest("--include-desktop") == plain
