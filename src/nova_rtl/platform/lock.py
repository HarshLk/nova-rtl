"""Deterministic serialization and byte verification for platform locks."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from nova_rtl.contracts.base import HashRef, canonical_json_bytes
from nova_rtl.contracts.platform import (
    PlatformAnalysisView,
    PlatformAnalysisViews,
    PlatformArtifact,
    PlatformLock,
    PlatformLockRequest,
    PlatformSelectionPolicy,
    TimingCorner,
    TimingCornerSelection,
)

MAX_LIBERTY_HEADER_BYTES = 4 * 1024 * 1024


class PlatformLockError(RuntimeError):
    """Raised when selected platform collateral cannot produce a trustworthy lock."""


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


def selection_policy_content_identity_hash(policy: PlatformSelectionPolicy) -> HashRef:
    """Hash reviewed policy content independently of YAML spelling."""

    return _hash_bytes(canonical_json_bytes(policy))


def load_platform_selection_policy(path: Path) -> PlatformSelectionPolicy:
    """Load and strictly validate a YAML or JSON platform selection policy."""

    suffix = path.suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise PlatformLockError(
            f"unsupported platform selection policy format: {path.suffix or '<none>'}"
        )
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
        return PlatformSelectionPolicy.model_validate(payload)
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        ValidationError,
        yaml.YAMLError,
    ) as error:
        raise PlatformLockError(f"invalid platform selection policy: {error}") from error


def _artifact(
    root: Path,
    logical_path: str,
    artifact_id: str,
    kind: str,
    *,
    missing_label: str,
) -> PlatformArtifact:
    candidate = root.joinpath(*logical_path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except FileNotFoundError as error:
        raise PlatformLockError(f"required {missing_label} is missing: {logical_path}") from error
    except (OSError, RuntimeError, ValueError) as error:
        raise PlatformLockError(
            f"selected platform artifact escapes the ORFS root: {logical_path}"
        ) from error
    if not resolved.is_file():
        raise PlatformLockError(f"required {missing_label} is not a regular file: {logical_path}")
    try:
        size = resolved.stat().st_size
        digest = hash_file(resolved)
    except OSError as error:
        raise PlatformLockError(
            f"required {missing_label} cannot be read: {logical_path}"
        ) from error
    return PlatformArtifact(
        artifact_id=artifact_id,
        kind=kind,
        logical_path=logical_path,
        resolved_path=resolved,
        sha256=digest,
        size_bytes=size,
    )


def _liberty_header(path: Path) -> str:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="strict") as stream:
                return stream.read(MAX_LIBERTY_HEADER_BYTES)
        with path.open("rt", encoding="utf-8", errors="strict") as stream:
            return stream.read(MAX_LIBERTY_HEADER_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        raise PlatformLockError(f"Liberty metadata cannot be read: {path}") from error


def _required_match(pattern: str, text: str, label: str, path: Path) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match is None:
        raise PlatformLockError(f"Liberty {label} is missing or unsupported: {path}")
    return match.group(1)


def _liberty_metadata(path: Path) -> tuple[str, float, float, Literal["PS", "NS"]]:
    text = _liberty_header(path)
    condition = _required_match(
        r"\boperating_conditions\s*\(\s*([A-Za-z0-9_.-]+)\s*\)",
        text,
        "operating condition",
        path,
    )
    voltage = float(
        _required_match(r"\bnom_voltage\s*:\s*([-+0-9.eE]+)\s*;", text, "voltage", path)
    )
    temperature = float(
        _required_match(
            r"\bnom_temperature\s*:\s*([-+0-9.eE]+)\s*;",
            text,
            "temperature",
            path,
        )
    )
    time_unit = _required_match(
        r'\btime_unit\s*:\s*"\s*1\s*(ps|ns)\s*"\s*;',
        text,
        "time unit",
        path,
    ).upper()
    if time_unit not in {"PS", "NS"}:
        raise PlatformLockError(f"unsupported Liberty time unit: {path}")
    return condition, voltage, temperature, time_unit


def _timing_corner(
    root: Path,
    selection: TimingCornerSelection,
    library_model: str,
) -> TimingCorner:
    artifacts = tuple(
        _artifact(
            root,
            path,
            f"{selection.corner_id}_lib_{index:02d}",
            "LIBERTY",
            missing_label=f"{selection.role} Liberty",
        )
        for index, path in enumerate(selection.liberty_files, start=1)
    )
    metadata = tuple(_liberty_metadata(item.resolved_path) for item in artifacts)
    physical_metadata = tuple((item[1], item[2], item[3]) for item in metadata)
    if len(set(physical_metadata)) != 1:
        raise PlatformLockError(
            f"selected {selection.role} Liberty files disagree on nominal PVT or time unit"
        )
    voltage, temperature, time_unit = physical_metadata[0]
    return TimingCorner(
        corner_id=selection.corner_id,
        role=selection.role,
        library_model=library_model,
        liberty_files=artifacts,
        operating_condition_mode="PER_LIBRARY_NOMINAL",
        operating_conditions=tuple(item[0] for item in metadata),
        voltage_v=voltage,
        temperature_c=temperature,
        native_time_unit=time_unit,
    )


def create_platform_lock(request: PlatformLockRequest) -> PlatformLock:
    """Discover selected ASAP7 bytes and seal one immutable local realization."""

    root = request.orfs_root
    policy = request.policy
    platform_directory = root.joinpath(*policy.platform_root.split("/"))
    if not platform_directory.is_dir():
        raise PlatformLockError(f"selected platform root is missing: {policy.platform_root}")
    setup_corner = _timing_corner(root, policy.setup_corner, policy.library_model)
    hold_corner = _timing_corner(root, policy.hold_corner, policy.library_model)
    reference_corner = (
        _timing_corner(root, policy.reference_corner, policy.library_model)
        if policy.reference_corner is not None
        else None
    )
    placeholder_hash = "sha256:" + "0" * 64
    lock = PlatformLock(
        schema_version=1,
        platform_id=policy.platform_id,
        source_manifest_hash=request.source_manifest_hash,
        selection_policy_hash=selection_policy_content_identity_hash(policy),
        orfs_commit=policy.orfs_commit,
        host=request.host,
        tool_fingerprints=request.tool_fingerprints,
        setup_corner=setup_corner,
        hold_corner=hold_corner,
        reference_corner=reference_corner,
        tech_lef=_artifact(
            root,
            policy.tech_lef,
            "asap7_tech_lef",
            "TECH_LEF",
            missing_label="technology LEF",
        ),
        cell_lefs=tuple(
            _artifact(
                root,
                path,
                f"asap7_cell_lef_{index:02d}",
                "CELL_LEF",
                missing_label="cell LEF",
            )
            for index, path in enumerate(policy.cell_lefs, start=1)
        ),
        rc_rules=_artifact(
            root,
            policy.rc_config,
            "asap7_rc_config",
            "RC_RULES",
            missing_label="RC config",
        ),
        flow_config=_artifact(
            root,
            policy.flow_config,
            "asap7_flow_config",
            "FLOW_CONFIG",
            missing_label="OpenROAD flow config",
        ),
        license_artifacts=tuple(
            _artifact(
                root,
                path,
                f"asap7_license_{index:02d}",
                "LICENSE",
                missing_label="license artifact",
            )
            for index, path in enumerate(policy.license_artifacts, start=1)
        ),
        redistribution_status=policy.redistribution_status,
        license_notes=policy.license_notes,
        deterministic_seed=policy.deterministic_seed,
        content_identity_hash=placeholder_hash,
        generated_at=request.generated_at,
    )
    return lock.model_copy(update={"content_identity_hash": platform_content_identity_hash(lock)})


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


def dump_analysis_views(lock: PlatformLock, lock_path: Path, views_path: Path) -> None:
    """Atomically materialize the two required views bound to exact lock bytes."""

    try:
        lock_hash = hash_file(lock_path)
    except OSError as error:
        raise PlatformLockError(f"platform lock cannot be hashed for views: {error}") from error
    views = PlatformAnalysisViews(
        schema_version=1,
        platform_id=lock.platform_id,
        platform_lock_hash=lock_hash,
        views=(
            PlatformAnalysisView(
                analysis_view_id="asap7_setup",
                check="SETUP",
                liberty_corner_id=lock.setup_corner.corner_id,
                liberty_artifact_hashes=tuple(
                    artifact.sha256 for artifact in lock.setup_corner.liberty_files
                ),
                rc_corner_id="asap7_rc",
                rc_artifact_hash=lock.rc_rules.sha256,
                operating_condition_mode=lock.setup_corner.operating_condition_mode,
                operating_conditions=lock.setup_corner.operating_conditions,
                required_stages=("OPENSTA_FULL", "OPENROAD_PHYSICAL"),
            ),
            PlatformAnalysisView(
                analysis_view_id="asap7_hold",
                check="HOLD",
                liberty_corner_id=lock.hold_corner.corner_id,
                liberty_artifact_hashes=tuple(
                    artifact.sha256 for artifact in lock.hold_corner.liberty_files
                ),
                rc_corner_id="asap7_rc",
                rc_artifact_hash=lock.rc_rules.sha256,
                operating_condition_mode=lock.hold_corner.operating_condition_mode,
                operating_conditions=lock.hold_corner.operating_conditions,
                required_stages=("OPENSTA_FULL", "OPENROAD_PHYSICAL"),
            ),
        ),
    )
    destination = views_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(
        views.model_dump(mode="json"),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    ).encode("utf-8")
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
    "PlatformLockError",
    "PlatformLockVerification",
    "VerificationIssue",
    "create_platform_lock",
    "dump_analysis_views",
    "dump_platform_lock",
    "hash_file",
    "load_platform_lock",
    "load_platform_selection_policy",
    "platform_content_identity_hash",
    "selection_policy_content_identity_hash",
    "verify_platform_lock",
]
