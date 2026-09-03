"""Common shell-free execution, raw evidence sealing, and adapter result finalization."""

from __future__ import annotations

import math
import os
import re
import resource
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import field_validator, model_validator

from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.contracts.base import (
    METRIC_VALUE_FIELDS,
    ArtifactRef,
    Diagnostic,
    EntityId,
    MetricSet,
    NonNegativeInt,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.execution import (
    EnvironmentName,
    PreparedCommand,
    RawToolResult,
    ResourceUsage,
    Stage,
    StageResult,
    StageStatus,
    ToolJob,
)
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.orchestrator.scheduler import (
    BoundedScheduler,
    JobHandle,
    JobRequest,
    WorkerTermination,
)

_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_INTEGER = re.compile(r"^[+-]?\d+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_FIXED_ENVIRONMENT_NAMES = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"})


class AdapterError(RuntimeError):
    """Base error for adapter preparation, execution, or evidence finalization."""


class ToolFingerprintMismatchError(AdapterError):
    """The executable or version probe differs from the locked fingerprint."""


class AdapterPreparationError(AdapterError):
    """A job cannot be staged into a fresh isolated working directory."""


class ReportParseError(ValueError):
    """A required report section is missing or malformed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AdapterInvocation(StrictContract):
    """Tool-specific argv tail, output inventory, and reviewed environment additions."""

    argv_tail: tuple[str, ...]
    expected_output_paths: tuple[str, ...]
    environment: dict[EnvironmentName, str]

    @field_validator("argv_tail")
    @classmethod
    def argv_is_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("adapter argv entries must be nonempty and NUL-free")
        return value

    @field_validator("expected_output_paths")
    @classmethod
    def outputs_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("expected output paths must be unique")
        for item in value:
            path = PurePosixPath(item)
            if (
                not item
                or path.is_absolute()
                or ".." in path.parts
                or "." in path.parts
                or item != path.as_posix()
            ):
                raise ValueError("expected output paths must be normalized and relative")
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_explicit_and_safe(
        cls, value: dict[EnvironmentName, str]
    ) -> dict[EnvironmentName, str]:
        invalid_names = sorted(name for name in value if not _ENVIRONMENT_NAME.fullmatch(name))
        if invalid_names:
            raise ValueError(f"invalid environment variable names: {', '.join(invalid_names)}")
        reserved = sorted(set(value) & _FIXED_ENVIRONMENT_NAMES)
        if reserved:
            raise ValueError(
                f"adapter environment cannot override fixed variables: {', '.join(reserved)}"
            )
        if any("\x00" in item for item in value.values()):
            raise ValueError("adapter environment values must be NUL-free")
        return value


class AdapterParseContext(StrictContract):
    """Identity and runtime facts required by a pure golden-report parser."""

    stage: Stage
    analysis_view_id: EntityId | None
    runtime_ms: NonNegativeInt
    evidence_artifact_id: EntityId


class ParsedAdapterResult(StrictContract):
    """Semantic parser output before immutable job and raw-artifact binding."""

    status: StageStatus
    metrics: MetricSet
    diagnostics: tuple[Diagnostic, ...]

    @model_validator(mode="after")
    def status_has_required_diagnostics(self) -> Self:
        if self.status == "PASS" and self.diagnostics:
            raise ValueError("passing parser result cannot contain diagnostics")
        if self.status != "PASS" and not self.diagnostics:
            raise ValueError("non-pass parser result requires diagnostics")
        return self


class ToolAdapter(Protocol):
    """Adapter capability used by the durable orchestrator execution boundary."""

    artifact_store: ArtifactStore

    def fingerprint(self) -> ToolFingerprint: ...

    def prepare(self, job: ToolJob) -> PreparedCommand: ...

    def run(self, command: PreparedCommand) -> RawToolResult: ...

    def parse(self, raw: RawToolResult) -> StageResult: ...

    def validate(self, result: StageResult) -> Sequence[Diagnostic]: ...


def metric_set(
    context: AdapterParseContext,
    **values: int | float | None,
) -> MetricSet:
    """Construct a complete MetricSet with explicit reasons for every absent metric."""

    unknown = sorted(set(values) - METRIC_VALUE_FIELDS)
    if unknown:
        raise ValueError(f"unknown metric fields: {', '.join(unknown)}")
    metrics: dict[str, int | float | None] = {
        field_name: None for field_name in METRIC_VALUE_FIELDS
    }
    metrics.update(values)
    missing = {
        field_name: f"not produced by {context.stage} adapter"
        for field_name, value in metrics.items()
        if value is None
    }
    return MetricSet(
        analysis_view_id=context.analysis_view_id,
        **metrics,
        runtime_ms=context.runtime_ms,
        missing_metric_reasons=missing,
    )


def diagnostic(
    context: AdapterParseContext,
    *,
    code: str,
    message: str,
    severity: Literal["INFO", "WARNING", "ERROR", "FATAL"] = "ERROR",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        message=message,
        evidence_refs=(context.evidence_artifact_id,),
    )


def infrastructure_result(
    context: AdapterParseContext,
    *,
    code: str,
    message: str,
) -> ParsedAdapterResult:
    return ParsedAdapterResult(
        status="INFRASTRUCTURE_ERROR",
        metrics=metric_set(context),
        diagnostics=(diagnostic(context, code=code, message=message, severity="FATAL"),),
    )


def summary_fields(text: str, *, start: str, end: str) -> dict[str, str]:
    """Extract an exact delimited key/value section with duplicate rejection."""

    lines = text.splitlines()
    try:
        start_index = lines.index(start)
        end_index = lines.index(end, start_index + 1)
    except ValueError as error:
        raise ReportParseError(
            "INFRASTRUCTURE_MISSING_REPORT_SECTION",
            f"required report boundary is missing: {start} .. {end}",
        ) from error
    if end_index <= start_index + 1:
        raise ReportParseError(
            "INFRASTRUCTURE_MISSING_REPORT_SECTION",
            f"required report section is empty: {start}",
        )
    fields: dict[str, str] = {}
    for line in lines[start_index + 1 : end_index]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                f"malformed report field: {line}",
            )
        key, value = (item.strip() for item in line.split(":", maxsplit=1))
        if not key or not value or key in fields:
            raise ReportParseError(
                "INFRASTRUCTURE_MALFORMED_REPORT",
                f"invalid or duplicate report field: {line}",
            )
        fields[key] = value
    return fields


def required_field(fields: dict[str, str], name: str) -> str:
    try:
        return fields[name]
    except KeyError as error:
        raise ReportParseError(
            "INFRASTRUCTURE_MISSING_REPORT_SECTION",
            f"required report field is missing: {name}",
        ) from error


def finite_float(fields: dict[str, str], name: str) -> float:
    value = required_field(fields, name)
    if not _NUMBER.fullmatch(value):
        raise ReportParseError(
            "INFRASTRUCTURE_MALFORMED_REPORT",
            f"report field {name} is not a finite number: {value}",
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ReportParseError(
            "INFRASTRUCTURE_MALFORMED_REPORT",
            f"report field {name} is not finite: {value}",
        )
    return parsed


def strict_int(fields: dict[str, str], name: str) -> int:
    value = required_field(fields, name)
    if not _INTEGER.fullmatch(value):
        raise ReportParseError(
            "INFRASTRUCTURE_MALFORMED_REPORT",
            f"report field {name} is not an integer: {value}",
        )
    return int(value)


def _semantic_artifact_id(stage_result_id: str, role: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", role.lower()).strip("_") or "artifact"
    candidate = f"{stage_result_id}_{normalized}"
    if len(candidate) <= 96:
        return candidate
    suffix = sha256(candidate.encode()).hexdigest()[:12]
    return f"{candidate[:83]}_{suffix}"


class BaseToolAdapter(ABC):
    """Shared stateful adapter implementing preparation, execution, and finalization."""

    expected_tool_ids: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        tool_fingerprint: ToolFingerprint,
        artifact_store: ArtifactStore,
        workspace_root: Path,
        invocation: AdapterInvocation,
    ) -> None:
        if self.expected_tool_ids and tool_fingerprint.tool_id not in self.expected_tool_ids:
            expected = ", ".join(sorted(self.expected_tool_ids))
            raise ValueError(
                f"adapter expected tool fingerprint in {{{expected}}}, got "
                f"{tool_fingerprint.tool_id}"
            )
        self._tool_fingerprint = tool_fingerprint
        self.artifact_store = artifact_store
        self.workspace_root = workspace_root.resolve()
        self.invocation = invocation
        self._jobs: dict[str, ToolJob] = {}
        self.last_prepared_command: PreparedCommand | None = None
        self.last_raw_result: RawToolResult | None = None
        self.last_stage_result_artifact: ArtifactRef | None = None
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[bytes] | None = None

    def fingerprint(self) -> ToolFingerprint:
        return self._tool_fingerprint

    def verify_fingerprint(self) -> None:
        executable = Path(self._tool_fingerprint.executable)
        try:
            before = executable.stat()
            executable_bytes = executable.read_bytes()
        except OSError as error:
            message = f"tool executable cannot be read: {error}"
            raise ToolFingerprintMismatchError(message) from error
        observed_hash = f"sha256:{sha256(executable_bytes).hexdigest()}"
        if observed_hash != self._tool_fingerprint.executable_sha256:
            raise ToolFingerprintMismatchError("tool executable SHA-256 differs from fingerprint")
        probe_environment = {
            "HOME": str(self.workspace_root.parent),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": f"{executable.parent}:{os.defpath}",
            **self.invocation.environment,
        }
        try:
            completed = subprocess.run(
                (str(executable), *self._tool_fingerprint.version_args),
                shell=False,
                check=False,
                capture_output=True,
                timeout=10,
                env=probe_environment,
            )
            after = executable.stat()
            after_bytes = executable.read_bytes()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolFingerprintMismatchError(f"tool version probe failed: {error}") from error
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or after_bytes != executable_bytes:
            raise ToolFingerprintMismatchError("tool executable changed during fingerprint probe")
        version_lines = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
        if completed.returncode != 0 or not version_lines:
            raise ToolFingerprintMismatchError("tool version probe did not complete successfully")
        try:
            version = version_lines[0].decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ToolFingerprintMismatchError("tool version is not valid UTF-8") from error
        if version != self._tool_fingerprint.version:
            raise ToolFingerprintMismatchError("tool version differs from fingerprint")
        probe_bytes = executable_bytes + completed.stdout + completed.stderr
        build_hash = f"sha256:{sha256(probe_bytes).hexdigest()}"
        if build_hash != self._tool_fingerprint.build_hash:
            raise ToolFingerprintMismatchError("tool build hash differs from fingerprint")

    def prepare(self, job: ToolJob) -> PreparedCommand:
        working_directory = (self.workspace_root / job.run_id / job.stage_result_id).resolve()
        if self.workspace_root not in working_directory.parents:
            raise AdapterPreparationError("job working directory escapes workspace root")
        if working_directory.exists():
            raise AdapterPreparationError(
                f"isolated job working directory already exists: {working_directory}"
            )
        inputs_directory = working_directory / "inputs"
        temporary_directory = working_directory / "tmp"
        inputs_directory.mkdir(parents=True)
        temporary_directory.mkdir()
        for artifact in job.input_artifact_refs:
            data = self.artifact_store.open_verified(artifact).read()
            (inputs_directory / artifact.artifact_id).write_bytes(data)

        argv = (self._tool_fingerprint.executable, *self.invocation.argv_tail)
        recipe_payload = {
            "schema_version": 1,
            "tool_job_id": job.tool_job_id,
            "stage_result_id": job.stage_result_id,
            "argv": argv,
            "input_artifact_ids": tuple(item.artifact_id for item in job.input_artifact_refs),
            "expected_output_paths": self.invocation.expected_output_paths,
        }
        recipe_ref = self.artifact_store.put_named_bytes(
            canonical_json_bytes(recipe_payload),
            artifact_id=_semantic_artifact_id(job.stage_result_id, "recipe"),
            media_type="application/json",
            classification="INTERNAL",
            producer_stage_result_id=job.stage_result_id,
        )
        environment = {
            "HOME": str(working_directory),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": f"{Path(self._tool_fingerprint.executable).parent}:{os.defpath}",
            "TMPDIR": str(temporary_directory),
            **self.invocation.environment,
        }
        payload = {
            "schema_version": 1,
            "tool_job_id": job.tool_job_id,
            "stage_result_id": job.stage_result_id,
            "tool_fingerprint": self._tool_fingerprint.model_dump(mode="json"),
            "argv": argv,
            "working_directory": str(working_directory),
            "environment": environment,
            "resource_limits": job.resource_limits.model_dump(mode="json"),
            "deadline": job.deadline.isoformat().replace("+00:00", "Z"),
            "staged_input_artifact_refs": tuple(
                item.model_dump(mode="json") for item in job.input_artifact_refs
            ),
            "recipe_artifact_refs": (recipe_ref.model_dump(mode="json"),),
        }
        command = PreparedCommand(
            schema_version=1,
            tool_job_id=job.tool_job_id,
            stage_result_id=job.stage_result_id,
            tool_fingerprint=self._tool_fingerprint,
            argv=argv,
            working_directory=working_directory,
            environment=environment,
            resource_limits=job.resource_limits,
            deadline=job.deadline,
            staged_input_artifact_refs=job.input_artifact_refs,
            recipe_artifact_refs=(recipe_ref,),
            preparation_hash=canonical_sha256(payload),
        )
        self._jobs[job.stage_result_id] = job
        self.last_prepared_command = command
        return command

    @staticmethod
    def _limit_child(command: PreparedCommand) -> None:
        limits = command.resource_limits
        cpu_seconds = max(1, math.ceil(limits.wall_time_ms / 1000))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (limits.max_output_bytes, limits.max_output_bytes),
        )
        if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
            available = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, available[: limits.cpu_cores])

    def _put_raw(
        self,
        command: PreparedCommand,
        role: str,
        data: bytes,
        media_type: str,
    ) -> ArtifactRef:
        return self.artifact_store.put_named_bytes(
            data,
            artifact_id=_semantic_artifact_id(command.stage_result_id, role),
            media_type=media_type,
            classification="INTERNAL",
            producer_stage_result_id=command.stage_result_id,
        )

    def run(self, command: PreparedCommand) -> RawToolResult:
        prepared_ref = self._put_raw(
            command,
            "prepared_command",
            canonical_json_bytes(command),
            "application/json",
        )
        argv_ref = self._put_raw(
            command,
            "argv",
            canonical_json_bytes({"argv": command.argv}),
            "application/json",
        )
        stdout_path = command.working_directory / ".nova-stdout.raw"
        stderr_path = command.working_directory / ".nova-stderr.raw"
        started_at = datetime.now(UTC)
        remaining_deadline = (command.deadline - started_at).total_seconds()
        timeout_seconds = min(
            command.resource_limits.wall_time_ms / 1000,
            remaining_deadline,
        )
        timed_out = timeout_seconds <= 0
        process: subprocess.Popen[bytes] | None = None
        before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        start_clock = time.monotonic()
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            if not timed_out:
                process = subprocess.Popen(
                    command.argv,
                    cwd=command.working_directory,
                    env=command.environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=lambda: self._limit_child(command),
                )
                with self._process_lock:
                    self._active_process = process
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                finally:
                    with self._process_lock:
                        self._active_process = None
        wall_time_ms = max(0, round((time.monotonic() - start_clock) * 1000))
        ended_at = datetime.now(UTC)
        after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        stdout_bytes = stdout_path.read_bytes()
        stderr_bytes = stderr_path.read_bytes()
        stdout_ref = self._put_raw(command, "stdout", stdout_bytes, "text/plain")
        stderr_ref = self._put_raw(command, "stderr", stderr_bytes, "text/plain")
        output_refs: list[ArtifactRef] = []
        for relative_path in self.invocation.expected_output_paths:
            output_path = (command.working_directory / relative_path).resolve()
            if command.working_directory not in output_path.parents:
                raise AdapterError(f"declared output escapes working directory: {relative_path}")
            if output_path.is_file() and not output_path.is_symlink():
                output_refs.append(
                    self._put_raw(
                        command,
                        relative_path.replace(".", "_"),
                        output_path.read_bytes(),
                        "application/octet-stream",
                    )
                )
        return_code = process.returncode if process is not None else -signal.SIGKILL
        observed_signal = -return_code if return_code < 0 else None
        exit_code = 128 + observed_signal if observed_signal is not None else return_code
        raw_artifacts = (
            stdout_ref,
            stderr_ref,
            argv_ref,
            prepared_ref,
            *command.recipe_artifact_refs,
            *output_refs,
        )
        raw = RawToolResult(
            tool_job_id=command.tool_job_id,
            stage_result_id=command.stage_result_id,
            tool_fingerprint=command.tool_fingerprint,
            prepared_command_artifact=prepared_ref,
            prepared_command_hash=prepared_ref.sha256,
            exit_code=exit_code,
            signal=observed_signal,
            timed_out=timed_out,
            started_at=started_at,
            ended_at=ended_at,
            resource_usage=ResourceUsage(
                cpu_time_ms=max(
                    0,
                    round(
                        (
                            after_usage.ru_utime
                            + after_usage.ru_stime
                            - before_usage.ru_utime
                            - before_usage.ru_stime
                        )
                        * 1000
                    ),
                ),
                wall_time_ms=wall_time_ms,
                peak_rss_bytes=max(0, after_usage.ru_maxrss * 1024),
            ),
            raw_artifacts=raw_artifacts,
        )
        self.last_raw_result = raw
        return raw

    def terminate_active_process(self) -> bool:
        """Terminate the active process group for scheduler cancellation."""

        with self._process_lock:
            process = self._active_process
        if process is None:
            return True
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    def _raw_bytes(self, raw: RawToolResult, role: str) -> bytes:
        suffix = f"_{role}"
        reference = next(
            (item for item in raw.raw_artifacts if item.artifact_id.endswith(suffix)),
            None,
        )
        if reference is None:
            raise AdapterError(f"raw result does not contain {role} artifact")
        return self.artifact_store.open_verified(reference).read()

    @abstractmethod
    def parse_report(
        self,
        text: str,
        *,
        exit_code: int,
        context: AdapterParseContext,
    ) -> ParsedAdapterResult:
        """Parse tool-specific raw text into a semantic outcome."""

    def parse(self, raw: RawToolResult) -> StageResult:
        job = self._jobs[raw.stage_result_id]
        stdout = self._raw_bytes(raw, "stdout")
        stderr = self._raw_bytes(raw, "stderr")
        evidence = next(
            item for item in raw.raw_artifacts if item.artifact_id.endswith("_stdout")
        )
        context = AdapterParseContext(
            stage=job.stage,
            analysis_view_id=job.analysis_view_id,
            runtime_ms=raw.resource_usage.wall_time_ms,
            evidence_artifact_id=evidence.artifact_id,
        )
        if raw.timed_out:
            outcome = infrastructure_result(
                context,
                code="INFRASTRUCTURE_TOOL_TIMEOUT",
                message="tool execution exceeded its wall-time or absolute deadline",
            )
        else:
            text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
            outcome = self.parse_report(text, exit_code=raw.exit_code, context=context)
            if raw.signal is not None and outcome.status == "PASS":
                outcome = infrastructure_result(
                    context,
                    code="INFRASTRUCTURE_TOOL_CRASH",
                    message=f"tool terminated by signal {raw.signal}",
                )
            elif raw.exit_code != 0 and outcome.status == "PASS":
                message = (
                    f"tool returned exit code {raw.exit_code} without "
                    "design-failure evidence"
                )
                outcome = infrastructure_result(
                    context,
                    code="INFRASTRUCTURE_TOOL_EXIT",
                    message=message,
                )
        return StageResult(
            stage_result_id=job.stage_result_id,
            run_id=job.run_id,
            candidate_id=job.candidate_id,
            stage=job.stage,
            analysis_view_id=job.analysis_view_id,
            status=outcome.status,
            tool_fingerprint=raw.tool_fingerprint,
            input_hashes=job.input_hashes,
            metrics=outcome.metrics,
            diagnostics=outcome.diagnostics,
            raw_artifacts=raw.raw_artifacts,
            started_at=raw.started_at,
            ended_at=raw.ended_at,
        )

    def validate(self, result: StageResult) -> Sequence[Diagnostic]:
        """Return additional adapter diagnostics after canonical contract validation."""

        return ()


def finalize_validated_stage_result(
    adapter: BaseToolAdapter,
    result: StageResult,
    diagnostics: Sequence[Diagnostic],
    artifact_store: ArtifactStore,
) -> StageResult:
    """Verify every raw artifact, fold validation diagnostics, and persist StageResult."""

    artifact_errors: list[str] = []
    for artifact in result.raw_artifacts:
        try:
            artifact_store.open_verified(artifact)
        except ArtifactStoreError as error:
            artifact_errors.append(str(error))
    extra = tuple(diagnostics)
    if artifact_errors:
        evidence_id = result.raw_artifacts[0].artifact_id
        extra = (
            *extra,
            Diagnostic(
                code="INFRASTRUCTURE_RAW_ARTIFACT_UNRESOLVABLE",
                severity="FATAL",
                message="; ".join(artifact_errors),
                evidence_refs=(evidence_id,),
            ),
        )
    if extra:
        payload = result.model_dump(mode="python")
        payload["status"] = "INFRASTRUCTURE_ERROR"
        payload["diagnostics"] = (*result.diagnostics, *extra)
        result = StageResult.model_validate(payload)
    result_ref = artifact_store.put_named_bytes(
        canonical_json_bytes(result),
        artifact_id=_semantic_artifact_id(result.stage_result_id, "stage_result"),
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=result.stage_result_id,
    )
    adapter.last_stage_result_artifact = result_ref
    return result


def execute_tool_job(
    adapter: BaseToolAdapter,
    job: ToolJob,
    artifact_store: ArtifactStore,
) -> StageResult:
    """Execute one exact-fingerprint tool job and return a canonical M1 StageResult."""

    if adapter.artifact_store is not artifact_store:
        raise ValueError("adapter and executor must use the same artifact store instance")
    adapter.verify_fingerprint()
    command = adapter.prepare(job)
    raw = adapter.run(command)
    parsed = adapter.parse(raw)
    diagnostics = adapter.validate(parsed)
    return finalize_validated_stage_result(adapter, parsed, diagnostics, artifact_store)


class ScheduledToolWorker:
    """Thread-backed WorkerControl bridge for the existing bounded scheduler."""

    def __init__(
        self,
        adapter: BaseToolAdapter,
        job: ToolJob,
        artifact_store: ArtifactStore,
    ) -> None:
        self.adapter = adapter
        self.job = job
        self.artifact_store = artifact_store
        self._thread: threading.Thread | None = None
        self._result: StageResult | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("scheduled tool worker can start only once")
        self._thread = threading.Thread(
            target=self._execute,
            name=f"nova-{self.job.tool_job_id}",
            daemon=True,
        )
        self._thread.start()

    def _execute(self) -> None:
        try:
            self._result = execute_tool_job(self.adapter, self.job, self.artifact_store)
        except BaseException as error:
            self._error = error

    def wait(self) -> StageResult:
        if self._thread is None:
            raise RuntimeError("scheduled tool worker has not started")
        self._thread.join()
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    def terminate_and_wait(self) -> WorkerTermination:
        terminated = self.adapter.terminate_active_process()
        if self._thread is not None:
            self._thread.join(timeout=5)
            terminated = terminated and not self._thread.is_alive()
        partial = (
            self.adapter.last_raw_result.raw_artifacts
            if self.adapter.last_raw_result is not None
            else ()
        )
        return WorkerTermination(terminated=terminated, partial_log_artifacts=partial)


class ScheduledToolExecution:
    """One scheduler handle paired with its typed tool-result future."""

    def __init__(self, handle: JobHandle, worker: ScheduledToolWorker) -> None:
        self.handle = handle
        self.worker = worker

    def result(self) -> StageResult:
        try:
            result = self.worker.wait()
        except BaseException:
            self.handle.cancel(
                partial_log_artifacts=(
                    self.worker.adapter.last_raw_result.raw_artifacts
                    if self.worker.adapter.last_raw_result is not None
                    else ()
                )
            )
            raise
        self.handle.complete(partial_log_artifacts=result.raw_artifacts)
        return result


def schedule_tool_job(
    scheduler: BoundedScheduler,
    adapter: BaseToolAdapter,
    job: ToolJob,
    artifact_store: ArtifactStore,
    *,
    retry_depth: int = 0,
) -> ScheduledToolExecution:
    """Submit a ToolJob through M1's durable bounded scheduler."""

    kind = {
        "OPENROAD_PHYSICAL": "OPENROAD",
        "OPENROAD_PLACED_CTS": "OPENROAD",
        "OPENROAD_ROUTED": "OPENROAD",
        "FORMAL_MODEL_PREFLIGHT": "FORMAL",
        "EQY_SMOKE": "FORMAL",
        "FORMAL_EQUIVALENCE": "FORMAL",
    }.get(job.stage, "SYNTHESIS_STA")
    worker = ScheduledToolWorker(adapter, job, artifact_store)
    handle = scheduler.submit(
        JobRequest(
            job_id=job.tool_job_id,
            run_id=job.run_id,
            kind=kind,
            cpu_cores=job.resource_limits.cpu_cores,
            memory_bytes=job.resource_limits.memory_bytes,
            wall_time_ms=job.resource_limits.wall_time_ms,
            candidate_cost=0,
            token_cost=0,
            retry_depth=retry_depth,
            deadline=job.deadline,
            worker=worker,
        )
    )
    return ScheduledToolExecution(handle, worker)


__all__ = [
    "AdapterError",
    "AdapterInvocation",
    "AdapterParseContext",
    "AdapterPreparationError",
    "BaseToolAdapter",
    "ParsedAdapterResult",
    "ReportParseError",
    "ScheduledToolExecution",
    "ScheduledToolWorker",
    "ToolAdapter",
    "ToolFingerprintMismatchError",
    "diagnostic",
    "execute_tool_job",
    "finite_float",
    "infrastructure_result",
    "metric_set",
    "required_field",
    "schedule_tool_job",
    "strict_int",
    "summary_fields",
]
