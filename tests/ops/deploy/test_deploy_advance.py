"""Tests for ops/deploy/deploy-advance.sh (MOORING Step 1.3).

Runs the real script as a subprocess against constructed fixture git repos (a
fake "live checkout", "origin", and "upstream") rather than importing
anything -- there is nothing to import, this is a deployment shell script.
Covers every refusal path (dirty tree, wrong branch, non-fast-forward,
unconfigured/placeholder approved-base, unreachable target, an undeclared
extra commit on top of the approved base) plus one full happy-path run
(a declared residual patch on top of the approved base) through pre-flight,
pull, and receipt-writing. The gateway-restart step itself is not exercised
here -- it needs a real systemd user session -- so the happy-path test only
asserts the script gets as far as scheduling it (the restart script's own
logic is a close derivative of scripts/sync-fork-restart-async.sh, already
proven in production).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "ops" / "deploy"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )


def _write_fixture_package(repo: Path) -> None:
    """A minimal, real, installable Python package: no external deps, so
    `uv sync --frozen` is instant and offline. Exercises deploy-advance.sh's
    own logic end to end without needing the real hermes-agent codebase."""
    (repo / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (repo / "hermes_cli" / "__init__.py").write_text("")
    (repo / "hermes_cli" / "main.py").write_text(
        "import sys\n"
        "def main():\n"
        "    if sys.argv[1:] == ['plugins', 'list']:\n"
        "        print('Loaded 0 plugins.')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_trivial.py").write_text("def test_ok():\n    assert True\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fixture-pkg"\nversion = "0.0.1"\nrequires-python = ">=3.11"\n'
        '\n[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        '\n[tool.setuptools]\npy-modules = []\n'
        '\n[tool.setuptools.packages.find]\ninclude = ["hermes_cli*"]\n'
    )


@pytest.fixture
def topology(tmp_path):
    """Builds upstream/origin/repo_dir with a shared root commit "C_root" and
    a second commit "C_base" (the real fixture package), tagged v1.0.0 in
    upstream. origin's `deploy` branch sits at C_base; repo_dir starts reset
    back to C_root, so advancing to C_base is a real one-commit fast-forward
    that lands exactly on the approved base (zero residual patches needed).

    Returns a dict of paths plus helpers to add extra commits on top of
    C_base in origin's deploy branch, for the reachability test cases.
    """
    upstream = tmp_path / "upstream_repo"
    origin = tmp_path / "origin_repo"
    repo_dir = tmp_path / "repo_dir"

    upstream.mkdir()
    _git(upstream, "init", "--quiet", "-b", "main")
    (upstream / "README.md").write_text("root\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "C_root")
    c_root = _git(upstream, "rev-parse", "HEAD").stdout.strip()

    _write_fixture_package(upstream)
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "--quiet", "-m", "C_base")
    c_base = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    _git(upstream, "tag", "v1.0.0")

    subprocess.run(["git", "clone", "--quiet", str(upstream), str(origin)], check=True, capture_output=True, text=True)
    _git(origin, "checkout", "--quiet", "-b", "deploy", c_base)

    # origin's active branch is already "deploy" (checked out above), so the
    # clone lands on branch "deploy" by default -- no explicit checkout -b
    # needed (and attempting one collides with the branch clone already made).
    subprocess.run(["git", "clone", "--quiet", str(origin), str(repo_dir)], check=True, capture_output=True, text=True)
    assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "deploy"
    _git(repo_dir, "reset", "--quiet", "--hard", c_root)
    _git(repo_dir, "remote", "add", "upstream", str(upstream))

    return {
        "upstream": upstream, "origin": origin, "repo_dir": repo_dir,
        "c_root": c_root, "c_base": c_base,
    }


def _run(topology_paths, tmp_path, *, approved_base="v1.0.0", extra_lines=(), env_overrides=None):
    approved_base_file = tmp_path / "approved-base.txt"
    approved_base_file.write_text("\n".join([approved_base, *extra_lines]) + "\n")

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(exist_ok=True)

    env = {
        **os.environ,
        "DEPLOY_REPO_DIR": str(topology_paths["repo_dir"]),
        "DEPLOY_HERMES_HOME": str(hermes_home),
        "DEPLOY_UV_BIN": os.environ.get("DEPLOY_TEST_UV_BIN", "uv"),
        "DEPLOY_APPROVED_BASE_FILE": str(approved_base_file),
        "DEPLOY_PREFLIGHT_SCRATCH": str(tmp_path / "scratch"),
        "DEPLOY_OFFLINE_CHECKLIST_TESTS": "tests/test_trivial.py",
    }
    if env_overrides:
        env.update(env_overrides)

    script = _plugin_dir() / "deploy-advance.sh"
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, timeout=120)


class TestRefusals:
    def test_dirty_tree_refused(self, topology, tmp_path):
        (topology["repo_dir"] / "scratch.txt").write_text("uncommitted\n")
        result = _run(topology, tmp_path)
        assert result.returncode != 0
        assert "uncommitted changes" in result.stdout

    def test_wrong_branch_refused(self, topology, tmp_path):
        _git(topology["repo_dir"], "checkout", "--quiet", "-b", "not-deploy")
        result = _run(topology, tmp_path)
        assert result.returncode != 0
        assert "not deploy" in result.stdout

    def test_nothing_to_do_is_silent_success(self, topology, tmp_path):
        _git(topology["repo_dir"], "reset", "--quiet", "--hard", "origin/deploy")
        result = _run(topology, tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_diverged_checkout_refused(self, topology, tmp_path):
        _git(topology["repo_dir"], "commit", "--quiet", "--allow-empty", "-m", "local divergence")
        result = _run(topology, tmp_path)
        assert result.returncode != 0
        assert "not an ancestor" in result.stdout

    def test_missing_approved_base_file_refused(self, topology, tmp_path):
        env = {"DEPLOY_APPROVED_BASE_FILE": str(tmp_path / "does-not-exist.txt")}
        result = _run(topology, tmp_path, env_overrides=env)
        assert result.returncode != 0
        assert "no approved-base file" in result.stdout

    def test_placeholder_approved_base_refused(self, topology, tmp_path):
        result = _run(topology, tmp_path, approved_base="REPLACE_ME_WITH_APPROVED_BASE_TAG_OR_SHA")
        assert result.returncode != 0
        assert "placeholder" in result.stdout

    def test_unresolvable_approved_base_refused(self, topology, tmp_path):
        result = _run(topology, tmp_path, approved_base="v9.9.9-does-not-exist")
        assert result.returncode != 0
        assert "does not resolve to a real commit" in result.stdout

    def test_target_not_descendant_of_approved_base_refused(self, topology, tmp_path):
        # Tag a commit that is NOT an ancestor of origin/deploy.
        _git(topology["upstream"], "checkout", "--quiet", topology["c_root"])
        _git(topology["upstream"], "checkout", "--quiet", "-b", "unrelated")
        (topology["upstream"] / "unrelated.txt").write_text("x\n")
        _git(topology["upstream"], "add", "-A")
        _git(topology["upstream"], "commit", "--quiet", "-m", "unrelated commit")
        _git(topology["upstream"], "tag", "v2.0.0-unrelated")
        result = _run(topology, tmp_path, approved_base="v2.0.0-unrelated")
        assert result.returncode != 0
        assert "not a descendant of the approved base" in result.stdout

    def test_undeclared_extra_commit_refused(self, topology, tmp_path):
        _git(topology["origin"], "checkout", "--quiet", "deploy")
        (topology["origin"] / "undeclared.txt").write_text("x\n")
        _git(topology["origin"], "add", "-A")
        _git(topology["origin"], "commit", "--quiet", "-m", "undeclared residual patch")
        result = _run(topology, tmp_path)  # no extra_lines declared
        assert result.returncode != 0
        assert "undeclared commit" in result.stdout


class TestHappyPath:
    def test_advance_to_approved_base_exactly(self, topology, tmp_path):
        """target_head == the approved base tag itself: zero residual patches
        needed, full pipeline (preflight -> pull -> receipt) runs end to end."""
        result = _run(topology, tmp_path)
        assert result.returncode == 0, result.stdout
        assert "advanced 1 commit" in result.stdout
        assert "gateway restart scheduled" in result.stdout

        repo_dir = topology["repo_dir"]
        assert _git(repo_dir, "rev-parse", "HEAD").stdout.strip() == topology["c_base"]

    def test_receipt_written_as_scheduled(self, topology, tmp_path):
        hermes_home = tmp_path / "hermes_home_receipt"
        hermes_home.mkdir()
        env = {"DEPLOY_HERMES_HOME": str(hermes_home)}
        result = _run(topology, tmp_path, env_overrides=env)
        assert result.returncode == 0, result.stdout
        receipt_path = hermes_home / "cron" / "deploy_advance_state.json"
        assert receipt_path.exists()
        receipt = json.loads(receipt_path.read_text())
        assert receipt["state"] == "scheduled"
        assert receipt["before_head"] == topology["c_root"]
        assert receipt["after_head"] == topology["c_base"]
        assert receipt["commit_count"] == 1

    def test_declared_residual_patch_allowed(self, topology, tmp_path):
        _git(topology["origin"], "checkout", "--quiet", "deploy")
        (topology["origin"] / "residual.txt").write_text("x\n")
        _git(topology["origin"], "add", "-A")
        _git(topology["origin"], "commit", "--quiet", "-m", "declared residual patch")
        residual_sha = _git(topology["origin"], "rev-parse", "HEAD").stdout.strip()
        result = _run(topology, tmp_path, extra_lines=(residual_sha,))
        assert result.returncode == 0, result.stdout
        assert "advanced 2 commit" in result.stdout
