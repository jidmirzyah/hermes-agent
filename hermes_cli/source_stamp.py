"""Atomically publish identity for a mutable source checkout."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from hermes_cli.version_info import _git_version_info, _reset_version_info_cache


def write_source_stamp(root: Path) -> dict | None:
    """Replace ``install-stamp.json`` with identity read from ``root`` itself.

    A root git cannot identify -- the ZIP update fallback runs precisely because
    git is unusable -- publishes no identity: the old stamp is removed rather
    than left naming the commit that was just replaced. Returns None then.
    """
    root = Path(root).resolve()
    info = _git_version_info(root, include_untracked=True)
    if info.commit is None:
        with suppress(FileNotFoundError):
            (root / "install-stamp.json").unlink()
        _reset_version_info_cache()
        return None
    stamp = {
        "schemaVersion": 2,
        "commit": info.commit,
        "commitDate": info.commit_date,
        "branch": info.branch,
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "dirty": info.dirty,
        "source": "git",
        "distribution": None,
        "updateMechanism": "self",
        "baseVersion": info.base_version,
        "displayVersion": info.derived_version,
        "distance": info.distance,
        "payload": "bootstrap",
        "tag": None,
    }
    stamp_path = root / "install-stamp.json"
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".install-stamp.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(stamp, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, stamp_path)
        with suppress(OSError):
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with suppress(OSError):
            os.unlink(tmp_name)
    _reset_version_info_cache()
    return stamp