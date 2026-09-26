"""Real Git local-work safety: caller divergence, restore faults and rescue retention."""
import contextlib
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from hermes_cli import main as hermes_main, update_cmd
from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401


@pytest.mark.parametrize('history,failure,keep', [
    ('ordinary', None, False), ('ordinary', None, True),
    ('ordinary', 'reset', False), ('ordinary', 'reset', True),
    ('orphan', None, False), ('orphan', 'reset', False),
    ('orphan', 'ref', False), ('orphan', 'head', False),
])
def test_update_preserves_local_work_and_rescues_orphan_before_reset(
    update_tree, monkeypatch, capsys, history, failure, keep,
):
    t = update_tree
    git(t.clone, 'checkout', '-q', 'main')
    if history == 'orphan':
        git(t.clone, 'checkout', '--orphan', 'fresh')
        git(t.clone, 'branch', '-D', 'main')
        git(t.clone, 'branch', '-m', 'main')
    (t.clone / 'local.txt').write_text('committed\n', encoding='utf-8')
    git(t.clone, 'add', 'local.txt')
    git(t.clone, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'local history')
    before = git(t.clone, 'rev-parse', 'HEAD')
    (t.clone / 'untracked.txt').write_text('local edit\n', encoding='utf-8')
    t.args.channel, t.args.keep_stash = 'main', keep
    monkeypatch.setattr(hermes_main, '_sync_with_upstream_if_needed', update_cmd._sync_with_upstream_if_needed)
    monkeypatch.setattr(update_cmd, '_UPDATE_CRITICAL_MODULES', ())
    original = subprocess.run
    resets = []

    def fault(command, *args, **kwargs):
        if 'reset' in command and '--hard' in command:
            refs = original(['git', 'for-each-ref', '--format=%(objectname)',
                             'refs/hermes-update-backups/'], cwd=t.clone,
                            check=True, capture_output=True, text=True).stdout.split()
            assert refs == ([before] if failure not in {'ref', 'head'} else [])
            resets.append(command)
        if ((failure == 'ref' and 'update-ref' in command and '-d' not in command)
                or (failure == 'reset' and 'reset' in command and '--hard' in command)):
            return subprocess.CompletedProcess(command, 128, stdout='', stderr='fixture I/O refusal')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', fault)
    if failure == 'head':
        monkeypatch.setattr(update_cmd, '_capture_head_sha', lambda *_: None)
    if failure == 'reset':
        with pytest.raises(SystemExit) as error:
            hermes_main.cmd_update(t.args)
        assert error.value.code == 1
        assert not t.requests
        assert git(t.clone, 'rev-parse', 'HEAD') == before
        assert not (t.clone / 'untracked.txt').exists()
    else:
        hermes_main.cmd_update(t.args)
        assert len(t.requests) == 1
        assert git(t.clone, 'rev-parse', 'HEAD') == t.newer
        assert (t.clone / 'untracked.txt').exists() is (not keep)
    assert len(resets) == 1
    stashes = git(t.clone, 'stash', 'list')
    assert bool(stashes) is (keep or failure == 'reset')
    if stashes:
        assert git(t.clone, 'show', 'stash@{0}^3:untracked.txt') == 'local edit'
    output = capsys.readouterr().out
    if failure == 'ref':
        assert 'backup write failed' in output and 'backed up current HEAD' not in output
    if failure == 'reset':
        assert 'preserved in stash' in output
    if failure not in {'ref', 'head'}:
        assert f'expires after {update_cmd._ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days' in output
        kind = 'orphan' if history == 'orphan' else 'diverged'
        assert f'refs/hermes-update-backups/{kind}-main-' in output
        if kind == 'diverged':
            assert 'commit(s) not on origin/main leave the branch' in output


# ---------------------------------------------------------------------------
# Non-main branch → auto-checkout main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fetch failure — friendly error messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reset --hard failure — don't attempt stash restore
# ---------------------------------------------------------------------------

def test_cmd_update_skips_stash_restore_when_reset_fails(monkeypatch, tmp_path, capsys):
    """When reset --hard fails, stash restore is skipped with a helpful message."""
    _setup_update_mocks(monkeypatch, tmp_path)
    # Re-enable stash so it actually returns a ref
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )

    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    # Bypass the update-approval staging gate — this test exercises the real
    # reset-failure/stash-restore-skip path, not the staging path.
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(), approved=True)

    # Stash restore should NOT have been called
    assert len(restore_calls) == 0

    out = capsys.readouterr().out
    assert "preserved in stash" in out


# ---------------------------------------------------------------------------
# #87694: orphan/unrelated-history divergence must be backed up to a rescue
# ref before `reset --hard` discards it (ordinary divergence is unaffected).
# ---------------------------------------------------------------------------

def test_cmd_update_orphan_history_backs_up_before_reset(monkeypatch, tmp_path, capsys):
    """No common ancestor with origin/<branch> → HEAD is parked behind a
    ``refs/hermes-update-backups/orphan-*`` ref before the reset proceeds."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    update_ref_calls = [c for c in recorded if "update-ref" in " ".join(str(x) for x in c)]
    assert len(update_ref_calls) == 1
    ref_name = update_ref_calls[0][update_ref_calls[0].index("update-ref") + 1]
    assert ref_name.startswith("refs/hermes-update-backups/orphan-main-")
    assert update_ref_calls[0][update_ref_calls[0].index("update-ref") + 2] == (
        "1111111111111111111111111111111111111beef"
    )
    # Ref name carries the pre-pull SHA, not just a second-resolution
    # timestamp, so two updates racing within the same second don't collide.
    assert ref_name.endswith("-111111111111")

    out = capsys.readouterr().out
    assert "orphan divergence" in out
    assert ref_name in out
    # The user is told the backup is temporary and when it expires.
    assert f"expires after {update_cmd._ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days" in out


def test_cmd_update_orphan_rescue_ref_write_failure_message_is_honest(monkeypatch, tmp_path, capsys):
    """When ``git update-ref`` fails, the printed message must not claim a
    backup exists — it should say the write was attempted and failed."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False, update_ref_fails=True,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    out = capsys.readouterr().out
    assert "orphan divergence" in out
    assert "backup write failed" in out
    assert "backed up current HEAD" not in out


def test_cmd_update_orphan_rescue_refs_pruned_beyond_keep_limit(monkeypatch, tmp_path, capsys):
    """Recent orphan rescue refs beyond the retention count are deleted so a
    repeatedly corrupted install doesn't pin unbounded objects against gc."""
    from datetime import datetime, timedelta, timezone

    git(tmp_path, 'init', '-q', '-b', 'main')
    git(tmp_path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'base')
    now = datetime.now(timezone.utc)
    total = update_cmd._ORPHAN_RESCUE_REFS_TO_KEEP + 2
    stale_refs = [
        "refs/hermes-update-backups/orphan-main-"
        f"{(now - timedelta(hours=total - i)).strftime('%Y%m%d-%H%M%S')}-abc"
        for i in range(total)
    ]
    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False, existing_rescue_refs=stale_refs,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    delete_calls = [
        c for c in recorded
        if "update-ref" in " ".join(str(x) for x in c) and "-d" in c
    ]
    assert len(delete_calls) == total - update_cmd._ORPHAN_RESCUE_REFS_TO_KEEP
    deleted_refs = {c[c.index("-d") + 1] for c in delete_calls}
    assert deleted_refs == set(stale_refs[: total - update_cmd._ORPHAN_RESCUE_REFS_TO_KEEP])


@pytest.mark.parametrize('fault,body,modules,message', [
    ('syntax', '<<<<<<< Updated upstream\nVALUE = 2\n', (), 'made the Hermes agent unexecutable'),
    ('import', "raise RuntimeError('restored local failure')\n", ('consumer',), 'restored local failure'),
    ('preexisting', 'VALUE = 2\n', ('first',), None),
    ('later', "raise RuntimeError('restored later failure')\n", ('first', 'consumer'), 'restored later failure'),
    ('exit', "raise SystemExit('restored exit')\n", ('first', 'consumer'), 'restored exit'),
    ('terminated', 'import os\nos._exit(7)\n', ('consumer',), 'exit code 7'),
    ('paths', 'VALUE = 2\n', (), 'restored Python source discovery'),
])
def test_restore_validates_real_stash_and_each_import(probe_root, monkeypatch, capsys, fault, body, modules, message):
    import hermes_cli.update_cmd_stash as stash

    tmp_path = probe_root

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=update_cmd._ORPHAN_RESCUE_REF_MAX_AGE_DAYS + 5)
    fresh = now - timedelta(days=1)
    expired_ref = (
        "refs/hermes-update-backups/orphan-main-"
        f"{old.strftime('%Y%m%d-%H%M%S')}-old1"
    )
    fresh_ref = (
        "refs/hermes-update-backups/orphan-main-"
        f"{fresh.strftime('%Y%m%d-%H%M%S')}-new1"
    )
    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False,
        existing_rescue_refs=[expired_ref, fresh_ref],
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    delete_calls = [
        c for c in recorded
        if "update-ref" in " ".join(str(x) for x in c) and "-d" in c
    ]
    deleted_refs = {c[c.index("-d") + 1] for c in delete_calls}
    assert deleted_refs == {expired_ref}


@pytest.mark.parametrize('error', [EOFError(), UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')])
def test_unreadable_stash_prompt_keeps_work(tmp_path, monkeypatch, capsys, error):
    def unreadable(*_):
        raise error
    monkeypatch.setattr('builtins.input', unreadable)
    assert hermes_main._restore_stashed_changes(['git'], tmp_path, 'stash@{0}', prompt_user=True) is False
    assert 'git stash apply stash@{0}' in capsys.readouterr().out


def test_cmd_update_ordinary_divergence_also_leaves_a_rescue_ref(monkeypatch, tmp_path, capsys):
    """Common ancestor still exists → rescue ref under the ``diverged-`` kind, no orphan messaging.

    Divergence on the target branch has two causes the checkout cannot tell apart: an upstream
    force-push, where nothing local is lost, and local commits on that branch, where the reset
    discards all of them. This case used to write no ref at all, which is correct only for the
    first cause.

    The #87694 size concern is specific to the orphan shape: there ``pre_pull_sha`` is an
    autostash orphan commit carrying a full working-tree snapshot, which can be multi-GB. Here it
    is ordinary branch history whose objects the reflog pins anyway for its expiry window, so the
    ref adds no meaningful footprint — and it expires under the same keep/age rules.
    """
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=True,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    update_ref_calls = [
        c for c in recorded
        if "update-ref" in " ".join(str(x) for x in c) and "-d" not in c
    ]
    assert len(update_ref_calls) == 1, "the discarded local history needs exactly one anchor"
    ref_name = str(update_ref_calls[0][2])
    assert ref_name.startswith("refs/hermes-update-backups/diverged-main-")

    out = capsys.readouterr().out
    assert "orphan divergence" not in out
    assert "Local history has diverged" in out
    assert "Fast-forward not possible (history diverged), resetting to match remote" in out


def test_cmd_update_orphan_rescue_ref_write_failure_is_non_fatal(monkeypatch, tmp_path, capsys):
    """#87694 stress test: ``git update-ref`` itself fails (disk full,
    permissions) while parking the orphan rescue ref. The backup attempt is
    best-effort — so the reset must still proceed and the update succeed."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False, update_ref_fails=True,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    update_ref_calls = [c for c in recorded if "update-ref" in " ".join(str(x) for x in c)]
    assert len(update_ref_calls) == 1

    reset_calls = [
        c for c in recorded if "reset" in " ".join(str(x) for x in c) and "--hard" in c
    ]
    assert len(reset_calls) == 1

    out = capsys.readouterr().out
    assert "orphan divergence" in out


def test_cmd_update_orphan_guard_skips_rescue_ref_when_pre_pull_sha_missing(
    monkeypatch, tmp_path, capsys
):
    """#87694 stress test: if capturing the pre-pull HEAD SHA itself fails
    (empty ``rev-parse HEAD`` output), the rescue-ref guard requires a
    truthy ``pre_pull_sha`` and must skip the backup rather than writing a
    ref pointing at nothing — the reset must still proceed without crashing.
    """
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False, pre_pull_sha_unavailable=True,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(), approved=True)

    update_ref_calls = [c for c in recorded if "update-ref" in " ".join(str(x) for x in c)]
    assert update_ref_calls == []

    out = capsys.readouterr().out
    assert "orphan divergence" not in out


def test_cmd_update_orphan_rescue_ref_persists_when_reset_fails(monkeypatch, tmp_path, capsys):
    """#87694 stress test: even when the subsequent ``reset --hard`` itself
    fails, the rescue ref must already have been written — the backup is
    not lost just because the overall update aborts."""
    _setup_update_mocks(monkeypatch, tmp_path)

    side_effect, recorded = _make_update_side_effect(
        ff_only_fails=True, merge_base_exists=False, reset_fails=True,
    )
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(SimpleNamespace(), approved=True)
    assert exc_info.value.code == 1

    update_ref_calls = [c for c in recorded if "update-ref" in " ".join(str(x) for x in c)]
    assert len(update_ref_calls) == 1

    out = capsys.readouterr().out
    assert "orphan divergence" in out
    assert "Failed to reset to origin/main" in out


# ---------------------------------------------------------------------------
# Non-interactive update.non_interactive_local_changes setting
# (chat app / gateway): "abort" refuses a dirty checkout before mutation,
# "discard" throws stashed changes away, and "stash" (default) restores them.
# Interactive terminal updates ignore the setting and always go through the
# restore path.
# ---------------------------------------------------------------------------



def _init_setting_test_repo(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("committed\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "init")
    return git


def test_abort_policy_preserves_dirty_checkout_without_creating_stash(tmp_path, capsys):
    git = _init_setting_test_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("local edit\n")
    (tmp_path / "untracked.txt").write_text("local untracked\n")
    before_head = git("rev-parse", "HEAD").stdout
    before_status = git("status", "--porcelain").stdout

    with pytest.raises(SystemExit, match="2"):
        hermes_main._abort_update_if_worktree_dirty(["git"], tmp_path)

    assert git("rev-parse", "HEAD").stdout == before_head
    assert git("status", "--porcelain").stdout == before_status
    assert git("stash", "list").stdout == ""
    assert (tmp_path / "tracked.txt").read_text() == "local edit\n"
    assert (tmp_path / "untracked.txt").read_text() == "local untracked\n"
    assert "unattended update aborted" in capsys.readouterr().out


def test_abort_policy_allows_clean_checkout(tmp_path):
    _init_setting_test_repo(tmp_path)
    hermes_main._abort_update_if_worktree_dirty(["git"], tmp_path)


def test_noninteractive_abort_runs_before_pre_update_backup(monkeypatch, tmp_path):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": "abort"}},
    )
    order = []

    def abort_guard(*args, **kwargs):
        order.append("abort_guard")
        raise SystemExit(2)

    monkeypatch.setattr(hermes_main, "_abort_update_if_worktree_dirty", abort_guard)
    monkeypatch.setattr(
        hermes_main,
        "_run_pre_update_backup",
        lambda *args, **kwargs: order.append("backup"),
    )
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)

    with pytest.raises(SystemExit, match="2"):
        hermes_main._cmd_update_impl(SimpleNamespace(yes=True), gateway_mode=False)

    assert order == ["abort_guard"]


def test_clean_noninteractive_abort_continues_into_normal_update_setup(monkeypatch, tmp_path):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": "abort"}},
    )
    order = []
    monkeypatch.setattr(
        hermes_main,
        "_abort_update_if_worktree_dirty",
        lambda *args, **kwargs: order.append("abort_guard"),
    )

    def stop_after_guard(*args, **kwargs):
        order.append("backup")
        raise RuntimeError("normal update setup reached")

    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", stop_after_guard)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)

    with pytest.raises(RuntimeError, match="normal update setup reached"):
        hermes_main._cmd_update_impl(SimpleNamespace(yes=True), gateway_mode=False)

    assert order == ["abort_guard", "backup"]


@pytest.mark.parametrize("mode", ["stash", "discard"])
def test_existing_noninteractive_modes_do_not_use_abort_guard(
    monkeypatch, tmp_path, mode
):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": mode}},
    )
    abort_calls = []
    monkeypatch.setattr(
        hermes_main,
        "_abort_update_if_worktree_dirty",
        lambda *args, **kwargs: abort_calls.append(1),
    )

    def stop_at_normal_setup(*args, **kwargs):
        raise RuntimeError("normal setup")

    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", stop_at_normal_setup)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)

    with pytest.raises(RuntimeError, match="normal setup"):
        hermes_main._cmd_update_impl(SimpleNamespace(yes=True), gateway_mode=False)

    assert abort_calls == []


def test_interactive_update_ignores_abort_policy(monkeypatch, tmp_path):
    import sys

    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": "abort"}},
    )
    abort_calls = []
    monkeypatch.setattr(
        hermes_main,
        "_abort_update_if_worktree_dirty",
        lambda *args, **kwargs: abort_calls.append(1),
    )

    def stop_at_normal_setup(*args, **kwargs):
        raise RuntimeError("normal setup")

    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", stop_at_normal_setup)
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)

    with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
        sys.stdout, "isatty", return_value=True
    ), pytest.raises(RuntimeError, match="normal setup"):
        hermes_main._cmd_update_impl(SimpleNamespace(yes=False), gateway_mode=False)

    assert abort_calls == []


# ---------------------------------------------------------------------------
# --keep-stash (desktop updater): stash for the update, never re-apply.
# ---------------------------------------------------------------------------

def _setup_keep_stash_test(monkeypatch, tmp_path):
    """Wiring for --keep-stash tests: stash returns a ref; restore, discard,
    and park are all recorded."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    park_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_park_stashed_changes",
        lambda *a, **kw: park_calls.append(a) or None,
    )
    # Keep the update flow away from the real gateway fleet on this machine —
    # a live gateway PID would trip the test-suite kill guard and turn the
    # run into exit 1 (gateway_fleet_restart_incomplete). All three gateway
    # discovery entry points must be mocked (see test_cmd_update.py's
    # _patch_gateway_discovery fixture) — find_gateway_pids alone still lets
    # find_profile_gateway_processes reach a real PID.
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **kw: [], raising=False
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.supports_systemd_services", lambda: False, raising=False
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes", lambda **kw: [], raising=False
    )
    return restore_calls, discard_calls, park_calls


def test_update_keep_stash_parks_instead_of_restoring(monkeypatch, tmp_path):
    """--keep-stash: after a successful update, the autostash is parked (left
    in git stash) — never re-applied, never discarded."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True), approved=True)

    assert len(park_calls) == 1
    assert park_calls[0][0] == "abc123deadbeef"
    assert restore_calls == []
    assert discard_calls == []


def test_update_without_keep_stash_still_restores(monkeypatch, tmp_path):
    """Regression guard: default behavior (no --keep-stash) is unchanged —
    the autostash is auto-restored under --yes."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=False), approved=True)

    assert restore_calls == [1]
    assert park_calls == []
    assert discard_calls == []


def test_update_keep_stash_failure_path_still_preserves(monkeypatch, tmp_path, capsys):
    """--keep-stash + failed update: neither restore nor park runs; the
    existing preserved-in-stash message fires (working tree unknown)."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True), approved=True)

    assert restore_calls == []
    assert park_calls == []
    assert discard_calls == []
    assert "preserved in stash" in capsys.readouterr().out
def test_unreadable_upstream_prompt_does_not_add_remote(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q', '-b', 'main')
    def unreadable(*_):
        raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')
    monkeypatch.setattr('builtins.input', unreadable)
    monkeypatch.setattr(update_cmd, '_should_skip_upstream_prompt', lambda *_: False)
    update_cmd._sync_with_upstream_if_needed(['git'], tmp_path)
    assert not git(tmp_path, 'remote')


def test_update_parser_accepts_keep_stash():
    """The flag parses and defaults off."""
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)

    args = parser.parse_args(["update", "--keep-stash"])
    assert args.keep_stash is True
    args = parser.parse_args(["update"])
    assert args.keep_stash is False



def test_bootstrap_marker_not_autostashed_by_update(tmp_path):
    """#38529: the Desktop bootstrap marker must be git-ignored so that
    ``hermes update``'s ``git stash push --include-untracked`` does not sweep it
    into an autostash on every run.

    Behavioral + hermetic: build a throwaway repo that adopts the project's real
    ``.gitignore`` (the contract under test), drop the marker, and confirm the
    same stash invocation the updater uses leaves it untouched.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo_gitignore = Path(hermes_main.__file__).resolve().parents[1] / ".gitignore"

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(repo_gitignore.read_text())
    (tmp_path / "tracked.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    marker = tmp_path / ".hermes-bootstrap-complete"
    marker.write_text("")

    # Exact flags used by hermes update (hermes_cli/main.py).
    git("stash", "push", "--include-untracked", "-m", "hermes-update-autostash")

    assert marker.exists(), (
        ".hermes-bootstrap-complete was swept into the update autostash — it must "
        "be listed in .gitignore so `git stash -u` skips it (#38529)."
    )
    # It must not even register as a dirty/untracked change.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".hermes-bootstrap-complete" not in status



def test_update_autostash_survives_undeletable_untracked_dir(tmp_path):
    """Behavioral E2E of the whole permission-denied class with real git:
    root-owned-style undeletable untracked dir → stash succeeds, update-style
    reset works, restore round-trips, nothing lost. (#70127 follow-up)"""
    import contextlib
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write bits")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    (tmp_path / "tracked.txt").write_text("v2 local change\n")
    pkg = tmp_path / "packaging" / "homebrew"
    pkg.mkdir(parents=True)
    (pkg / "hermes-agent.rb").write_text("formula\n")
    os.chmod(pkg, 0o555)  # undeletable contents, like a root-owned dir
    try:
        stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
        assert stash_ref

        # The tracked change is stashed; simulate the updater's checkout window.
        assert (tmp_path / "tracked.txt").read_text() == "v1\n"

        restored = hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )
        assert restored is True
        assert (tmp_path / "tracked.txt").read_text() == "v2 local change\n"
        assert (pkg / "hermes-agent.rb").read_text() == "formula\n"
    finally:
        os.chmod(pkg, 0o755)


def test_stash_selector_is_a_bare_index_never_a_brace_selector(tmp_path):
    """The updater drops its autostash through a selector read back from ``git stash list``; on
    native Windows MSYS strips the braces from ``stash@{N}`` in git.exe's argv, so the selector
    must be the bare index git accepts everywhere (#87542)."""
    import subprocess

    import hermes_cli.update_cmd_stash as stash_mod

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("v1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    (tmp_path / "f.txt").write_text("older\n")
    git("stash", "push", "-q", "-m", "older")
    target_sha = git("rev-parse", "refs/stash").stdout.strip()
    (tmp_path / "f.txt").write_text("newer\n")
    git("stash", "push", "-q", "-m", "newer")

    selector = stash_mod._resolve_stash_selector(["git"], tmp_path, target_sha)

    assert selector == "1"
    git("stash", "drop", selector)
    assert target_sha not in git("stash", "list", "--format=%H").stdout


def test_autostash_survives_intent_to_add_entries(tmp_path):
    """An index entry from `git add -N` must not block the update autostash.

    Reported: `hermes update` aborted with "Entry 'tests/...' not uptodate. Cannot merge." because
    `git add -N` records a path with the empty blob and zeroed stat data, which `git stash push`
    refuses outright. Editors that show new files in diffs leave exactly that state behind, and the
    update must not require the user to repair their index by hand.
    """
    import subprocess

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    # The reported shape: a new local file recorded with `git add -N`, alongside a normal edit.
    (tmp_path / "tracked.txt").write_text("v2 local\n", encoding="utf-8")
    local_test = tmp_path / "tests" / "test_live_custom_provider_poll.py"
    local_test.parent.mkdir()
    body = "def test_poll():\n    assert True\n"
    local_test.write_text(body, encoding="utf-8")
    git("add", "-N", "tests/test_live_custom_provider_poll.py")
    # Precondition: git reports it as " A" (present in the worktree, absent from the index) - the
    # intent-to-add shape that `git stash push` refuses.
    assert " A tests/test_live_custom_provider_poll.py" in git("status", "--porcelain").stdout.splitlines()

    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)

    assert stash_ref, "the update must be able to stash an intent-to-add entry"
    # The stash must have taken everything, so the pull cannot be blocked by a dirty tree.
    assert git("status", "--porcelain").stdout == ""
    assert hermes_main._restore_stashed_changes(["git"], tmp_path, stash_ref, prompt_user=False)
    assert local_test.read_text(encoding="utf-8") == body
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "v2 local\n"


def test_restore_rejects_invalid_python_and_keeps_clean_updated_tree(
    monkeypatch, tmp_path, capsys
):
    """A cleanly-applied stash must not be allowed to brick every agent turn."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "tools" / "terminal_tool.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("<<<<<<< Updated upstream\nVALUE = 2\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "made the Hermes agent unexecutable" in output
    assert "gateway was not restarted" in output
    assert f"git stash apply {stash_ref}" in output


def test_restore_rejects_new_import_time_failure_and_preserves_stash(
    monkeypatch, tmp_path, capsys
):
    """A valid-Python stash must not introduce a critical import failure."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("raise RuntimeError('restored local failure')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import consumer" in output
    assert "restored local failure" in output
    assert "gateway was not restarted" in output


def test_restore_allows_preexisting_import_time_failure(monkeypatch, tmp_path):
    """A restore may proceed when it does not worsen an environment failure."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "consumer.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    local_file = tmp_path / "local.txt"
    local_file.write_text("original\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    local_file.write_text("restored\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    assert hermes_main._restore_stashed_changes(
        ["git"], tmp_path, stash_ref, prompt_user=False
    )
    assert local_file.read_text(encoding="utf-8") == "restored\n"
    assert git("stash", "list").stdout.strip() == ""


def test_restore_rejects_later_failure_masked_by_preexisting_failure(
    monkeypatch, tmp_path, capsys
):
    """Every critical module must be compared, not only the first failure."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "first.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    second = tmp_path / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    second.write_text("raise RuntimeError('restored later failure')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("first", "second"))
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ("first", "second"))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert second.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import second" in output
    assert "restored later failure" in output
    assert "gateway was not restarted" in output


def test_restore_rejects_system_exit_masked_by_preexisting_failure(
    monkeypatch, tmp_path, capsys
):
    """A terminating import must be compared instead of hiding the marker."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "first.py").write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    second = tmp_path / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    second.write_text("raise SystemExit('restored exit')\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("first", "second"))
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ("first", "second"))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert second.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "agent import second" in output
    assert "restored exit" in output
    assert "gateway was not restarted" in output


def test_restore_rejects_probe_termination(monkeypatch, tmp_path, capsys):
    """A stash cannot bypass import validation by terminating the probe."""
    import subprocess
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_deps as update_cmd_deps

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("import os\nos._exit(7)\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    monkeypatch.setattr(update_cmd_deps, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    with pytest.raises(SystemExit) as exc_info:
        hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git("status", "--porcelain").stdout == ""
    assert git("stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "critical-module probe" in output
    assert "exit code 7" in output
    assert "gateway was not restarted" in output

def test_restore_stays_parked_when_untracked_baseline_is_unknown(
    monkeypatch, tmp_path, capsys
):
    """Unknown cleanup scope must not turn into a destructive empty baseline."""
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_stash as update_cmd_stash

    monkeypatch.setattr(update_cmd, "_git_untracked_paths", lambda *_args: None)
    monkeypatch.setattr(update_cmd_stash, "_git_untracked_paths", lambda *_args: None)

    restored = hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "stash@{0}", prompt_user=False
    )

    assert restored is False
    output = capsys.readouterr().out
    assert "cleanup baseline is unknown" in output
    assert "git stash apply stash@{0}" in output



def test_reject_does_not_claim_cleanup_when_git_state_is_unknown(
    monkeypatch, tmp_path, capsys
):
    """Cleanup failures must not be reported as a restored clean tree."""
    from hermes_cli import update_cmd
    import hermes_cli.update_cmd_stash as update_cmd_stash

    monkeypatch.setattr(update_cmd, "_git_untracked_paths", lambda *_args: None)
    monkeypatch.setattr(update_cmd_stash, "_git_untracked_paths", lambda *_args: None)

    with pytest.raises(SystemExit):
        update_cmd._reject_unsafe_stash_restore(
            ["git"], tmp_path, "stash@{0}", set(), "consumer.py", "invalid"
        )

    output = capsys.readouterr().out
    assert "could not be fully restored automatically" in output
    assert "The clean updated tree has been restored" not in output



def test_gateway_restore_prompt_defaults_to_keep_stash(tmp_path, capsys):
    prompts = []

    restored = hermes_main._restore_stashed_changes(
        ["git"],
        tmp_path,
        "stash@{0}",
        prompt_user=True,
        input_fn=lambda prompt, default: prompts.append((prompt, default)) or "",
    )

    assert restored is False
    assert prompts == [("Restore local changes now? [y/N]", "n")]
    assert "still preserved in git stash" in capsys.readouterr().out



def test_prune_orphan_rescue_refs_with_real_git_unpins_objects(tmp_path):
    """End-to-end with real git: an orphan rescue ref pins a snapshot's
    objects against gc; pruning the ref (age-expired) makes them collectable.
    This is the sabotage/size test for the #87745 bounded-growth mitigation."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "gc.auto", "0")
    (tmp_path / "f.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    # Snapshot commit carrying a "large" payload (scaled down for CI).
    import os

    (tmp_path / "big.bin").write_bytes(os.urandom(512 * 1024))
    git("add", "-A")
    git("commit", "-qm", "snapshot")
    snap_sha = git("rev-parse", "HEAD").stdout.strip()

    # Rewind, park the snapshot behind an AGE-EXPIRED rescue ref.
    git("reset", "-q", "--hard", "HEAD~1")
    old_ref = "refs/hermes-update-backups/orphan-main-20200101-000000-" + snap_sha[:12]
    git("update-ref", old_ref, snap_sha)

    # With the ref present, gc cannot drop the snapshot objects.
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "-q", "--prune=now")
    assert git("cat-file", "-e", snap_sha, check=False).returncode == 0

    # Prune (the ref's 2020 timestamp is way past the age window) → ref gone.
    update_cmd._prune_orphan_rescue_refs(["git"], tmp_path, "main")
    remaining = git("for-each-ref", "refs/hermes-update-backups/").stdout
    assert old_ref not in remaining

    # And gc can now reclaim the snapshot's objects.
    git("gc", "-q", "--prune=now")
    assert git("cat-file", "-e", snap_sha, check=False).returncode != 0


def test_autostash_survives_intent_to_add_entries(tmp_path):
    """An index entry from `git add -N` must not block the update autostash.

    Reported: `hermes update` aborted with "Entry 'tests/...' not uptodate. Cannot merge." because
    `git add -N` records a path with the empty blob and zeroed stat data, which `git stash push`
    refuses outright. Editors that show new files in diffs leave exactly that state behind, and the
    update must not require the user to repair their index by hand.
    """
    import subprocess

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    # The reported shape: a new local file recorded with `git add -N`, alongside a normal edit.
    (tmp_path / "tracked.txt").write_text("v2 local\n", encoding="utf-8")
    local_test = tmp_path / "tests" / "test_live_custom_provider_poll.py"
    local_test.parent.mkdir()
    body = "def test_poll():\n    assert True\n"
    local_test.write_text(body, encoding="utf-8")
    git("add", "-N", "tests/test_live_custom_provider_poll.py")
    # Precondition: git reports it as " A" (present in the worktree, absent from the index) - the
    # intent-to-add shape that `git stash push` refuses.
    assert " A tests/test_live_custom_provider_poll.py" in git("status", "--porcelain").stdout.splitlines()

    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)

    assert stash_ref, "the update must be able to stash an intent-to-add entry"
    # The stash must have taken everything, so the pull cannot be blocked by a dirty tree.
    assert git("status", "--porcelain").stdout == ""
    assert hermes_main._restore_stashed_changes(["git"], tmp_path, stash_ref, prompt_user=False)
    assert local_test.read_text(encoding="utf-8") == body
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "v2 local\n"


class _ReceiptProbe:
    """Minimal stand-in for the active update receipt: records steps.

    ``record_step`` clones the active receipt before mutating it (copy-on-write per context),
    so the steps list is shared by reference for the probe to observe.
    """

    def __init__(self):
        self.steps = []
        self.data = {}  # copied per record; ``steps`` is shared by reference

    def step(self, name, ok, detail=""):
        self.steps.append({"name": name, "ok": ok, "detail": detail})


@contextlib.contextmanager
def _active_receipt(probe):
    from hermes_cli import update_receipt

    token = update_receipt._current.set(probe)
    try:
        yield
    finally:
        update_receipt._current.reset(token)


def test_conflicted_restore_records_parked_step_in_receipt(monkeypatch, tmp_path):
    import subprocess
    from hermes_cli import update_receipt

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "tools" / "terminal_tool.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    # Local edit whose restore will conflict with the pulled change.
    source.write_text("VALUE = 2\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    # Simulate the pull moving the same lines: the stash apply now conflicts.
    git("checkout", "HEAD")
    source.write_text("VALUE = 3\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "pulled change")

    probe = _ReceiptProbe()
    with _active_receipt(probe):
        restored = hermes_main._restore_stashed_changes(["git"], tmp_path, stash_ref, prompt_user=False)

    assert restored is False
    disposition = [s for s in probe.steps if s["name"] == "local_changes_stash"]
    assert len(disposition) == 1
    assert disposition[0]["ok"] is False
    assert "parked" in disposition[0]["detail"]
    assert stash_ref in disposition[0]["detail"]
    assert git("stash", "list").stdout.strip(), "stash must survive for manual recovery"


def test_clean_restore_records_restored_step_in_receipt(monkeypatch, tmp_path):
    import subprocess
    from hermes_cli import update_receipt

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "tools" / "terminal_tool.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("VALUE = 2\n", encoding="utf-8")
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref

    probe = _ReceiptProbe()
    with _active_receipt(probe):
        restored = hermes_main._restore_stashed_changes(["git"], tmp_path, stash_ref, prompt_user=False)

    assert restored is True
    disposition = [s for s in probe.steps if s["name"] == "local_changes_stash"]
    assert len(disposition) == 1
    assert disposition[0]["ok"] is True
    assert "restored" in disposition[0]["detail"]
    assert stash_ref in disposition[0]["detail"]


def test_keep_stash_park_records_parked_step_in_receipt(capsys):
    probe = _ReceiptProbe()
    import hermes_cli.update_cmd_stash as stash_mod
    from hermes_cli import update_receipt

    with _active_receipt(probe):
        stash_mod._park_stashed_changes("deadbeefcafe")

    out = capsys.readouterr().out
    assert "--keep-stash" in out
    disposition = [s for s in probe.steps if s["name"] == "local_changes_stash"]
    assert len(disposition) == 1
    assert disposition[0]["ok"] is False
    assert "parked" in disposition[0]["detail"]
