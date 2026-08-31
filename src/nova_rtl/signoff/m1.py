"""Commit-bound M1 sign-off evidence generation and verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import pytest
from pydantic import Field, ValidationError, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    StrictContract,
    UtcDatetime,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.platform import SignoffEvidenceFile
from nova_rtl.contracts.schema_export import (
    SchemaDriftError,
    check_all_schemas,
    export_all_schemas,
)


class M1SignoffError(RuntimeError):
    """Raised when M1 evidence cannot be generated or verified safely."""


@dataclass(frozen=True)
class M1SignoffRequest:
    """Committed M1 inputs and the new immutable packet destination."""

    project_root: Path
    schema_directory: Path
    replay_fixture: Path
    output_directory: Path
    python_executable: Path = Path(sys.executable)


@dataclass(frozen=True)
class _GitCheckpoint:
    commit: str
    tree_hash: str


class M1CommandEvidence(StrictContract):
    """One required command and its immutable combined output."""

    evidence_id: EntityId
    argv: tuple[str, ...] = Field(min_length=1)
    exit_code: Literal[0]
    output: SignoffEvidenceFile

    @field_validator("argv")
    @classmethod
    def arguments_are_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in value):
            raise ValueError("command arguments must be nonempty")
        return value


class M1SignoffReport(StrictContract):
    """Machine-readable index for the complete M1 evidence packet."""

    schema_version: Literal[1] = 1
    milestone: Literal["M1"]
    status: Literal["PASS"]
    generated_at: UtcDatetime
    implementation_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    implementation_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    python_executable: str = Field(min_length=1)
    python_executable_sha256: HashRef
    python_version: str = Field(min_length=1)
    pytest_version: str = Field(min_length=1)
    schema_export_digest_first: HashRef
    schema_export_digest_second: HashRef
    schema_files: tuple[SignoffEvidenceFile, ...] = Field(min_length=1)
    schema_test: M1CommandEvidence
    artifact_corruption_test: M1CommandEvidence
    replay_test: M1CommandEvidence
    m1_regression_test: M1CommandEvidence
    replay_first: M1CommandEvidence
    replay_second: M1CommandEvidence
    replay_digest: HashRef
    signoff_invocation_hash: HashRef

    @model_validator(mode="after")
    def evidence_is_complete_and_deterministic(self) -> Self:
        if self.schema_export_digest_first != self.schema_export_digest_second:
            raise ValueError("schema exports must be byte-identical")
        schema_paths = [item.relative_path for item in self.schema_files]
        if schema_paths != sorted(schema_paths):
            raise ValueError("schema evidence must use deterministic path ordering")
        if any(
            not path.startswith("schemas/") or not path.endswith(".schema.json")
            for path in schema_paths
        ):
            raise ValueError("schema evidence paths must name versioned packet schemas")
        commands = (
            self.schema_test,
            self.artifact_corruption_test,
            self.replay_test,
            self.m1_regression_test,
            self.replay_first,
            self.replay_second,
        )
        expected_ids = (
            "schema_tests",
            "artifact_corruption_test",
            "replay_tests",
            "m1_regression_tests",
            "replay_first",
            "replay_second",
        )
        if tuple(command.evidence_id for command in commands) != expected_ids:
            raise ValueError("M1 command evidence set or order is invalid")
        evidence_paths = schema_paths + [command.output.relative_path for command in commands]
        if len(evidence_paths) != len(set(evidence_paths)):
            raise ValueError("M1 evidence paths must be unique")
        return self


def m1_signoff_invocation_hash(report: M1SignoffReport) -> HashRef:
    """Hash every semantic sign-off input while excluding time and self-identity."""

    return canonical_sha256(
        report,
        exclude=frozenset({"generated_at", "signoff_invocation_hash"}),
    )


def _hash_file(path: Path) -> HashRef:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _evidence_file(path: Path, relative_path: str) -> SignoffEvidenceFile:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size <= 0:
        raise M1SignoffError(f"required M1 evidence is missing or empty: {path}")
    return SignoffEvidenceFile(
        relative_path=relative_path,
        sha256=_hash_file(resolved),
        size_bytes=resolved.stat().st_size,
    )


def _packet_file(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise M1SignoffError(f"unsafe M1 evidence path: {relative_path}")
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise M1SignoffError(f"M1 evidence traverses a symlink: {relative_path}")
    if not candidate.is_file():
        raise M1SignoffError(f"M1 evidence is not a regular file: {relative_path}")
    return candidate


def _report_evidence(report: M1SignoffReport) -> tuple[SignoffEvidenceFile, ...]:
    commands = (
        report.schema_test,
        report.artifact_corruption_test,
        report.replay_test,
        report.m1_regression_test,
        report.replay_first,
        report.replay_second,
    )
    return (*report.schema_files, *(command.output for command in commands))


def _require_exact_packet_entries(root: Path, expected_files: set[str]) -> None:
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise M1SignoffError(f"M1 packet contains a symlink: {relative}")
            actual_directories.add(relative)
        for name in file_names:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise M1SignoffError(f"M1 packet contains a symlink: {relative}")
            actual_files.add(relative)
    expected_directories = {
        parent.as_posix()
        for relative_path in expected_files
        for parent in PurePosixPath(relative_path).parents
        if parent.as_posix() != "."
    }
    if actual_files != expected_files or actual_directories != expected_directories:
        raise M1SignoffError(
            "M1 packet entries differ from the evidence index; "
            f"unexpected={sorted(actual_files - expected_files)}, "
            f"missing={sorted(expected_files - actual_files)}"
        )


def _schema_manifest_hash(schema_files: tuple[SignoffEvidenceFile, ...]) -> HashRef:
    payload = [item.model_dump(mode="json") for item in schema_files]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _git_query(project_root: Path, *arguments: str) -> str:
    git = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
    if git is None:
        raise M1SignoffError("Git is required to verify the M1 checkpoint")
    completed = subprocess.run(
        [git, "-C", str(project_root), *arguments],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise M1SignoffError(f"Git checkpoint query failed: {detail or completed.returncode}")
    return completed.stdout.strip()


def _capture_git_checkpoint(project_root: Path) -> _GitCheckpoint:
    root = project_root.resolve(strict=True)
    if _git_query(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise M1SignoffError("repository working tree is not clean at the M1 checkpoint")
    commit = _git_query(root, "rev-parse", "--verify", "HEAD")
    tree_hash = _git_query(root, "rev-parse", "--verify", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(
        r"[0-9a-f]{40}", tree_hash
    ):
        raise M1SignoffError("Git checkpoint did not produce full SHA-1 identities")
    return _GitCheckpoint(commit=commit, tree_hash=tree_hash)


def _require_ignored_output(project_root: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(project_root)
    except ValueError:
        return
    git = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
    if git is None:
        raise M1SignoffError("Git is required to validate the M1 output boundary")
    completed = subprocess.run(
        [git, "-C", str(project_root), "check-ignore", "-q", "--", str(relative)],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise M1SignoffError("M1 output inside the repository must be Git-ignored")


def _execution_environment(python_executable: Path) -> dict[str, str]:
    return {
        "PATH": f"{python_executable.parent}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


def _run_evidence_command(
    *,
    evidence_id: str,
    argv: tuple[str, ...],
    project_root: Path,
    staging: Path,
    environment: dict[str, str],
    timeout_seconds: int = 300,
) -> M1CommandEvidence:
    try:
        completed = subprocess.run(
            argv,
            cwd=project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise M1SignoffError(f"M1 command {evidence_id} could not execute: {error}") from error
    output = completed.stdout or b"<no output>\n"
    output_path = staging / "reports" / f"{evidence_id}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output)
    if completed.returncode != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        raise M1SignoffError(
            f"M1 command {evidence_id} failed with exit code {completed.returncode}: {detail}"
        )
    return M1CommandEvidence(
        evidence_id=evidence_id,
        argv=argv,
        exit_code=0,
        output=_evidence_file(output_path, f"reports/{evidence_id}.txt"),
    )


def _replay_digest(command: M1CommandEvidence, staging: Path) -> HashRef:
    output = _packet_file(staging, command.output.relative_path).read_text(encoding="utf-8")
    value = output.removesuffix("\n")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise M1SignoffError(f"{command.evidence_id} did not emit exactly one replay digest")
    return value


def _verify_git_object(project_root: Path, report: M1SignoffReport) -> None:
    root = project_root.resolve(strict=True)
    commit = _git_query(root, "rev-parse", "--verify", f"{report.implementation_commit}^{{commit}}")
    tree = _git_query(root, "rev-parse", "--verify", f"{report.implementation_commit}^{{tree}}")
    if commit != report.implementation_commit or tree != report.implementation_tree_hash:
        raise M1SignoffError("M1 packet does not match its recorded Git checkpoint")


def verify_m1_signoff_packet(
    packet_directory: Path,
    *,
    project_root: Path | None = None,
) -> M1SignoffReport:
    """Fail closed when any packet byte, identity, or deterministic result changed."""

    try:
        root = packet_directory.resolve(strict=True)
        report_path = _packet_file(root, "m1-signoff.json")
        report = M1SignoffReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        evidence = _report_evidence(report)
        _require_exact_packet_entries(
            root,
            {"m1-signoff.json", *(item.relative_path for item in evidence)},
        )
        for item in evidence:
            path = _packet_file(root, item.relative_path)
            if path.stat().st_size != item.size_bytes or _hash_file(path) != item.sha256:
                raise M1SignoffError(f"M1 evidence identity mismatch: {item.relative_path}")
        schema_digest = _schema_manifest_hash(report.schema_files)
        if schema_digest not in {
            report.schema_export_digest_first,
            report.schema_export_digest_second,
        }:
            raise M1SignoffError("M1 schema manifest disagrees with exported schema evidence")
        for item in report.schema_files:
            json.loads(_packet_file(root, item.relative_path).read_text(encoding="utf-8"))
        expected_replay_output = f"{report.replay_digest}\n"
        for command in (report.replay_first, report.replay_second):
            actual = _packet_file(root, command.output.relative_path).read_text(encoding="utf-8")
            if actual != expected_replay_output:
                raise M1SignoffError("M1 replay digest output is inconsistent")
        if report.signoff_invocation_hash != m1_signoff_invocation_hash(report):
            raise M1SignoffError("M1 sign-off invocation identity is incoherent")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", report.replay_digest):
            raise M1SignoffError("M1 replay digest is malformed")
        if project_root is not None:
            _verify_git_object(project_root, report)
    except M1SignoffError:
        raise
    except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise M1SignoffError(f"M1 sign-off packet verification failed: {error}") from error
    return report


def run_m1_signoff(
    request: M1SignoffRequest,
    *,
    generated_at: UtcDatetime | None = None,
) -> tuple[Path, M1SignoffReport]:
    """Execute every M1 gate and atomically publish its compact evidence packet."""

    project_root = request.project_root.resolve(strict=True)
    schema_directory = request.schema_directory.resolve(strict=True)
    replay_fixture = request.replay_fixture.resolve(strict=True)
    destination = request.output_directory.resolve(strict=False)
    python_executable = request.python_executable.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise M1SignoffError(f"M1 sign-off destination already exists: {destination}")
    if not schema_directory.is_dir():
        raise M1SignoffError("M1 schema directory is not a directory")
    if not replay_fixture.is_dir():
        raise M1SignoffError("M1 replay fixture is not a directory")
    if python_executable != Path(sys.executable).resolve(strict=True):
        raise M1SignoffError("M1 sign-off must use the active Python interpreter")
    _require_ignored_output(project_root, destination)
    checkpoint = _capture_git_checkpoint(project_root)
    timestamp = generated_at or datetime.now(UTC)
    environment = _execution_environment(python_executable)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".m1-signoff-", dir=destination.parent))
    try:
        export_first = staging / ".schema-export-first"
        export_second = staging / ".schema-export-second"
        first_paths = export_all_schemas(export_first)
        second_paths = export_all_schemas(export_second)
        check_all_schemas(schema_directory)
        first_bytes = {path.name: path.read_bytes() for path in first_paths}
        second_bytes = {path.name: path.read_bytes() for path in second_paths}
        committed_bytes = {
            path.name: path.read_bytes()
            for path in sorted(schema_directory.glob("*.schema.json"))
        }
        if first_bytes != second_bytes or first_bytes != committed_bytes:
            raise M1SignoffError("canonical schema exports are not byte-deterministic")
        schema_files: list[SignoffEvidenceFile] = []
        for name, content in sorted(first_bytes.items()):
            output_path = staging / "schemas" / name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)
            schema_files.append(_evidence_file(output_path, f"schemas/{name}"))
        schema_evidence = tuple(schema_files)
        schema_digest_first = _schema_manifest_hash(schema_evidence)
        second_evidence = tuple(
            SignoffEvidenceFile(
                relative_path=f"schemas/{name}",
                sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
                size_bytes=len(content),
            )
            for name, content in sorted(second_bytes.items())
        )
        schema_digest_second = _schema_manifest_hash(second_evidence)
        python = str(python_executable)
        schema_test = _run_evidence_command(
            evidence_id="schema_tests",
            argv=(python, "-m", "pytest", "tests/unit/contracts", "-q"),
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        artifact_corruption_test = _run_evidence_command(
            evidence_id="artifact_corruption_test",
            argv=(
                python,
                "-m",
                "pytest",
                "tests/unit/artifacts/test_store.py::"
                "test_open_verified_fails_closed_for_corruption_and_missing_blob",
                "-q",
            ),
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        replay_test = _run_evidence_command(
            evidence_id="replay_tests",
            argv=(
                python,
                "-m",
                "pytest",
                "tests/unit/artifacts/test_replay.py::"
                "test_replay_uses_sequence_and_makes_no_model_or_tool_calls",
                "tests/unit/artifacts/test_replay.py::"
                "test_replay_digest_is_deterministic_for_the_verified_event_stream",
                "-q",
            ),
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        m1_regression_test = _run_evidence_command(
            evidence_id="m1_regression_tests",
            argv=(
                python,
                "-m",
                "pytest",
                "tests/unit/contracts",
                "tests/unit/artifacts",
                "tests/unit/orchestrator",
                "tests/unit/analysis_views",
                "-q",
            ),
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        replay_argv = (
            python,
            "-m",
            "nova_rtl.artifacts.replay",
            str(replay_fixture),
            "--digest",
        )
        replay_first = _run_evidence_command(
            evidence_id="replay_first",
            argv=replay_argv,
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        replay_second = _run_evidence_command(
            evidence_id="replay_second",
            argv=replay_argv,
            project_root=project_root,
            staging=staging,
            environment=environment,
        )
        replay_digest_first = _replay_digest(replay_first, staging)
        replay_digest_second = _replay_digest(replay_second, staging)
        if replay_digest_first != replay_digest_second:
            raise M1SignoffError("repeated M1 replay produced different digests")
        final_checkpoint = _capture_git_checkpoint(project_root)
        if final_checkpoint != checkpoint:
            raise M1SignoffError("Git implementation checkpoint changed during M1 sign-off")
        report = M1SignoffReport(
            milestone="M1",
            status="PASS",
            generated_at=timestamp,
            implementation_commit=checkpoint.commit,
            implementation_tree_hash=checkpoint.tree_hash,
            python_executable=python,
            python_executable_sha256=_hash_file(python_executable),
            python_version=platform.python_version(),
            pytest_version=pytest.__version__,
            schema_export_digest_first=schema_digest_first,
            schema_export_digest_second=schema_digest_second,
            schema_files=schema_evidence,
            schema_test=schema_test,
            artifact_corruption_test=artifact_corruption_test,
            replay_test=replay_test,
            m1_regression_test=m1_regression_test,
            replay_first=replay_first,
            replay_second=replay_second,
            replay_digest=replay_digest_first,
            signoff_invocation_hash=f"sha256:{'0' * 64}",
        )
        report = report.model_copy(
            update={"signoff_invocation_hash": m1_signoff_invocation_hash(report)}
        )
        shutil.rmtree(export_first)
        shutil.rmtree(export_second)
        report_path = staging / "m1-signoff.json"
        report_path.write_bytes(canonical_json_bytes(report) + b"\n")
        verify_m1_signoff_packet(staging, project_root=project_root)
        os.rename(staging, destination)
        staging = destination
        verified = verify_m1_signoff_packet(destination, project_root=project_root)
        return destination / "m1-signoff.json", verified
    except (OSError, SchemaDriftError, ValueError) as error:
        if isinstance(error, M1SignoffError):
            raise
        raise M1SignoffError(f"M1 sign-off failed: {error}") from error
    finally:
        if staging.exists() and staging != destination:
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "M1CommandEvidence",
    "M1SignoffError",
    "M1SignoffReport",
    "M1SignoffRequest",
    "m1_signoff_invocation_hash",
    "run_m1_signoff",
    "verify_m1_signoff_packet",
]
