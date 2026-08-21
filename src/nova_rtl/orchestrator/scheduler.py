"""Persistent separate-queue scheduler for bounded EDA and planner work."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    NonNegativeInt,
    StrictContract,
    UtcDatetime,
    canonical_json_bytes,
    canonical_sha256,
)

JobKind = Literal["SYNTHESIS_STA", "FORMAL", "OPENROAD", "PLANNER"]
JobStatus = Literal[
    "QUEUED",
    "DISPATCHING",
    "RUNNING",
    "CANCELLING",
    "QUARANTINED",
    "COMPLETED",
    "CANCELLED",
    "INFRASTRUCTURE_ERROR",
]
_ACTIVE_STATUSES = ("DISPATCHING", "RUNNING", "CANCELLING", "QUARANTINED")
_NONTERMINAL_STATUSES = ("QUEUED", *_ACTIVE_STATUSES)
_ENTITY_ID = re.compile(r"^[a-z][a-z0-9_]{2,95}$")


class WorkerTermination(StrictContract):
    terminated: bool
    partial_log_artifacts: tuple[ArtifactRef, ...]


class WorkerControl(Protocol):
    """Recoverable worker capability owned only by the scheduler."""

    def start(self) -> None: ...

    def terminate_and_wait(self) -> WorkerTermination: ...


class SchedulerError(RuntimeError):
    """Base error for scheduling and durable job bookkeeping."""


class BudgetExceededError(SchedulerError):
    """A request exceeds a locked resource, count, or deadline budget."""


class SchedulerPolicyMismatchError(SchedulerError):
    """A resumed scheduler supplied a policy different from persisted policy."""


class JobStateError(SchedulerError):
    """A job operation conflicts with its durable lifecycle state."""


class JobKindLimits(StrictContract):
    max_concurrency: int = Field(strict=True, gt=0)
    cpu_cores: int = Field(strict=True, gt=0)
    memory_bytes: int = Field(strict=True, gt=0)
    wall_time_ms: int = Field(strict=True, gt=0)


class SchedulerPolicy(StrictContract):
    kind_limits: dict[JobKind, JobKindLimits]
    max_candidates: int = Field(strict=True, gt=0)
    max_tokens: int = Field(strict=True, gt=0)
    max_retry_depth: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def all_queues_are_explicit(self) -> SchedulerPolicy:
        required = {"SYNTHESIS_STA", "FORMAL", "OPENROAD", "PLANNER"}
        if set(self.kind_limits) != required:
            raise ValueError("scheduler policy must define every separate job queue")
        return self


@dataclass(frozen=True)
class JobRequest:
    job_id: EntityId
    run_id: EntityId
    kind: JobKind
    cpu_cores: int
    memory_bytes: int
    wall_time_ms: int
    candidate_cost: int
    token_cost: int
    retry_depth: int
    deadline: datetime
    worker: WorkerControl | None = None


class BudgetUsage(StrictContract):
    candidate_count: NonNegativeInt
    token_count: NonNegativeInt


class JobRecord(StrictContract):
    job_id: EntityId
    run_id: EntityId
    kind: JobKind
    status: JobStatus
    cpu_cores: int = Field(strict=True, gt=0)
    memory_bytes: int = Field(strict=True, gt=0)
    wall_time_ms: int = Field(strict=True, gt=0)
    candidate_cost: NonNegativeInt
    token_cost: NonNegativeInt
    retry_depth: NonNegativeInt
    deadline: UtcDatetime
    partial_log_artifacts: tuple[ArtifactRef, ...]
    submitted_at: UtcDatetime
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    error_code: str | None


class SchedulerEventRecord(StrictContract):
    event_index: int = Field(strict=True, gt=0)
    job_id: EntityId
    event_type: Literal[
        "QUEUED",
        "DISPATCHING",
        "RUNNING",
        "CANCELLATION_REQUESTED",
        "QUARANTINED",
        "COMPLETED",
        "CANCELLED",
        "INFRASTRUCTURE_ERROR",
    ]
    timestamp: UtcDatetime
    partial_log_artifacts: tuple[ArtifactRef, ...]
    error_code: str | None


class JobHandle:
    """Capability-limited terminal control for one admitted job."""

    def __init__(self, scheduler: BoundedScheduler, job_id: EntityId) -> None:
        self._scheduler = scheduler
        self.job_id = job_id

    @property
    def record(self) -> JobRecord:
        return self._scheduler.job(self.job_id)

    def complete(self, *, partial_log_artifacts: tuple[ArtifactRef, ...] = ()) -> None:
        self._scheduler._finish(
            self.job_id,
            status="COMPLETED",
            partial_log_artifacts=partial_log_artifacts,
            error_code=None,
        )

    def cancel(self, *, partial_log_artifacts: tuple[ArtifactRef, ...] = ()) -> None:
        self._scheduler._cancel(
            self.job_id, partial_log_artifacts=partial_log_artifacts
        )


WorkerResolver = Callable[[JobRecord], WorkerControl | None]


class BoundedScheduler:
    """Persist, dispatch, cancel, and reconcile jobs under immutable budgets."""

    def __init__(
        self,
        database_path: Path,
        policy: SchedulerPolicy,
        *,
        worker_resolver: WorkerResolver | None = None,
    ) -> None:
        self.database_path = database_path.resolve()
        self.policy = policy
        self._workers: dict[str, WorkerControl] = {}
        self._worker_resolver = worker_resolver
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._reconcile_nonterminal()

    def submit(self, job: JobRequest) -> JobHandle:
        """Persist a queue entry and reserve cumulative run costs exactly once."""

        now = datetime.now(UTC)
        self._validate_request(job, now)
        with self._write_connection() as connection:
            if connection.execute(
                "SELECT 1 FROM scheduler_jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone():
                raise JobStateError(f"job already exists: {job.job_id}")
            consumed = self._consumed_on_connection(connection, job.run_id)
            if consumed.candidate_count + job.candidate_cost > self.policy.max_candidates:
                raise BudgetExceededError("run candidate budget exceeded")
            if consumed.token_count + job.token_cost > self.policy.max_tokens:
                raise BudgetExceededError("run token budget exceeded")
            connection.execute(
                """
                INSERT INTO scheduler_jobs(
                    job_id, run_id, kind, status, cpu_cores, memory_bytes,
                    wall_time_ms, candidate_cost, token_cost, retry_depth,
                    deadline, partial_logs_json, submitted_at, started_at,
                    ended_at, error_code
                ) VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    job.job_id,
                    job.run_id,
                    job.kind,
                    job.cpu_cores,
                    job.memory_bytes,
                    job.wall_time_ms,
                    job.candidate_cost,
                    job.token_cost,
                    job.retry_depth,
                    self._timestamp(job.deadline),
                    self._encode_artifacts(()),
                    self._timestamp(now),
                ),
            )
            self._append_event(connection, job.job_id, "QUEUED", now, (), None)
        if job.worker is not None:
            self._workers[job.job_id] = job.worker
        self.dispatch()
        return JobHandle(self, job.job_id)

    def attach_worker(self, job_id: EntityId, worker: WorkerControl) -> JobHandle:
        record = self.job(job_id)
        if record.status != "QUEUED":
            raise JobStateError(f"worker can attach only to a queued job: {job_id}")
        self._workers[job_id] = worker
        self.dispatch()
        return JobHandle(self, job_id)

    def handle(self, job_id: EntityId) -> JobHandle:
        self.job(job_id)
        return JobHandle(self, job_id)

    def dispatch(self) -> None:
        """Start eligible queue heads while preserving each kind's FIFO order."""

        while True:
            record = self._reserve_next_dispatch()
            if record is None:
                return
            worker = self._workers[record.job_id]
            try:
                worker.start()
            except Exception:
                termination = self._attempt_termination(worker)
                if termination is not None and termination.terminated:
                    self._finish(
                        record.job_id,
                        status="INFRASTRUCTURE_ERROR",
                        partial_log_artifacts=termination.partial_log_artifacts,
                        error_code="WORKER_START_FAILED",
                        expected_status="DISPATCHING",
                        dispatch_after=False,
                    )
                else:
                    logs = termination.partial_log_artifacts if termination else ()
                    self._quarantine(
                        record.job_id,
                        partial_log_artifacts=logs,
                        error_code="WORKER_START_TERMINATION_UNCONFIRMED",
                        expected_status="DISPATCHING",
                    )
                continue
            now = datetime.now(UTC)
            with self._write_connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE scheduler_jobs SET status = 'RUNNING', started_at = ?
                    WHERE job_id = ? AND status = 'DISPATCHING'
                    """,
                    (self._timestamp(now), record.job_id),
                )
                if cursor.rowcount != 1:
                    with suppress(Exception):
                        worker.terminate_and_wait()
                    raise JobStateError(
                        f"dispatch state changed before worker start commit: {record.job_id}"
                    )
                self._append_event(connection, record.job_id, "RUNNING", now, (), None)

    def enforce_deadlines(self, *, now: datetime | None = None) -> tuple[EntityId, ...]:
        """Cancel every queued/running job whose absolute or wall deadline expired."""

        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
            raise ValueError("deadline enforcement time must be timezone-aware UTC")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT job_id, deadline, started_at, wall_time_ms
                FROM scheduler_jobs
                WHERE status IN ('QUEUED', 'DISPATCHING', 'RUNNING')
                ORDER BY submit_index
                """
            ).fetchall()
        expired: list[str] = []
        for job_id, deadline, started_at, wall_time_ms in rows:
            absolute_expired = self._parse_timestamp(deadline) <= instant
            wall_expired = started_at is not None and (
                self._parse_timestamp(started_at) + timedelta(milliseconds=wall_time_ms)
                <= instant
            )
            if absolute_expired or wall_expired:
                cancelled = self._cancel(
                    job_id,
                    partial_log_artifacts=(),
                    error_code="DEADLINE_EXCEEDED",
                    skip_if_terminal=True,
                )
                if cancelled:
                    expired.append(job_id)
        return tuple(expired)

    def record_partial_logs(
        self, job_id: EntityId, artifacts: tuple[ArtifactRef, ...]
    ) -> None:
        """Persist verified log identities incrementally without releasing a lease."""

        self._validate_artifacts(artifacts)
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT status, partial_logs_json FROM scheduler_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row[0] not in _NONTERMINAL_STATUSES:
                raise JobStateError(f"job is missing or terminal: {job_id}")
            merged = self._merge_artifacts(self._decode_artifacts(row[1]), artifacts)
            connection.execute(
                "UPDATE scheduler_jobs SET partial_logs_json = ? WHERE job_id = ?",
                (self._encode_artifacts(merged), job_id),
            )

    def reconcile_quarantined(
        self, job_id: EntityId, worker: WorkerControl
    ) -> JobRecord:
        """Release a quarantined lease only after process death is confirmed."""

        record = self.job(job_id)
        if record.status != "QUARANTINED":
            raise JobStateError(f"job is not quarantined: {job_id}")
        self._workers[job_id] = worker
        termination = self._attempt_termination(worker)
        logs = self._merge_artifacts(
            record.partial_log_artifacts,
            termination.partial_log_artifacts if termination else (),
        )
        if termination is None or not termination.terminated:
            self._quarantine(
                job_id,
                partial_log_artifacts=logs,
                error_code="WORKER_TERMINATION_UNCONFIRMED",
                expected_status="QUARANTINED",
            )
            return self.job(job_id)
        self._finish(
            job_id,
            status="INFRASTRUCTURE_ERROR",
            partial_log_artifacts=logs,
            error_code=record.error_code or "QUARANTINED_WORKER_TERMINATED",
            expected_status="QUARANTINED",
        )
        return self.job(job_id)

    def consumed(self, run_id: EntityId) -> BudgetUsage:
        with self._read_connection() as connection:
            return self._consumed_on_connection(connection, run_id)

    def job(self, job_id: EntityId) -> JobRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                self._job_select() + " WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobStateError(f"unknown job: {job_id}")
        return self._job_record(row)

    def events(self, job_id: EntityId) -> tuple[SchedulerEventRecord, ...]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT event_index, job_id, event_type, timestamp,
                       partial_logs_json, error_code
                FROM scheduler_job_events WHERE job_id = ? ORDER BY event_index
                """,
                (job_id,),
            ).fetchall()
        return tuple(
            SchedulerEventRecord(
                event_index=int(index),
                job_id=stored_job_id,
                event_type=event_type,
                timestamp=timestamp,
                partial_log_artifacts=self._decode_artifacts(logs),
                error_code=error_code,
            )
            for index, stored_job_id, event_type, timestamp, logs, error_code in rows
        )

    def _reserve_next_dispatch(self) -> JobRecord | None:
        with self._write_connection() as connection:
            queued = connection.execute(
                self._job_select()
                + " WHERE status = 'QUEUED' ORDER BY submit_index ASC"
            ).fetchall()
            blocked_kinds: set[str] = set()
            for row in queued:
                record = self._job_record(row)
                if record.kind in blocked_kinds:
                    continue
                if record.job_id not in self._workers:
                    blocked_kinds.add(record.kind)
                    continue
                limits = self.policy.kind_limits[record.kind]
                count, cpu, memory = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(cpu_cores), 0),
                           COALESCE(SUM(memory_bytes), 0)
                    FROM scheduler_jobs
                    WHERE kind = ? AND status IN (
                        'DISPATCHING', 'RUNNING', 'CANCELLING', 'QUARANTINED'
                    )
                    """,
                    (record.kind,),
                ).fetchone()
                if (
                    int(count) >= limits.max_concurrency
                    or int(cpu) + record.cpu_cores > limits.cpu_cores
                    or int(memory) + record.memory_bytes > limits.memory_bytes
                ):
                    blocked_kinds.add(record.kind)
                    continue
                now = datetime.now(UTC)
                connection.execute(
                    """
                    UPDATE scheduler_jobs SET status = 'DISPATCHING'
                    WHERE job_id = ? AND status = 'QUEUED'
                    """,
                    (record.job_id,),
                )
                self._append_event(connection, record.job_id, "DISPATCHING", now, (), None)
                return record.model_copy(update={"status": "DISPATCHING"})
        return None

    def _cancel(
        self,
        job_id: EntityId,
        *,
        partial_log_artifacts: tuple[ArtifactRef, ...],
        error_code: str | None = None,
        skip_if_terminal: bool = False,
    ) -> bool:
        self._validate_artifacts(partial_log_artifacts)
        now = datetime.now(UTC)
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT status, partial_logs_json FROM scheduler_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row[0] not in _NONTERMINAL_STATUSES:
                if skip_if_terminal:
                    return False
                raise JobStateError(f"job is missing or terminal: {job_id}")
            if row[0] == "QUARANTINED":
                raise JobStateError(
                    f"quarantined job requires explicit reconciliation: {job_id}"
                )
            prior_status = row[0]
            merged_logs = self._merge_artifacts(
                self._decode_artifacts(row[1]), partial_log_artifacts
            )
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = 'CANCELLING', partial_logs_json = ?, error_code = ?
                WHERE job_id = ?
                """,
                (self._encode_artifacts(merged_logs), error_code, job_id),
            )
            self._append_event(
                connection,
                job_id,
                "CANCELLATION_REQUESTED",
                now,
                merged_logs,
                error_code,
            )
        if prior_status == "QUEUED":
            self._finish(
                job_id,
                status="CANCELLED",
                partial_log_artifacts=merged_logs,
                error_code=error_code,
                expected_status="CANCELLING",
            )
            return True
        worker = self._workers.get(job_id)
        termination = self._attempt_termination(worker) if worker is not None else None
        completed_logs = self._merge_artifacts(
            merged_logs,
            termination.partial_log_artifacts if termination else (),
        )
        if termination is None or not termination.terminated:
            self._quarantine(
                job_id,
                partial_log_artifacts=completed_logs,
                error_code="WORKER_TERMINATION_UNCONFIRMED",
                expected_status="CANCELLING",
            )
            return True
        self._finish(
            job_id,
            status="CANCELLED",
            partial_log_artifacts=completed_logs,
            error_code=error_code,
            expected_status="CANCELLING",
        )
        return True

    def _quarantine(
        self,
        job_id: EntityId,
        *,
        partial_log_artifacts: tuple[ArtifactRef, ...],
        error_code: str,
        expected_status: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = 'QUARANTINED', partial_logs_json = ?, error_code = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    self._encode_artifacts(partial_log_artifacts),
                    error_code,
                    job_id,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                raise JobStateError(f"job cannot enter quarantine: {job_id}")
            self._append_event(
                connection,
                job_id,
                "QUARANTINED",
                now,
                partial_log_artifacts,
                error_code,
            )

    def _finish(
        self,
        job_id: EntityId,
        *,
        status: Literal["COMPLETED", "CANCELLED", "INFRASTRUCTURE_ERROR"],
        partial_log_artifacts: tuple[ArtifactRef, ...],
        error_code: str | None,
        expected_status: str = "RUNNING",
        dispatch_after: bool = True,
    ) -> None:
        self._validate_artifacts(partial_log_artifacts)
        now = datetime.now(UTC)
        logs = self._encode_artifacts(partial_log_artifacts)
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_jobs
                SET status = ?, partial_logs_json = ?, ended_at = ?, error_code = ?
                WHERE job_id = ? AND status = ?
                """,
                (status, logs, self._timestamp(now), error_code, job_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise JobStateError(f"job is missing or has unexpected state: {job_id}")
            self._append_event(
                connection, job_id, status, now, partial_log_artifacts, error_code
            )
        self._workers.pop(job_id, None)
        if dispatch_after:
            self.dispatch()

    def _reconcile_nonterminal(self) -> None:
        with self._read_connection() as connection:
            rows = connection.execute(
                self._job_select()
                + " WHERE status IN ("
                + "'QUEUED', 'DISPATCHING', 'RUNNING', 'CANCELLING', 'QUARANTINED')"
                + " ORDER BY submit_index"
            ).fetchall()
        records = tuple(self._job_record(row) for row in rows)
        for record in records:
            worker = self._worker_resolver(record) if self._worker_resolver else None
            if worker is not None:
                self._workers[record.job_id] = worker
            if record.status == "DISPATCHING":
                termination = self._attempt_termination(worker) if worker else None
                logs = self._merge_artifacts(
                    record.partial_log_artifacts,
                    termination.partial_log_artifacts if termination else (),
                )
                if termination is not None and termination.terminated:
                    self._finish(
                        record.job_id,
                        status="INFRASTRUCTURE_ERROR",
                        partial_log_artifacts=logs,
                        error_code="INTERRUPTED_DURING_DISPATCH",
                        expected_status="DISPATCHING",
                        dispatch_after=False,
                    )
                else:
                    self._quarantine(
                        record.job_id,
                        partial_log_artifacts=logs,
                        error_code="INTERRUPTED_DISPATCH_WORKER_UNCONFIRMED",
                        expected_status="DISPATCHING",
                    )
            elif record.status == "RUNNING" and worker is None:
                self._quarantine(
                    record.job_id,
                    partial_log_artifacts=record.partial_log_artifacts,
                    error_code="WORKER_OWNERSHIP_LOST",
                    expected_status="RUNNING",
                )
            elif record.status == "CANCELLING":
                self._resume_cancellation(record, worker)
            elif record.status == "QUARANTINED" and worker is not None:
                self.reconcile_quarantined(record.job_id, worker)
        self.dispatch()

    def _resume_cancellation(
        self, record: JobRecord, worker: WorkerControl | None
    ) -> None:
        termination = self._attempt_termination(worker) if worker is not None else None
        logs = self._merge_artifacts(
            record.partial_log_artifacts,
            termination.partial_log_artifacts if termination else (),
        )
        if termination is None or not termination.terminated:
            self._quarantine(
                record.job_id,
                partial_log_artifacts=logs,
                error_code="WORKER_TERMINATION_UNCONFIRMED",
                expected_status="CANCELLING",
            )
            return
        self._finish(
            record.job_id,
            status="CANCELLED",
            partial_log_artifacts=logs,
            error_code=record.error_code,
            expected_status="CANCELLING",
            dispatch_after=False,
        )

    @staticmethod
    def _attempt_termination(worker: WorkerControl) -> WorkerTermination | None:
        try:
            return worker.terminate_and_wait()
        except Exception:
            return None

    @staticmethod
    def _merge_artifacts(
        first: tuple[ArtifactRef, ...], second: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        merged = {artifact.artifact_id: artifact for artifact in first}
        for artifact in second:
            prior = merged.get(artifact.artifact_id)
            if prior is not None and prior != artifact:
                raise JobStateError(
                    f"partial log artifact identity conflicts: {artifact.artifact_id}"
                )
            merged[artifact.artifact_id] = artifact
        return tuple(merged.values())

    def _validate_request(self, job: JobRequest, now: datetime) -> None:
        if not _ENTITY_ID.fullmatch(job.job_id) or not _ENTITY_ID.fullmatch(job.run_id):
            raise ValueError("job_id and run_id must be canonical entity IDs")
        if job.kind not in self.policy.kind_limits:
            raise ValueError(f"unknown job kind: {job.kind}")
        for name in ("cpu_cores", "memory_bytes", "wall_time_ms"):
            if not isinstance(getattr(job, name), int) or getattr(job, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("candidate_cost", "token_cost", "retry_depth"):
            if not isinstance(getattr(job, name), int) or getattr(job, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        limits = self.policy.kind_limits[job.kind]
        if job.cpu_cores > limits.cpu_cores:
            raise BudgetExceededError(f"{job.kind} CPU budget exceeded")
        if job.memory_bytes > limits.memory_bytes:
            raise BudgetExceededError(f"{job.kind} memory budget exceeded")
        if job.wall_time_ms > limits.wall_time_ms:
            raise BudgetExceededError(f"{job.kind} wall time budget exceeded")
        if job.retry_depth > self.policy.max_retry_depth:
            raise BudgetExceededError("retry depth budget exceeded")
        if job.deadline.tzinfo is None or job.deadline.utcoffset() != timedelta(0):
            raise ValueError("deadline must be timezone-aware UTC")
        if job.deadline <= now or job.deadline < now + timedelta(milliseconds=job.wall_time_ms):
            raise BudgetExceededError("job deadline cannot accommodate its wall time budget")

    @staticmethod
    def _validate_artifacts(artifacts: tuple[ArtifactRef, ...]) -> None:
        ids = tuple(item.artifact_id for item in artifacts)
        if len(ids) != len(set(ids)):
            raise ValueError("partial log artifact IDs must be unique")

    @staticmethod
    def _encode_artifacts(artifacts: tuple[ArtifactRef, ...]) -> str:
        return canonical_json_bytes(
            {"artifacts": [item.model_dump(mode="json") for item in artifacts]}
        ).decode()

    @staticmethod
    def _decode_artifacts(encoded: str) -> tuple[ArtifactRef, ...]:
        try:
            payload = json.loads(encoded)
            return tuple(ArtifactRef.model_validate(item) for item in payload["artifacts"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise JobStateError("persisted partial-log artifacts are invalid") from error

    @staticmethod
    def _consumed_on_connection(
        connection: sqlite3.Connection, run_id: str
    ) -> BudgetUsage:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(candidate_cost), 0), COALESCE(SUM(token_cost), 0)
            FROM scheduler_jobs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        return BudgetUsage(candidate_count=int(row[0]), token_count=int(row[1]))

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        timestamp: datetime,
        logs: tuple[ArtifactRef, ...],
        error_code: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO scheduler_job_events(
                job_id, event_type, timestamp, partial_logs_json, error_code
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                BoundedScheduler._timestamp(timestamp),
                BoundedScheduler._encode_artifacts(logs),
                error_code,
            ),
        )

    @staticmethod
    def _job_select() -> str:
        return """
            SELECT job_id, run_id, kind, status, cpu_cores, memory_bytes,
                   wall_time_ms, candidate_cost, token_cost, retry_depth,
                   deadline, partial_logs_json, submitted_at, started_at,
                   ended_at, error_code
            FROM scheduler_jobs
        """

    @staticmethod
    def _job_record(row: tuple[object, ...]) -> JobRecord:
        values = list(row)
        values[11] = BoundedScheduler._decode_artifacts(str(values[11]))
        return JobRecord.model_validate(
            dict(
                zip(
                    (
                        "job_id",
                        "run_id",
                        "kind",
                        "status",
                        "cpu_cores",
                        "memory_bytes",
                        "wall_time_ms",
                        "candidate_cost",
                        "token_cost",
                        "retry_depth",
                        "deadline",
                        "partial_log_artifacts",
                        "submitted_at",
                        "started_at",
                        "ended_at",
                        "error_code",
                    ),
                    values,
                    strict=True,
                )
            )
        )

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _write_connection(self) -> sqlite3.Connection:
        connection = self._read_connection()
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _initialize(self) -> None:
        with self._read_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    policy_hash TEXT NOT NULL,
                    canonical_policy BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_jobs (
                    submit_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cpu_cores INTEGER NOT NULL CHECK(cpu_cores > 0),
                    memory_bytes INTEGER NOT NULL CHECK(memory_bytes > 0),
                    wall_time_ms INTEGER NOT NULL CHECK(wall_time_ms > 0),
                    candidate_cost INTEGER NOT NULL CHECK(candidate_cost >= 0),
                    token_cost INTEGER NOT NULL CHECK(token_cost >= 0),
                    retry_depth INTEGER NOT NULL CHECK(retry_depth >= 0),
                    deadline TEXT NOT NULL,
                    partial_logs_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS scheduler_jobs_live_kind
                    ON scheduler_jobs(kind, status, submit_index);
                CREATE INDEX IF NOT EXISTS scheduler_jobs_run
                    ON scheduler_jobs(run_id, submit_index);
                CREATE TABLE IF NOT EXISTS scheduler_job_events (
                    event_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    partial_logs_json TEXT NOT NULL,
                    error_code TEXT,
                    FOREIGN KEY(job_id) REFERENCES scheduler_jobs(job_id)
                );
                CREATE TRIGGER IF NOT EXISTS scheduler_events_no_update
                BEFORE UPDATE ON scheduler_job_events
                BEGIN
                    SELECT RAISE(ABORT, 'scheduler events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS scheduler_events_no_delete
                BEFORE DELETE ON scheduler_job_events
                BEGIN
                    SELECT RAISE(ABORT, 'scheduler events are append-only');
                END;
                """
            )
            expected_hash = canonical_sha256(self.policy)
            policy_bytes = canonical_json_bytes(self.policy)
            row = connection.execute(
                "SELECT policy_hash, canonical_policy FROM scheduler_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO scheduler_metadata(singleton, policy_hash, canonical_policy)
                    VALUES (1, ?, ?)
                    """,
                    (expected_hash, policy_bytes),
                )
            elif row != (expected_hash, policy_bytes):
                raise SchedulerPolicyMismatchError(
                    "resumed scheduler policy differs from persisted policy"
                )


__all__ = [
    "BoundedScheduler",
    "BudgetExceededError",
    "BudgetUsage",
    "JobHandle",
    "JobKind",
    "JobKindLimits",
    "JobRecord",
    "JobRequest",
    "JobStateError",
    "SchedulerError",
    "SchedulerEventRecord",
    "SchedulerPolicy",
    "SchedulerPolicyMismatchError",
    "WorkerControl",
    "WorkerResolver",
    "WorkerTermination",
]
