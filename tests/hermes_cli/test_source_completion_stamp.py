"""A source checkout publishes identity only after successful completion."""

import os
from pathlib import Path
import subprocess

from hermes_cli.source_completion import complete_source_checkout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Hermes Test")
    git("config", "user.email", "hermes@example.invalid")
    (root / "tracked").write_text("release\n", encoding="utf-8")
    git("add", "tracked")
    git("commit", "-qm", "release")
    git("tag", "v0.21.4")
    return root


def _completion_dependencies(monkeypatch, maintenance):
    monkeypatch.setattr("hermes_cli.venv_sync.publish_launchers", lambda root: None)
    monkeypatch.setattr("hermes_cli.source_build.build_update_products", lambda root, *, desktop: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._run_post_update_maintenance", maintenance)


def test_successful_source_completion_writes_checkout_identity(tmp_path, monkeypatch):
    root = _repo(tmp_path)

    def maintenance(**_kwargs):
        assert not (root / "install-stamp.json").exists()
        return True

    _completion_dependencies(monkeypatch, maintenance)

    assert complete_source_checkout(root, desktop=False, assume_yes=True)
    assert (root / "install-stamp.json").is_file()


def test_failed_source_completion_does_not_publish_identity(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _completion_dependencies(monkeypatch, lambda **_kwargs: False)

    assert not complete_source_checkout(root, desktop=False, assume_yes=True)
    assert not (root / "install-stamp.json").exists()
