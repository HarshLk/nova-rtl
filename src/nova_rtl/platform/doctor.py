"""Deterministic bootstrap checks for the NOVA-RTL toolchain."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from nova_rtl.contracts.platform import DoctorCheck, DoctorReport, ToolFingerprint

DEFAULT_REQUIRED_TOOLS = ("yosys", "opensta", "openroad", "eqy", "sby")

_TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "yosys": ("yosys",),
    "opensta": ("opensta", "sta"),
    "openroad": ("openroad",),
    "eqy": ("eqy",),
    "sby": ("sby",),
}

_VERSION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "yosys": ("-V",),
    "opensta": ("-version",),
    "openroad": ("-version",),
    "eqy": ("--version",),
    "sby": ("--version",),
}


def _sha256_file(path: Path, suffix: bytes = b"") -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    digest.update(suffix)
    return f"sha256:{digest.hexdigest()}"


def _resolve_tool(logical_name: str, which: Callable[[str], str | None]) -> str | None:
    for candidate in _TOOL_CANDIDATES.get(logical_name, (logical_name,)):
        resolved = which(candidate)
        if resolved is not None:
            return str(Path(resolved).resolve())
    return None


def _probe_tool(logical_name: str, which: Callable[[str], str | None]) -> DoctorCheck:
    resolved = _resolve_tool(logical_name, which)
    if resolved is None:
        return DoctorCheck(
            name=logical_name,
            status="FAIL",
            resolved_path=None,
            tool_fingerprint=None,
            artifact_hash=None,
            message="required executable was not found on PATH",
        )

    executable = Path(resolved)
    arguments = _VERSION_ARGUMENTS.get(logical_name, ("--version",))
    try:
        completed = subprocess.run(
            (resolved, *arguments),
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return DoctorCheck(
            name=logical_name,
            status="FAIL",
            resolved_path=resolved,
            tool_fingerprint=None,
            artifact_hash=None,
            message=f"version probe failed: {error}",
        )

    version_output = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    if completed.returncode != 0 or not version_output:
        return DoctorCheck(
            name=logical_name,
            status="FAIL",
            resolved_path=resolved,
            tool_fingerprint=None,
            artifact_hash=None,
            message=f"version probe returned exit code {completed.returncode}",
        )

    version = version_output[0].strip()
    fingerprint = ToolFingerprint(
        tool_id=logical_name,
        executable=resolved,
        version=version,
        build_hash=_sha256_file(
            executable,
            suffix=completed.stdout.encode() + completed.stderr.encode(),
        ),
        adapter_version="bootstrap-doctor-v1",
        container_digest=None,
    )
    return DoctorCheck(
        name=logical_name,
        status="PASS",
        resolved_path=resolved,
        tool_fingerprint=fingerprint,
        artifact_hash=None,
        message=f"available: {version}",
    )


def _check_platform_lock(platform_lock: Path) -> DoctorCheck:
    resolved = platform_lock.resolve()
    if not resolved.is_file():
        return DoctorCheck(
            name="platform_lock",
            status="FAIL",
            resolved_path=str(resolved),
            tool_fingerprint=None,
            artifact_hash=None,
            message="platform lock does not exist or is not a regular file",
        )

    artifact_hash = _sha256_file(resolved)
    return DoctorCheck(
        name="platform_lock",
        status="PASS",
        resolved_path=str(resolved),
        tool_fingerprint=None,
        artifact_hash=artifact_hash,
        message="platform lock is present and fingerprinted",
    )


def run_doctor(
    required_tools: Sequence[str] = DEFAULT_REQUIRED_TOOLS,
    platform_lock: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> DoctorReport:
    """Check every requested dependency and return a complete immutable report."""

    checks = tuple(_probe_tool(tool, which) for tool in required_tools)
    if platform_lock is not None:
        checks += (_check_platform_lock(platform_lock),)

    status = "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    platform_lock_hash = checks[-1].artifact_hash if platform_lock is not None else None
    return DoctorReport(
        status=status,
        checks=checks,
        platform_lock_hash=platform_lock_hash,
        generated_at=datetime.now(UTC),
        exit_code=0 if status == "PASS" else 2,
    )
