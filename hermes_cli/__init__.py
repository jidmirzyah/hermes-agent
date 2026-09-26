"""Hermes CLI - Unified command-line interface for Hermes Agent."""

import os
import sys

__release_date__ = "2026.9.21"
# Declared for type checkers and the old-updater surface audit; served lazily by __getattr__.
__version__: str


def __getattr__(name: str) -> str:
    """Old-updater compat: shipped updaters import ``__version__`` after the checkout swap.

    tests/compat/old_updater_surface.json freezes that import. In-tree code resolves
    identity through hermes_cli.version_info.get_version_info(); this reads only the
    install stamp -- never git -- and keeps the pre-stamp placeholder when a checkout
    has no stamp.

    Lazy because ``pm`` is not importable when this package loads: a venv
    editable-installed from a pre-PM tree maps only the top-level packages it knew
    at install time, and the repo root reaches ``sys.path`` only once
    ``hermes_bootstrap`` runs -- after this ``__init__``, from ``hermes_cli.main``.
    """
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from hermes_cli.steward import read_install_stamp
    from pm.paths import repo_root

    return str(read_install_stamp(repo_root()).get("baseVersion") or "0.0.0")


def _ensure_utf8():
    """Force UTF-8 stdout/stderr to prevent UnicodeEncodeError crashes.

    The CLI prints box-drawing characters and the ☤ glyph in the setup wizard, doctor, and status
    banners; under a non-UTF-8 codec that raises before the command can even start (e.g.
    `hermes setup` on a fresh Pi).
    """
    repaired = False
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
                continue
            # Preferred: reconfigure in place, preserving object identity so code already holding
            # a reference to the old sys.stdout benefits from the repair too.
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
            else:
                # No reconfigure(): reopen the fd as UTF-8 (closefd=False keeps the original fd open).
                new_stream = open(stream.fileno(), "w", encoding="utf-8", errors="replace",  # windows-footgun: ok (stdout re-open for write, not a read)
                                  buffering=1, closefd=False)
                setattr(sys, stream_name, new_stream)
            repaired = True
        except (AttributeError, OSError, ValueError):
            pass
    # Only nudge child processes toward UTF-8 when a non-UTF-8 locale was actually detected; on a
    # healthy UTF-8 host children inherit it from the locale already.
    if repaired:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_ensure_utf8()
