"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5


def _make_local_git_side_effect(*, origin_url, results):
    """Build a subprocess.run side_effect for _check_via_local_git tests.

    ``results`` maps a command tuple to a (returncode, stdout) pair.
    ``git remote get-url origin`` is always wired to ``origin_url``.
    """
    table = dict(results)
    table[("git", "remote", "get-url", "origin")] = (0, origin_url)

    def side_effect(cmd, **kwargs):
        key = tuple(cmd)
        if key not in table:
            raise AssertionError(f"unexpected command: {cmd}")
        rc, stdout = table[key]
        return MagicMock(returncode=rc, stdout=stdout)

    return side_effect


class TestCheckViaLocalGitCompareRef:
    """Regression coverage for the banner/--check disagreement: the "N
    commits behind" badge must prefer upstream/<branch> the same way
    ``hermes update --check`` does, not silently compare against the fork's
    own origin/<branch>."""

    def test_prefers_upstream_when_available(self, tmp_path):
        from hermes_cli.banner import _check_via_local_git

        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)

        side_effect = _make_local_git_side_effect(
            origin_url="https://github.com/example/hermes-agent.git\n",
            results={
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
                ("git", "remote", "get-url", "upstream"): (0, "https://github.com/NousResearch/hermes-agent.git\n"),
                ("git", "rev-parse", "--is-shallow-repository"): (0, "false\n"),
                ("git", "fetch", "upstream", "main", "--quiet"): (0, ""),
                ("git", "rev-list", "--count", "HEAD..upstream/main"): (0, "19879\n"),
            },
        )

        with patch("hermes_cli.banner.subprocess.run", side_effect=side_effect):
            behind = _check_via_local_git(repo_dir)

        assert behind == 19879

    def test_falls_back_to_origin_when_no_upstream_remote(self, tmp_path):
        """No upstream remote configured — behavior matches the pre-fix
        origin-only comparison, just reached via the shared helper now."""
        from hermes_cli.banner import _check_via_local_git

        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)

        side_effect = _make_local_git_side_effect(
            origin_url="https://github.com/example/hermes-agent.git\n",
            results={
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
                ("git", "remote", "get-url", "upstream"): (1, ""),
                ("git", "rev-parse", "--is-shallow-repository"): (0, "false\n"),
                ("git", "fetch", "origin", "main", "--quiet"): (0, ""),
                ("git", "rev-list", "--count", "HEAD..origin/main"): (0, "0\n"),
            },
        )

        with patch("hermes_cli.banner.subprocess.run", side_effect=side_effect):
            behind = _check_via_local_git(repo_dir)

        assert behind == 0

    def test_non_main_branch_never_fetches_upstream(self, tmp_path):
        """On a non-default branch, upstream is never consulted — a fork's
        feature branch has no upstream counterpart."""
        from hermes_cli.banner import _check_via_local_git

        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)

        side_effect = _make_local_git_side_effect(
            origin_url="https://github.com/example/hermes-agent.git\n",
            results={
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "bb/gui\n"),
                ("git", "rev-parse", "--is-shallow-repository"): (0, "false\n"),
                ("git", "fetch", "origin", "bb/gui", "--quiet"): (0, ""),
                ("git", "rev-list", "--count", "HEAD..origin/bb/gui"): (0, "4\n"),
            },
        )

        with patch("hermes_cli.banner.subprocess.run", side_effect=side_effect):
            behind = _check_via_local_git(repo_dir)

        assert behind == 4

    def test_shallow_clone_uses_resolved_ref_presence_only(self, tmp_path):
        """A shallow clone must never fabricate a commit count — even when
        the resolved compare ref is upstream/<branch> instead of origin."""
        from hermes_cli.banner import UPDATE_AVAILABLE_NO_COUNT, _check_via_local_git

        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)

        side_effect = _make_local_git_side_effect(
            origin_url="https://github.com/example/hermes-agent.git\n",
            results={
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
                ("git", "remote", "get-url", "upstream"): (0, "https://github.com/NousResearch/hermes-agent.git\n"),
                ("git", "rev-parse", "--is-shallow-repository"): (0, "true\n"),
                ("git", "fetch", "upstream", "main", "--depth", "1", "--quiet"): (0, ""),
                ("git", "rev-parse", "HEAD"): (0, "abc123\n"),
                ("git", "rev-parse", "FETCH_HEAD"): (0, "def456\n"),
            },
        )

        with patch("hermes_cli.banner.subprocess.run", side_effect=side_effect):
            behind = _check_via_local_git(repo_dir)

        # Different SHAs -> behind, no fabricated count.
        assert behind == UPDATE_AVAILABLE_NO_COUNT


def test_banner_badge_check_does_not_wait_on_network():
    """build_welcome_banner must never block on the prefetch thread — the
    badge call site should poll with timeout=0, not wait up to 0.5s."""
    import inspect
    from hermes_cli import banner

    source = inspect.getsource(banner.build_welcome_banner)
    assert "get_update_result(timeout=0)" in source
    assert "get_update_result(timeout=0.5)" not in source
def test_check_via_local_git_fetch_failure_returns_none(tmp_path, monkeypatch):
    """When git fetch fails and the stale origin/main ref is not ahead,
    _check_via_local_git must return None (#82166).

    A stale tracking ref cannot prove *currentness* (rev-list 0 just means
    the ref hasn't caught up), so returning None is the honest inconclusive
    result — and the caller must not cache it as "up to date".
    """
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Simulate a non-shallow, non-SSH-remote checkout
    def mock_git_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    # Fetch fails (returncode != 0); stale rev-list reports 0 behind
    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    stale_zero_proc = MagicMock()
    stale_zero_proc.returncode = 0
    stale_zero_proc.stdout = "0"

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            return failed_proc
        if args[:2] == ["git", "rev-list"]:
            return stale_zero_proc
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result is None, (
        "Fetch failure with stale 0-behind must return None, not 'up to date'"
    )


def test_check_via_local_git_fetch_failure_keeps_positive_stale_count(tmp_path, monkeypatch):
    """A failed fetch must preserve sound evidence: if the stale origin/main
    ref already shows HEAD behind, that positive count is still an update
    signal and must be returned (review #92578)."""
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def mock_git_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    stale_behind_proc = MagicMock()
    stale_behind_proc.returncode = 0
    stale_behind_proc.stdout = "5"

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            return failed_proc
        if args[:2] == ["git", "rev-list"]:
            return stale_behind_proc
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result == 5, "Stale positive behind-count must be preserved on fetch failure"


def test_check_via_local_git_fetch_failure_rev_list_error_returns_none(tmp_path, monkeypatch):
    """If the stale rev-list itself fails, the check stays inconclusive (None)."""
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def mock_git_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    bad_rev_list = MagicMock()
    bad_rev_list.returncode = 128
    bad_rev_list.stdout = ""
    bad_rev_list.stderr = "fatal: ambiguous argument 'HEAD..origin/main'"

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            return failed_proc
        if args[:2] == ["git", "rev-list"]:
            return bad_rev_list
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result is None


def test_check_for_updates_does_not_cache_none(tmp_path, monkeypatch):
    """check_for_updates must not cache None results so a transient fetch
    failure doesn't suppress retries for the full 6-hour cache window (#82166).

    Instead of mocking the full Path resolution chain, we verify the cache-write
    guard directly: call check_for_updates with a mocked _check_via_local_git
    that returns None, and confirm no cache file is created.
    """
    import hermes_cli.banner as banner

    cache_file = tmp_path / ".update_check"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_REVISION", raising=False)

    # Create a fake repo dir so the .git check passes
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Mock the internal functions to force the local-git path returning None
    monkeypatch.setattr(banner, "_check_via_local_git", lambda rd: None)
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda root: "git"
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_project_root", lambda: repo_dir
    )

    # Patch __file__ resolution by monkeypatching the module's Path calls.
    # check_for_updates does: Path(__file__).parent.parent.resolve()
    # We intercept by making the resolve() return our fake repo_dir.
    original_init = Path.__init__

    def patched_path_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

    # Simpler: just patch the get_hermes_home and the repo_dir resolution
    # by making check_for_updates find our fake repo via hermes_home fallback.
    # The code checks Path(__file__).parent.parent/.git first, then falls
    # back to hermes_home / "hermes-agent". We ensure the fallback hits.
    # To do this, we make Path(__file__).parent.parent.resolve() return
    # a path without .git, so it falls through to hermes_home / "hermes-agent".
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        s = str(self)
        if "banner.py" in s or s.endswith("hermes_cli"):
            # Return a path that has no .git, forcing the fallback
            return tmp_path / "no-git-here"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = banner.check_for_updates()
    assert result is None

    # The cache file must NOT have been written with a None result
    assert not cache_file.exists(), "None result must not be cached"



