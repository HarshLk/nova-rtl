"""Deterministic bootstrap checks for the NOVA-RTL toolchain."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from nova_rtl.contracts.platform import DoctorCheck, DoctorIssue, DoctorReport, PlatformLock
from nova_rtl.platform.lock import verify_platform_lock
from nova_rtl.platform.probe import ToolProbeError, probe_executable, resolve_tool

DEFAULT_REQUIRED_TOOLS = ("yosys", "opensta", "openroad", "eqy", "sby")


def _probe_tool(logical_name: str, which: Callable[[str], str | None]) -> DoctorCheck:
    executable = resolve_tool(logical_name, which)
    resolved = str(executable) if executable is not None else None
    if resolved is None:
        return DoctorCheck(
            name=logical_name,
            status="FAIL",
            resolved_path=None,
            tool_fingerprint=None,
            artifact_hash=None,
            issues=(
                DoctorIssue(
                    code="TOOL_MISSING",
                    subject=logical_name,
                    message="required executable was not found on PATH",
                ),
            ),
            message="required executable was not found on PATH",
        )

    try:
        fingerprint = probe_executable(logical_name, executable)
    except ToolProbeError as error:
        return DoctorCheck(
            name=logical_name,
            status="FAIL",
            resolved_path=resolved,
            tool_fingerprint=None,
            artifact_hash=None,
            issues=(
                DoctorIssue(
                    code="TOOL_PROBE_FAILED",
                    subject=logical_name,
                    message=str(error),
                ),
            ),
            message=f"version probe failed: {error}",
        )

    return DoctorCheck(
        name=logical_name,
        status="PASS",
        resolved_path=resolved,
        tool_fingerprint=fingerprint,
        artifact_hash=None,
        issues=(),
        message=f"available: {fingerprint.version}",
    )


def _check_platform_lock(platform_lock: Path) -> tuple[DoctorCheck, PlatformLock | None]:
    try:
        resolved = platform_lock.resolve()
    except (OSError, RuntimeError) as error:
        issue = DoctorIssue(
            code="PLATFORM_LOCK_IO_ERROR",
            subject="platform_lock",
            message=f"platform lock path could not be resolved: {error}",
        )
        return (
            DoctorCheck(
                name="platform_lock",
                status="FAIL",
                resolved_path=str(platform_lock.absolute()),
                tool_fingerprint=None,
                artifact_hash=None,
                issues=(issue,),
                message=issue.message,
            ),
            None,
        )
    if not resolved.is_file():
        return (
            DoctorCheck(
                name="platform_lock",
                status="FAIL",
                resolved_path=str(resolved),
                tool_fingerprint=None,
                artifact_hash=None,
                issues=(
                    DoctorIssue(
                        code="PLATFORM_LOCK_MISSING",
                        subject="platform_lock",
                        message="platform lock does not exist or is not a regular file",
                    ),
                ),
                message="platform lock does not exist or is not a regular file",
            ),
            None,
        )

    verification = verify_platform_lock(resolved)
    artifact_hash = verification.lock_hash
    if verification.status == "FAIL":
        details = "; ".join(
            f"{issue.code}:{issue.subject}: {issue.message}"
            for issue in verification.issues
        )
        return (
            DoctorCheck(
                name="platform_lock",
                status="FAIL",
                resolved_path=str(resolved),
                tool_fingerprint=None,
                artifact_hash=artifact_hash,
                issues=tuple(
                    DoctorIssue(
                        code=issue.code,
                        subject=issue.subject,
                        message=issue.message,
                    )
                    for issue in verification.issues
                ),
                message=f"platform lock verification failed: {details}",
            ),
            verification.lock,
        )
    return (
        DoctorCheck(
            name="platform_lock",
            status="PASS",
            resolved_path=str(resolved),
            tool_fingerprint=None,
            artifact_hash=artifact_hash,
            issues=(),
            message="platform lock is present and fingerprinted",
        ),
        verification.lock,
    )


def _correlate_live_tools(
    checks: tuple[DoctorCheck, ...],
    lock: PlatformLock,
) -> tuple[DoctorCheck, ...]:
    locked_by_id = {item.tool_id: item for item in lock.tool_fingerprints}
    correlated: list[DoctorCheck] = []
    for check in checks:
        expected = locked_by_id.get(check.name)
        actual = check.tool_fingerprint
        if check.status == "PASS" and expected is None:
            issue = DoctorIssue(
                code="TOOL_LOCK_MISMATCH",
                subject=check.name,
                message="required live tool is absent from the platform lock",
            )
            check = check.model_copy(
                update={
                    "status": "FAIL",
                    "issues": (issue,),
                    "message": issue.message,
                }
            )
        elif check.status == "PASS" and actual != expected:
            issue = DoctorIssue(
                code="TOOL_LOCK_MISMATCH",
                subject=check.name,
                message="live probe fingerprint does not match the platform lock",
            )
            check = check.model_copy(
                update={
                    "status": "FAIL",
                    "issues": (issue,),
                    "message": issue.message,
                }
            )
        correlated.append(check)
    return tuple(correlated)


def run_doctor(
    required_tools: Sequence[str] = DEFAULT_REQUIRED_TOOLS,
    platform_lock: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> DoctorReport:
    """Check every requested dependency and return a complete immutable report."""

    checks = tuple(_probe_tool(tool, which) for tool in required_tools)
    if platform_lock is not None:
        lock_check, verified_lock = _check_platform_lock(platform_lock)
        if verified_lock is not None:
            checks = _correlate_live_tools(checks, verified_lock)
        checks += (lock_check,)

    status = "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    platform_lock_hash = checks[-1].artifact_hash if platform_lock is not None else None
    return DoctorReport(
        status=status,
        checks=checks,
        platform_lock_hash=platform_lock_hash,
        generated_at=datetime.now(UTC),
        exit_code=0 if status == "PASS" else 2,
    )
