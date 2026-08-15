"""Shared executable discovery and fingerprinting for M0 tooling."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from nova_rtl.contracts.platform import PROBE_VERSION_ARGUMENTS, ToolFingerprint

TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "yosys": ("yosys",),
    "opensta": ("opensta", "sta"),
    "openroad": ("openroad",),
    "eqy": ("eqy",),
    "sby": ("sby",),
    "slang": ("slang",),
    "iverilog": ("iverilog",),
    "verilator": ("verilator",),
}

VERSION_ARGUMENTS = PROBE_VERSION_ARGUMENTS
SUPPORTED_ADAPTER_VERSION = "bootstrap-doctor-v1"


class ToolProbeError(RuntimeError):
    """Raised when an executable cannot produce a trustworthy fingerprint."""


def resolve_tool(
    logical_name: str,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Resolve the first registered executable candidate for a logical tool."""

    for candidate in TOOL_CANDIDATES.get(logical_name, (logical_name,)):
        resolved = which(candidate)
        if resolved is not None:
            return Path(resolved).resolve()
    return None


def probe_executable(
    logical_name: str,
    executable: Path,
    *,
    adapter_version: str = SUPPORTED_ADAPTER_VERSION,
    version_args: tuple[str, ...] | None = None,
    timeout_seconds: int = 10,
) -> ToolFingerprint:
    """Fingerprint exact executable bytes together with its version/build output."""

    if adapter_version != SUPPORTED_ADAPTER_VERSION:
        raise ToolProbeError(f"unsupported probe adapter: {adapter_version}")
    try:
        resolved = executable.resolve()
    except (OSError, RuntimeError) as error:
        raise ToolProbeError(f"executable path resolution failed: {error}") from error
    if not resolved.is_file():
        raise ToolProbeError(f"required executable is missing: {resolved}")
    arguments = version_args or VERSION_ARGUMENTS.get(logical_name, ("--version",))
    executable_digest = hashlib.sha256()
    try:
        initial_stat = resolved.stat()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                executable_digest.update(chunk)
    except OSError as error:
        raise ToolProbeError(f"executable hash failed: {error}") from error
    executable_sha256 = f"sha256:{executable_digest.hexdigest()}"
    try:
        completed = subprocess.run(
            (str(resolved), *arguments),
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolProbeError(f"version probe failed: {error}") from error
    observed_after = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                observed_after.update(chunk)
        final_stat = resolved.stat()
    except OSError as error:
        raise ToolProbeError(f"post-probe executable hash failed: {error}") from error
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    identity_changed = any(
        getattr(initial_stat, field) != getattr(final_stat, field)
        for field in identity_fields
    )
    if identity_changed or observed_after.digest() != executable_digest.digest():
        raise ToolProbeError("executable changed during version probe")
    version_bytes = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    if completed.returncode != 0 or not version_bytes:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ToolProbeError(
            f"version probe returned exit code {completed.returncode}: {detail}"
        )

    build_digest = executable_digest.copy()
    build_digest.update(completed.stdout)
    build_digest.update(completed.stderr)
    try:
        version = version_bytes[0].decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ToolProbeError("version output is not valid UTF-8") from error

    return ToolFingerprint(
        tool_id=logical_name,
        executable=str(resolved),
        version=version,
        version_args=arguments,
        executable_sha256=executable_sha256,
        build_hash=f"sha256:{build_digest.hexdigest()}",
        adapter_version=adapter_version,
        container_digest=None,
    )


__all__ = [
    "TOOL_CANDIDATES",
    "SUPPORTED_ADAPTER_VERSION",
    "VERSION_ARGUMENTS",
    "ToolProbeError",
    "probe_executable",
    "resolve_tool",
]
