"""Update markers, Windows launcher recovery and subprocess handoff."""

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import time as _time

from pathlib import Path
from hermes_cli import _early_recovery as _early_recovery_mod

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.main")


def _pyproject_project(debug_fmt: str | None = None) -> dict | None:
    """``[project]`` table of pyproject.toml, or ``None`` when absent/unreadable."""
    from hermes_cli.main import PROJECT_ROOT
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
        with pyproject.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except Exception as exc:
        if debug_fmt:
            logger.debug(debug_fmt, exc)
        return None
    return project if isinstance(project, dict) else None


# Install-scoped breadcrumbs live next to the venv (not under $HERMES_HOME)
# because the venv is shared across profiles.
#   ``.update-incomplete``       — generic core ``.[all]`` install was interrupted;
#     cleared only after a confirmed full dependency reinstall/recovery.
#   ``.lazy-refresh-incomplete`` — lazy-backend refresh may have corrupted packages;
#     cleared only after import-probe repair confirms healthy (never on indeterminate).
# Narrow lazy probes must NEVER clear the generic core marker.
# See #58004.
def _update_marker_path() -> Path:
    from hermes_cli.main import PROJECT_ROOT
    return PROJECT_ROOT / ".update-incomplete"


def _lazy_refresh_marker_path() -> Path:
    from hermes_cli.main import PROJECT_ROOT
    return PROJECT_ROOT / ".lazy-refresh-incomplete"


def _pytest_owns_live_checkout(root: Path) -> bool:
    """True under pytest when ``root`` is this checkout: unsandboxed update/recovery tests must
    neither litter the live repo root with breadcrumbs (false-arming the developer's next launch)
    nor run a real reinstall against the executing venv (cf. ``managed_scope._under_pytest``)."""
    return "PYTEST_CURRENT_TEST" in os.environ and root == Path(__file__).resolve().parent.parent


def _clear_marker_file(path: Path, *, label: str) -> None:
    """Remove an update-recovery breadcrumb. Never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not clear %s marker: %s", label, exc)


def _clear_update_incomplete_marker() -> None:
    """Remove the interrupted core-install breadcrumb. Never raises."""
    _clear_marker_file(_update_marker_path(), label="update-incomplete")


def _clear_lazy_refresh_incomplete_marker() -> None:
    """Remove the interrupted lazy-refresh breadcrumb. Never raises."""
    _clear_marker_file(_lazy_refresh_marker_path(), label="lazy-refresh-incomplete")


def _norm_exe_path(path) -> str:
    """Case-folded resolved path, for comparing executables on Windows."""
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def _windows_shim_in_process_chain() -> Path | None:
    """The venv console shim this process runs from or under, if any.

    ``venv\\Scripts\\hermes.exe`` holds itself open (no ``FILE_SHARE_DELETE``) for the whole
    process lifetime, so an editable install run from one can never rewrite it. Two probes, since
    either can come up empty: own launch paths (argv[0], ``__main__`` file/spec origin — runpy/
    zipapp puts ``<shim>\\__main__.py`` there) and psutil ancestry. Candidates are intersected
    with the project venv's own shims so a foreign ``hermes.exe`` never matches.

    See #88838, #89599.
    """
    _match = _venv_shim_matcher()
    if _match is None:
        return None

    main_mod = sys.modules.get("__main__")
    candidates = [*sys.argv[:1], *filter(None, (
        getattr(main_mod, "__file__", None),
        getattr(getattr(main_mod, "__spec__", None), "origin", None)))]
    for candidate in candidates:
        matched = _match(candidate)
        if matched is not None:
            return matched

    ancestor = _windows_shim_ancestor(_match)
    return None if ancestor is None else ancestor[0]


def _venv_shim_matcher():
    """``candidate -> shim | None`` against the project venv's own console shims, or ``None`` when
    there is nothing to match (not Windows, no venv, no shims)."""
    if not _is_windows():
        return None
    scripts_dir = _venv_scripts_dir()
    if scripts_dir is None:
        return None
    shims = {_norm_exe_path(shim): shim for shim in _hermes_exe_shims(scripts_dir)}
    if not shims:
        return None

    def _match(candidate) -> Path | None:
        path = Path(candidate)
        if path.name.lower() == "__main__.py":
            path = path.parent
        return shims.get(_norm_exe_path(path))

    return _match


def _windows_shim_ancestor(_match) -> tuple[Path, int] | None:
    """``(shim, pid)`` of the nearest process in our chain (self first) whose executable IS a shim."""
    with contextlib.suppress(Exception):
        import psutil
        me = psutil.Process()
        for proc in [me] + list(me.parents()):
            try:
                matched = _match(proc.exe())
            except Exception:
                continue
            if matched is not None:
                return matched, proc.pid
    return None


def _windows_shim_holder_pid() -> int:
    """Pid a detached child must outwait before touching the venv: the ``hermes.exe`` launcher
    ancestor that holds the shim image open (it spawns this interpreter and exits only after
    reaping it), else this process — argv names the shim but it is the launcher that locks it."""
    _match = _venv_shim_matcher()
    ancestor = _windows_shim_ancestor(_match) if _match is not None else None
    return os.getpid() if ancestor is None else ancestor[1]


def _windows_running_hermes_launcher_locked() -> bool:
    """True when a venv ``hermes*.exe`` shim is this process or an ancestor (best-effort)."""
    return _windows_shim_in_process_chain() is not None


# Set on the re-exec'd child so it can never spawn another one.
_UPDATE_REEXEC_ENV = "HERMES_UPDATE_REEXEC"


def _reexec_dependency_sync_off_windows_shim(gateway_resume: dict | None = None) -> bool:
    """Hand the dependency sync to the venv interpreter, off the console shim.

    Returns True when a child was spawned and the caller must exit at once (releasing the
    shim before the child reaches ``pip install -e .``); False to continue in-process.

    Called at the dependency-sync boundary, NOT at the top of the command: by then the code swap
    is done and every interactive question has been answered; only the venv rewrite — the one
    step that cannot run inside the shim — remains. Earlier would detach every run (even the
    ``Already up to date!`` no-op) and take the prompts along. Waiting on the child deadlocks
    (we hold the handle it needs) and Windows has no exec, so the shell returns; the child keeps
    the console, prints its own result, and ``--gateway`` writes the true exit code to
    ``.update_exit_code``. The child re-runs ``hermes update`` so the sync and its tail happen
    exactly once; ``_UPDATE_REEXEC_ENV`` stops it spawning again and stops the "already up to
    date" early return from swallowing the sync. ``.update-incomplete`` is already written, so
    a child that dies mid-install is finished by the next launch's recovery.

    The child owns the Windows gateway resume from the moment it exists: ``gateway_resume``
    travels in its env and this process's copy is disarmed, so the parent exits at once instead
    of relaunching gateways while it still holds the shim (#101600). The child waits for this
    pid before its own pause/venv work (``update_handoff.wait_for_shim_parent_exit``).
    ``venv\\Scripts\\hermes.exe`` is a launcher that runs the interpreter with the shim as its script and
    holds it open without ``FILE_SHARE_DELETE`` for the whole command, so the quarantine rename is refused
    and uv fails to replace it with os error 32 (#88838, #89599).
    """
    if os.environ.get(_UPDATE_REEXEC_ENV) == "1":
        return False
    shim = _windows_shim_in_process_chain()
    if shim is None:
        return False
    from hermes_constants import venv_python_path
    from hermes_cli.update_handoff import detached_shim_child_env
    python_exe = venv_python_path(shim.parent.parent, windows=True)
    cmd = [str(python_exe), "-m", "hermes_cli.main", *sys.argv[1:]]
    if python_exe.is_file():
        try:
            subprocess.Popen(
                cmd, env=detached_shim_child_env({**os.environ, _UPDATE_REEXEC_ENV: "1"}, gateway_resume),
                stdin=subprocess.DEVNULL)
            if gateway_resume is not None:
                gateway_resume["resume_needed"] = False
            print(
                f"→ Windows: {shim.name} cannot replace itself while it runs; "
                "finishing the dependency install under the venv Python.")
            print(
                "  The code update is already applied. The install continues "
                "below and this shell returns right away.")
            return True
        except OSError as exc:
            logger.debug("Dependency-sync hand-off via %s failed: %s", python_exe, exc)
        print(f"  ⚠ Could not hand the dependency install off {shim.name}.")
        print("    Continuing in-process; if it cannot replace the shim, run:")
        print(f"    {subprocess.list2cmdline(cmd)}")
    return False


def _is_windows() -> bool:
    return sys.platform == "win32"


def _venv_scripts_dir() -> Path | None:
    """Return the venv Scripts directory if we're running inside the project venv."""
    from hermes_cli.main import PROJECT_ROOT
    from hermes_constants import project_venv_dir, venv_bin_dir
    venv_dir = project_venv_dir(PROJECT_ROOT)
    if venv_dir is None:
        return None
    scripts = venv_bin_dir(venv_dir, windows=_is_windows())
    return scripts if scripts.is_dir() else None


def _hermes_exe_shims(scripts_dir: Path) -> list[Path]:
    """Entry-point shims uv may rewrite during ``pip install -e .`` — Windows .exe launchers
    only; POSIX shims are plain scripts replaced atomically."""
    if not _is_windows():
        return []
    names = set(_load_console_script_names()) or {"hermes", "hermes-agent", "hermes-acp"}
    # Not a [project.scripts] entry point, but older update/install paths still
    # rewrite and quarantine it.
    names.add("hermes-gateway")
    return [scripts_dir / f"{name}.exe" for name in sorted(names)]


_PENDING_RENAME_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager"
_PENDING_RENAME_VALUE = "PendingFileRenameOperations"


def _filter_pending_shim_renames(entries: list[str], shims: list[Path]) -> tuple[list[str], int]:
    """Drop our ``<shim>`` -> ``<shim>.old.<stamp>`` pairs from a PendingFileRenameOperations
    value (a flat REG_MULTI_SZ of (source, target) pairs shared with other installers).
    Returns the entries to keep and how many pairs were dropped."""
    import ntpath

    def _norm(value: str) -> str:
        path = str(value).lstrip("!")
        if path.startswith("\\??\\"):
            path = path[4:]
        return ntpath.normcase(ntpath.normpath(path))

    shim_paths = {_norm(str(shim)) for shim in shims}
    kept: list[str] = []
    removed = 0
    for index in range(0, len(entries) - 1, 2):
        source, target = entries[index], entries[index + 1]
        source_norm = _norm(source)
        if source_norm in shim_paths and _norm(target).startswith(f"{source_norm}.old."):
            removed += 1
        else:
            kept.extend((source, target))
    if len(entries) % 2:
        kept.append(entries[-1])
    return kept, removed


def _cleanup_pending_shim_renames(scripts_dir: Path) -> int:
    """Drop reboot renames older Hermes versions queued for our shims: ``MOVEFILE_DELAY_UNTIL_REBOOT``
    fallbacks outlive the update that queued them and move away whatever sits at the shim path
    at next boot — even a shim a later repair just wrote. Needs elevation; a no-op otherwise."""
    if not _is_windows():
        return 0
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _PENDING_RENAME_KEY, 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            entries, value_type = winreg.QueryValueEx(key, _PENDING_RENAME_VALUE)
            if value_type != winreg.REG_MULTI_SZ or not isinstance(entries, list):
                return 0
            kept, removed = _filter_pending_shim_renames(entries, _hermes_exe_shims(scripts_dir))
            if not removed:
                return 0
            if kept:
                winreg.SetValueEx(key, _PENDING_RENAME_VALUE, 0, winreg.REG_MULTI_SZ, kept)
            else:
                winreg.DeleteValue(key, _PENDING_RENAME_VALUE)
            return removed
    except (OSError, ValueError):
        return 0


# Fresh orphan files can belong to an updater still running.
_QUARANTINE_GRACE_SECONDS = 15 * 60


def _quarantine_stamp_ms(stale: Path) -> int | None:
    """The ``.old.<unix-ms>`` stamp in a quarantine filename; ``None`` (not ours — neither rescued
    nor deleted) otherwise. Parsed from the NAME, not ``st_mtime``: ``rename`` preserves the
    shim's mtime (when uv wrote it), not when it was quarantined."""
    try:
        return int(stale.name.rsplit(".old.", 1)[1])
    except (IndexError, ValueError):
        return None


def _cleanup_quarantined_exes(scripts_dir: Path | None = None) -> None:
    """Sweep — and where necessary RESCUE — ``hermes.exe.old.*`` from updates.

    Called early on every invocation. Two cases an unconditional ``unlink()`` gets wrong:
    (1) orphan rescue — ``hermes.exe`` missing while ``hermes.exe.old.*`` exists means the .old
    file is the ONLY surviving copy (update died between rename and uv's write); put it back via
    the same retry-and-report helper the update-time restore uses. (2) concurrency — a fresh
    quarantine file may belong to an update in flight in another process; leave anything inside
    the grace window alone. Silent no-op on non-Windows, nothing to do, or locked/permission errors.

    Deleting it converts a one-rename recovery into a full reinstall. See #75584.
    """
    if not _is_windows():
        return
    scripts_dir = scripts_dir if scripts_dir is not None else _venv_scripts_dir()
    if scripts_dir is None:
        return
    _cleanup_pending_shim_renames(scripts_dir)
    now = _time.time()
    try:
        candidates = [
            (stamp, stale) for stale in scripts_dir.glob("*.exe.old.*")
            if (stamp := _quarantine_stamp_ms(stale)) is not None]
    except OSError:
        return
    # Newest first by PARSED stamp: lexicographic order breaks when a stray ``.old.999`` exists.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for stamp, stale in candidates:
        try:
            original = stale.with_name(stale.name.rsplit(".old.", 1)[0])
            if not original.exists():
                # Orphan rescue: last copy of the shim — retry ladder + recovery message.
                _early_recovery_mod.restore_quarantined_shims([(original, stale)])
                continue
            if now - stamp / 1000.0 < _QUARANTINE_GRACE_SECONDS:
                continue  # may be a live quarantine from a concurrent update
            stale.unlink()
        except OSError:
            pass  # still locked or in use — try again next run


def _load_console_script_names() -> list[str]:
    """Return ``[project.scripts]`` entry-point names from pyproject.toml."""
    project = _pyproject_project("console script verification: failed to read pyproject.toml: %s")
    scripts = (project or {}).get("scripts", {}) or {}
    return [str(name) for name in scripts if name]


def _is_termux_env(env: dict[str, str] | None = None) -> bool:
    from hermes_cli.main import _is_termux_startup_environment
    return _is_termux_startup_environment(env)


def _is_windows_npm_path(npm_path: str) -> bool:
    """True if ``npm_path`` points at a Windows npm shim (WSL drive interop, ``.cmd``/``.exe``, UNC).

    Callers use this only on a POSIX host — on native Windows ``npm.cmd`` is correct.
    """
    low = npm_path.lower()
    mount = low.split("/", 3)[2] if low.startswith("/mnt/") else ""
    return (
        low.endswith((".exe", ".cmd", ".bat"))
        or (len(mount) == 1 and mount.isalpha())
        or "\\" in npm_path
    )


def _resolve_node_runtime_npm() -> str | None:
    """Resolve an npm executable that belongs to the host's Node runtime.

    On WSL, PATH interop can hand back a Windows npm that fails with EISDIR / symlink errors over
    ``\\\\wsl.localhost\\...`` UNC paths. Refuse it on a POSIX host and re-scan PATH minus the
    Windows drive mounts. ``None`` when no suitable npm is reachable.

    On WSL/Linux ``shutil.which("npm")`` may resolve a Windows npm exposed through PATH interop. See #30271.
    """
    from hermes_constants import find_node_executable
    npm = find_node_executable("npm")
    if _is_windows():
        return npm
    if not npm:
        return None
    if not _is_windows_npm_path(npm):
        return npm
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or _is_windows_npm_path(directory):
            continue
        candidate = shutil.which("npm", path=directory)
        if candidate and not _is_windows_npm_path(candidate):
            return candidate
    return None


def _resolve_update_branch(args) -> str:
    """Normalize ``args.branch`` to a non-empty name (default ``main``; blank/whitespace = default)."""
    return (getattr(args, "branch", None) or "main").strip() or "main"
