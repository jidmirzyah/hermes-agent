"""Hardware selection for PM-owned llama.cpp binaries; mutable runtime state stays separate."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hermes_platform.host import facts

import pm
from pm.downloader import ProgressFn

BACKEND_PACKAGES = {
    "cuda": "llamacpp-cuda",
    "vulkan": "llamacpp-vulkan",
    "metal": "llamacpp-metal",
    "hip": "llamacpp-hip",
    "cpu": "llamacpp-cpu",
}


class BinaryResolutionError(RuntimeError):
    """The requested backend has no pinned or installed engine."""


@dataclass(frozen=True)
class Engine:
    backend: str
    tag: str
    binary: Path


def runtimes_root() -> Path:
    """Machine-scoped presets and server state. Binaries belong to PM's store."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "runtimes" / "llamacpp"


def manifest_verified(manifest: Path) -> bool:
    """True when an install manifest records a verified_version (missing/damaged -> False)."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and bool(data.get("verified_version"))


def _release_number(tag: str) -> int:
    digits = "".join(ch for ch in tag if ch.isdigit())
    return int(digits) if digits else 0


def installed_tags() -> list[str]:
    """Tags with a verified install, newest first by release number. The boot ladder and the
    update check both read installed-ness from here — one resolver, every caller."""
    root = runtimes_root()
    if not root.exists():
        return []
    found = {entry.name for entry in root.iterdir()
             if entry.is_dir() and entry.name != "downloads"
             and any(manifest_verified(m) for m in entry.glob("*/manifest.json"))}
    return sorted(found, key=_release_number, reverse=True)


def _host_os_arch() -> tuple[str, str]:
    """Return the host OS and architecture in release-asset vocabulary."""
    system = platform.system().lower()
    os_name = {"windows": "win", "darwin": "macos", "linux": "ubuntu"}.get(system, system)
    arch = "arm64" if facts.native_arch() == "arm64" else "x64"
    return os_name, arch
def pinned_tag(backend: str) -> str:
    version = pm.Lockfile(pm.paths.lockfile_path()).version(BACKEND_PACKAGES[backend])
    if version is None:
        raise BinaryResolutionError(f"llama.cpp {backend} has no PM version pin")
    return f"b{version}"


def select_backend(gpu_vendor: str | None, os_name: str | None = None) -> str:
    if os_name is None:
        os_name = "macos" if pm.current_target().startswith("darwin-") else "other"
    if os_name == "macos":
        return "metal"
    vendor = (gpu_vendor or "").lower()
    if "nvidia" in vendor:
        return "cuda"
    if any(name in vendor for name in ("amd", "intel", "radeon", "arc")):
        return "vulkan"
    return "cpu"


# Per-OS (human label, {backend: asset-name templates}). Windows CUDA pairs the runtime zip with
# its cudart zip; ubuntu ships tarballs, win ships zips.
_ASSET_TEMPLATES = {
    "ubuntu": ("linux", {
        "vulkan": ["llama-{tag}-bin-ubuntu-vulkan-{arch}.tar.gz"],
        "hip": ["llama-{tag}-bin-ubuntu-rocm-7.2-{arch}.tar.gz"],
        "cpu": ["llama-{tag}-bin-ubuntu-{arch}.tar.gz"],
    }),
    "win": ("windows", {
        "cuda": ["llama-{tag}-bin-win-cuda-{cuda_ver}-{arch}.zip",
                 "cudart-llama-bin-win-cuda-{cuda_ver}-{arch}.zip"],
        "vulkan": ["llama-{tag}-bin-win-vulkan-x64.zip"],
        "hip": ["llama-{tag}-bin-win-hip-radeon-x64.zip"],
        "cpu": ["llama-{tag}-bin-win-cpu-{arch}.zip"],
    }),
}


def resolve_assets(tag: str, backend: str, os_name: str | None = None,
                   arch: str | None = None) -> AssetPlan:
    """Compose the asset list for (tag, backend, platform). Raises BinaryResolutionError for pairs
    the release ships no artifact for; callers fall back down the ladder cuda -> vulkan -> cpu."""
    host_os, host_arch = _host_os_arch()
    os_name = os_name or host_os
    arch = arch or host_arch
    if os_name == "macos":
        # macOS tarballs are unified (Metal built in).
        return AssetPlan(tag, backend, [f"llama-{tag}-bin-macos-{arch}.tar.gz"])
    if os_name not in _ASSET_TEMPLATES:
        raise BinaryResolutionError(f"unsupported platform {os_name}-{arch}")
    if os_name == "ubuntu" and backend == "cuda":
        # No prebuilt Linux CUDA zips at current tags — Linux CUDA users build from source or
        # use vulkan; the resolver is honest about it.
        raise BinaryResolutionError(
            f"no prebuilt linux CUDA asset at {tag}; use vulkan/cpu or a source build")
    if os_name == "win" and backend == "vulkan" and arch == "arm64":
        raise BinaryResolutionError(f"no win-vulkan-arm64 asset at {tag}")
    label, templates = _ASSET_TEMPLATES[os_name]
    if backend not in templates:
        raise BinaryResolutionError(f"unsupported {label} backend {backend}")
    # release.yml switched both HIP names at b10356 (0666ad2b2b), then
    # ROCm 7.14 -> 10.0 at b10767 (cff184438e). Keep older explicit pins.
    if backend == "hip" and tag.startswith("b") and tag[1:].isascii() and tag[1:].isdigit():
        build = int(tag[1:])
        if build >= 10356:
            if arch != "x64":
                raise BinaryResolutionError(f"no {label} HIP {arch} asset at {tag}")
            rocm_ver = "10.0" if build >= 10767 else "7.14"
            extension = "zip" if os_name == "win" else "tar.gz"
            return AssetPlan(tag, backend, [
                f"llama-{tag}-bin-{os_name}-rocm-{rocm_ver}-{arch}.{extension}"])
    cuda_ver = _WIN_CUDA_VERSION_ARM64 if arch == "arm64" else _WIN_CUDA_VERSION
    return AssetPlan(tag, backend, [t.format(tag=tag, arch=arch, cuda_ver=cuda_ver)
                                    for t in templates[backend]])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


# How long a finished download may wait for another process to let go of it before the
# download is reported as failed.
_RELEASE_WAIT_SECONDS = 60.0


def replace_when_released(tmp: Path, dest: Path, *, timeout: float = _RELEASE_WAIT_SECONDS) -> None:
    """Rename a finished download into place, waiting out a transient hold on the file.

    On Windows a file whose last write handle just closed is often still open to an antivirus
    or indexing scan, and renaming it fails with a permission error until the scan lets go —
    for a multi-gigabyte model that can take many seconds. ``os.replace`` is retried through
    that window; it never falls back to copying (``shutil.move`` does, which duplicates the whole
    file and then reports the leftover's failed delete as the download's failure). A hold that
    outlasts the window raises a plain-language error.
    """
    deadline = time.monotonic() + timeout
    delay = 0.1
    while True:
        try:
            os.replace(tmp, dest)
            return
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"The download finished, but another program (usually an antivirus scan) kept "
                    f"{tmp.name} open and it could not be renamed into place. Please try again.") from exc
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def _download(url: str, dest: Path,
              progress: "Callable[[int, int], None] | None" = None) -> None:
    """Stream url -> dest. ``progress(done_bytes, total_bytes)`` ticks per chunk (total 0 when
    the server sends no Content-Length) — a several-hundred-MB archive must never look hung."""
    logger.info("downloading %s", url)
    staging = tempfile.NamedTemporaryFile(
        mode="wb", dir=dest.parent, prefix=f"{dest.name}.", suffix=".part", delete=False)
    tmp = Path(staging.name)
    try:
        with staging as f, urllib.request.urlopen(url, timeout=120) as r:
            length = r.headers.get("Content-Length")
            total = int(length) if length is not None else 0
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
            # Chunked reads can return EOF without raising IncompleteRead.
            if length is not None and done != total:
                raise BinaryResolutionError(
                    f"incomplete download for {dest.name}: expected {total} bytes, got {done}")
        replace_when_released(tmp, dest)
    finally:
        # Best effort: a leftover that cannot be removed must not hide the error that left it.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def _extract(archive: Path, dest: Path,
             progress: "Callable[[int, int], None] | None" = None) -> None:
    """Extract member by member so ``progress(done, total)`` can tick in uncompressed bytes."""
    if archive.name.endswith(".zip"):
        opener, list_members, size = zipfile.ZipFile, "infolist", "file_size"
        kwargs = {}
    else:
        import tarfile
        opener, list_members, size = tarfile.open, "getmembers", "size"
        kwargs = {"filter": "data"}
    with opener(archive) as ar:
        members = getattr(ar, list_members)()
        total = sum(getattr(m, size) for m in members)
        done = 0
        for m in members:
            ar.extract(m, dest, **kwargs)
            done += getattr(m, size)
            if progress is not None:
                progress(done, total)


def server_binary(install_dir: Path) -> Path:
    """Locate llama-server within an extracted runtime (zips differ in whether they nest a
    build/bin directory)."""
    names = ("llama-server.exe", "llama-server")
    for name in names:
        direct = install_dir / name
        if direct.exists():
            return direct
    for name in names:
        hits = sorted(install_dir.rglob(name))
        if hits:
            return hits[0]
    raise BinaryResolutionError(f"llama-server not found under {install_dir}")


def verify_install(install_dir: Path, tag: str) -> str:
    """Run --version; require the tag's build number in the output (printed WITHOUT the 'b')."""
    exe = server_binary(install_dir)
    out = subprocess.run([str(exe), "--version"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=60, cwd=str(exe.parent))
    text = (out.stdout + out.stderr).strip()
    if tag.lstrip("b") not in text:
        raise BinaryResolutionError(
            f"version check failed for {exe}: expected {tag}, got: {text[:120]}")
    return text.splitlines()[0] if text else ""


def prune_old_tags(keep: list[str]) -> None:
    """Retain only the tags in ``keep`` (current + previous — N-1 rollback). The shared
    ``downloads/`` archive cache is not a tag and always survives."""
    root = runtimes_root()
    if not root.exists():
        return
    for entry in root.iterdir():
        if entry.is_dir() and entry.name != "downloads" and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
            logger.info("pruned old runtime %s", entry.name)


def ensure_runtime_installed(tag: str, backend: str,
                             expected_sha256: dict[str, str] | None = None,
                             progress: "Callable[[str, int, int, str], None] | None" = None) -> Path:
    """Idempotent: resolve, download, verify, extract, version-check; returns the install dir.
    ``expected_sha256`` pins hashes per asset; without pins the computed hash is recorded in the
    manifest (trust on first download, verified on every reinstall). ``progress(stage, done,
    total, label)`` ticks through download/extract/verify."""
    plan = resolve_assets(tag, backend)
    install_dir = plan.install_dir
    manifest_path = install_dir / "manifest.json"
    if manifest_verified(manifest_path):
        return install_dir

    install_dir.mkdir(parents=True, exist_ok=True)
    downloads = runtimes_root() / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    def stage_progress(stage: str, label: str):
        if progress is None:
            return None
        tick = progress
        return lambda d, t: tick(stage, d, t, label)

    recorded: dict[str, str] = {}
    n_assets = len(plan.assets)
    for i, asset in enumerate(plan.assets, 1):
        label = f"{i}/{n_assets}" if n_assets > 1 else ""
        archive = downloads / asset
        if not archive.exists():
            _download(RELEASE_URL.format(tag=tag, asset=asset), archive,
                      progress=stage_progress("download", label))
        if progress is not None:
            progress("verify", 0, 0, label)
        digest = _sha256(archive)
        expected = (expected_sha256 or {}).get(asset)
        if expected and digest != expected:
            archive.unlink(missing_ok=True)
            raise BinaryResolutionError(
                f"sha256 mismatch for {asset}: expected {expected}, got {digest}")
        recorded[asset] = digest
        _extract(archive, install_dir, progress=stage_progress("extract", label))

    if progress is not None:
        progress("verify", 0, 0, "")
    version = verify_install(install_dir, tag)
    manifest_path.write_text(json.dumps({"tag": tag, "backend": plan.backend, "assets": recorded,
                                         "verified_version": version}, indent=2), encoding="utf-8")
    logger.info("installed llama.cpp %s (%s): %s", tag, backend, version)
    return install_dir
def _candidates(requested: str, gpu_vendor: str | None, target: str) -> tuple[str, ...]:
    if requested != "auto":
        if requested not in BACKEND_PACKAGES:
            raise BinaryResolutionError(f"unknown backend {requested}")
        return (requested,)
    preferred = select_backend(gpu_vendor, "macos" if target.startswith("darwin-") else "other")
    fallback = ("vulkan", "cpu") if gpu_vendor else ("cpu",)
    return tuple(dict.fromkeys((preferred, *fallback)))


def unavailable_reason(backend: str, target: str | None = None) -> str | None:
    name = BACKEND_PACKAGES.get(backend)
    if name is None:
        return f"unknown backend {backend}"
    target = target or pm.current_target()
    reason = pm.get_package(name).missing_reason(target)
    if reason:
        return reason
    lock = pm.Lockfile(pm.paths.lockfile_path())
    if not lock.version(name) or not lock.artifacts(name, target):
        return f"{name} is not pinned for {target}"
    return None


def resolve_backend(requested: str = "auto", *, gpu_vendor: str | None = None,
                    target: str | None = None) -> str:
    """Explicit choices are strict. Auto only falls back to compatible pinned builds."""
    if requested == "auto" and target is None and gpu_vendor is None:
        from hermes_cli.local_runtime.bootstrap import _detect_gpu_vendor

        gpu_vendor = _detect_gpu_vendor()
    target = target or pm.current_target()
    reasons = []
    for backend in _candidates(requested, gpu_vendor, target):
        reason = unavailable_reason(backend, target)
        if reason is None:
            return backend
        reasons.append(reason)
    raise BinaryResolutionError("; ".join(reasons))


def installed_engine(backend: str = "auto", *, allow_outdated: bool = True) -> Engine | None:
    """Boot may retain a prior PM pin, but never installs or adopts unmanaged bytes."""
    vendor = None
    if backend == "auto":
        from hermes_cli.local_runtime.bootstrap import _detect_gpu_vendor

        vendor = _detect_gpu_vendor()
    for candidate in _candidates(backend, vendor, pm.current_target()):
        found = pm.installed_package(BACKEND_PACKAGES[candidate], allow_outdated=allow_outdated)
        if found is not None and found.binary is not None:
            return Engine(candidate, f"b{found.version}", found.binary)
    return None


def ensure_engine(backend: str, *, progress: Callable[[str, int, int, str], None] | None = None,
                  pause_event: threading.Event | None = None,
                  download_progress: ProgressFn | None = None) -> Engine:
    """Only deliberate install/update jobs call this. PM verifies every archive and publishes."""
    resolved = resolve_backend(backend)
    pm.ensure(BACKEND_PACKAGES[resolved], explicit=True, progress=progress,
              pause_event=pause_event, download_progress=download_progress)
    engine = installed_engine(resolved, allow_outdated=False)
    if engine is None:
        raise BinaryResolutionError(f"llama.cpp {resolved} install has no usable pinned binary")
    return engine


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.


_PLUGIN_COMPAT_LAZY = {
    'get_hermes_home': ('hermes_constants', 'get_hermes_home'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
