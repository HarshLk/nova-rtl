"""Deterministic serialization and byte verification for platform locks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from nova_rtl.contracts.base import HashRef, canonical_json_bytes
from nova_rtl.contracts.platform import PlatformArtifact, PlatformLock


@dataclass(frozen=True)
class VerificationIssue:
    """One stable lock-integrity failure."""

    code: str
    subject: str
    message: str


@dataclass(frozen=True)
class PlatformLockVerification:
    """Complete result of verifying a lock and all referenced bytes."""

    status: Literal["PASS", "FAIL"]
    issues: tuple[VerificationIssue, ...]
    lock_hash: HashRef | None
    lock: PlatformLock | None


def hash_file(path: Path) -> HashRef:
    """Hash exact file bytes with SHA-256."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _hash_bytes(data: bytes) -> HashRef:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def platform_content_identity_hash(lock: PlatformLock) -> HashRef:
    """Hash identity-bearing lock fields, excluding its self-hash and timestamp."""

    payload = lock.model_dump(
        mode="json",
        exclude={"content_identity_hash", "generated_at"},
    )
    data = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _lock_format(suffix: str) -> Literal["JSON", "YAML"]:
    normalized = suffix.lower()
    if normalized == ".json":
        return "JSON"
    if normalized in {".yaml", ".yml"}:
        return "YAML"
    raise ValueError(f"unsupported platform-lock format: {suffix or '<none>'}")


def _serialized_lock_bytes(lock: PlatformLock, suffix: str) -> bytes:
    if _lock_format(suffix) == "YAML":
        text = yaml.safe_dump(
            lock.model_dump(mode="json"),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
        )
        return text.encode("utf-8")
    return canonical_json_bytes(lock) + b"\n"


def dump_platform_lock(lock: PlatformLock, path: Path) -> None:
    """Atomically write a platform lock using deterministic key ordering."""

    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = _serialized_lock_bytes(lock, destination.suffix)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_platform_lock(path: Path) -> PlatformLock:
    """Load and strictly validate a JSON or YAML platform lock."""

    return _load_platform_lock_bytes(path.read_bytes(), path.suffix)


def _load_platform_lock_bytes(data: bytes, suffix: str) -> PlatformLock:
    lock_format = _lock_format(suffix)
    text = data.decode("utf-8")
    payload = json.loads(text) if lock_format == "JSON" else yaml.safe_load(text)
    return PlatformLock.model_validate(payload)


def _platform_artifacts(lock: PlatformLock) -> tuple[PlatformArtifact, ...]:
    reference_liberties = (
        lock.reference_corner.liberty_files if lock.reference_corner is not None else ()
    )
    return (
        *lock.setup_corner.liberty_files,
        *lock.hold_corner.liberty_files,
        *reference_liberties,
        lock.tech_lef,
        *lock.cell_lefs,
        lock.rc_rules,
        lock.flow_config,
        *lock.license_artifacts,
    )


def _verify_artifact(artifact: PlatformArtifact) -> tuple[VerificationIssue, ...]:
    path = artifact.resolved_path
    if not path.is_file():
        return (
            VerificationIssue(
                code="ARTIFACT_MISSING",
                subject=artifact.artifact_id,
                message=f"platform artifact is missing: {path}",
            ),
        )
    issues: list[VerificationIssue] = []
    try:
        initial_stat = path.stat()
        observed_hash = hash_file(path)
        final_stat = path.stat()
    except OSError as error:
        return (
            VerificationIssue(
                code="ARTIFACT_IO_ERROR",
                subject=artifact.artifact_id,
                message=f"platform artifact could not be read: {path}: {error}",
            ),
        )
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(initial_stat, field) != getattr(final_stat, field) for field in identity_fields):
        return (
            VerificationIssue(
                code="ARTIFACT_CHANGED_DURING_VERIFY",
                subject=artifact.artifact_id,
                message=f"platform artifact changed while it was verified: {path}",
            ),
        )
    if final_stat.st_size != artifact.size_bytes:
        issues.append(
            VerificationIssue(
                code="ARTIFACT_SIZE_MISMATCH",
                subject=artifact.artifact_id,
                message=f"platform artifact size changed: {path}",
            )
        )
    if observed_hash != artifact.sha256:
        issues.append(
            VerificationIssue(
                code="ARTIFACT_HASH_MISMATCH",
                subject=artifact.artifact_id,
                message=f"platform artifact bytes changed: {path}",
            )
        )
    return tuple(issues)


def verify_platform_lock(lock_or_path: PlatformLock | Path) -> PlatformLockVerification:
    """Verify strict lock structure plus every referenced artifact and executable."""

    lock_hash: HashRef | None = None
    if isinstance(lock_or_path, Path):
        try:
            lock_bytes = lock_or_path.read_bytes()
        except OSError as error:
            return PlatformLockVerification(
                status="FAIL",
                issues=(
                    VerificationIssue(
                        code="PLATFORM_LOCK_IO_ERROR",
                        subject="platform_lock",
                        message=str(error),
                    ),
                ),
                lock_hash=None,
                lock=None,
            )
        lock_hash = _hash_bytes(lock_bytes)
    try:
        if isinstance(lock_or_path, Path):
            lock = _load_platform_lock_bytes(lock_bytes, lock_or_path.suffix)
        else:
            lock = lock_or_path
    except (ValueError, TypeError, yaml.YAMLError) as error:
        return PlatformLockVerification(
            status="FAIL",
            issues=(
                VerificationIssue(
                    code="INVALID_PLATFORM_LOCK",
                    subject="platform_lock",
                    message=str(error),
                ),
            ),
            lock_hash=lock_hash,
            lock=None,
        )

    issues: list[VerificationIssue] = []
    actual_content_identity = platform_content_identity_hash(lock)
    if actual_content_identity != lock.content_identity_hash:
        issues.append(
            VerificationIssue(
                code="CONTENT_IDENTITY_MISMATCH",
                subject=lock.platform_id,
                message="platform lock identity-bearing fields changed",
            )
        )
    for artifact in _platform_artifacts(lock):
        issues.extend(_verify_artifact(artifact))

    for fingerprint in lock.tool_fingerprints:
        executable = Path(fingerprint.executable)
        if not executable.is_file():
            issues.append(
                VerificationIssue(
                    code="TOOL_MISSING",
                    subject=fingerprint.tool_id,
                    message=f"locked executable is missing: {executable}",
                )
            )
            continue
        try:
            observed_executable_hash = hash_file(executable)
        except OSError as error:
            issues.append(
                VerificationIssue(
                    code="TOOL_IO_ERROR",
                    subject=fingerprint.tool_id,
                    message=f"locked executable could not be read: {executable}: {error}",
                )
            )
            continue
        if observed_executable_hash != fingerprint.executable_sha256:
            issues.append(
                VerificationIssue(
                    code="TOOL_EXECUTABLE_HASH_MISMATCH",
                    subject=fingerprint.tool_id,
                    message=f"locked executable bytes changed: {executable}",
                )
            )
            continue
    return PlatformLockVerification(
        status="PASS" if not issues else "FAIL",
        issues=tuple(issues),
        lock_hash=lock_hash,
        lock=lock,
    )


__all__ = [
    "PlatformLockVerification",
    "VerificationIssue",
    "dump_platform_lock",
    "hash_file",
    "load_platform_lock",
    "platform_content_identity_hash",
    "verify_platform_lock",
]
