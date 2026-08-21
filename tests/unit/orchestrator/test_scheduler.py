from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nova_rtl.contracts.base import ArtifactRef
from nova_rtl.orchestrator.scheduler import (
    BoundedScheduler,
    BudgetExceededError,
    JobKindLimits,
    JobRequest,
    SchedulerPolicy,
    WorkerTermination,
)


class FakeWorker:
    def __init__(
        self,
        *,
        terminate_fails: bool = False,
        termination_logs: tuple[ArtifactRef, ...] = (),
    ) -> None:
        self.started = False
        self.terminated = False
        self.terminate_fails = terminate_fails
        self.termination_logs = termination_logs

    def start(self) -> None:
        self.started = True

    def terminate_and_wait(self) -> WorkerTermination:
        self.terminated = True
        if self.terminate_fails:
            raise RuntimeError("termination failed")
        return WorkerTermination(
            terminated=True, partial_log_artifacts=self.termination_logs
        )


def policy(*, max_tokens: int = 100) -> SchedulerPolicy:
    return SchedulerPolicy(
        kind_limits={
            "SYNTHESIS_STA": JobKindLimits(
                max_concurrency=1,
                cpu_cores=4,
                memory_bytes=4_000,
                wall_time_ms=10_000,
            ),
            "FORMAL": JobKindLimits(
                max_concurrency=1,
                cpu_cores=2,
                memory_bytes=2_000,
                wall_time_ms=20_000,
            ),
            "OPENROAD": JobKindLimits(
                max_concurrency=1,
                cpu_cores=8,
                memory_bytes=8_000,
                wall_time_ms=30_000,
            ),
            "PLANNER": JobKindLimits(
                max_concurrency=2,
                cpu_cores=1,
                memory_bytes=1_000,
                wall_time_ms=5_000,
            ),
        },
        max_candidates=2,
        max_tokens=max_tokens,
        max_retry_depth=1,
    )


def request(
    job_id: str,
    *,
    kind: str = "SYNTHESIS_STA",
    candidate_cost: int = 0,
    token_cost: int = 0,
    retry_depth: int = 0,
    worker: FakeWorker | None = None,
) -> JobRequest:
    return JobRequest(
        job_id=job_id,
        run_id="run_001",
        kind=kind,
        cpu_cores=1,
        memory_bytes=500,
        wall_time_ms=1_000,
        candidate_cost=candidate_cost,
        token_cost=token_cost,
        retry_depth=retry_depth,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        worker=worker or FakeWorker(),
    )


def artifact(artifact_id: str, digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        uri=f"artifact://logs/{artifact_id}",
        sha256=f"sha256:{digit * 64}",
        media_type="text/plain",
        size_bytes=10,
        created_at="2026-08-21T06:30:00Z",
        producer_stage_result_id=None,
        classification="INTERNAL",
    )


def test_scheduler_enforces_separate_per_kind_concurrency_queues(tmp_path: Path) -> None:
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    second_worker = FakeWorker()
    synth = scheduler.submit(request("job_synth_001"))
    formal = scheduler.submit(request("job_formal_001", kind="FORMAL"))
    queued = scheduler.submit(request("job_synth_002", worker=second_worker))

    assert synth.record.status == "RUNNING"
    assert formal.record.status == "RUNNING"
    assert queued.record.status == "QUEUED"
    assert not second_worker.started

    synth.complete()
    assert queued.record.status == "RUNNING"
    assert second_worker.started
    formal.complete()
    queued.complete()


def test_scheduler_rejects_resource_deadline_and_retry_overruns(tmp_path: Path) -> None:
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    base = request("job_001")

    with pytest.raises(BudgetExceededError, match="CPU"):
        scheduler.submit(replace(base, job_id="job_cpu", cpu_cores=5))
    with pytest.raises(BudgetExceededError, match="memory"):
        scheduler.submit(replace(base, job_id="job_memory", memory_bytes=5_000))
    with pytest.raises(BudgetExceededError, match="wall time"):
        scheduler.submit(replace(base, job_id="job_wall", wall_time_ms=11_000))
    with pytest.raises(BudgetExceededError, match="retry depth"):
        scheduler.submit(replace(base, job_id="job_retry", retry_depth=2))
    with pytest.raises(BudgetExceededError, match="deadline"):
        scheduler.submit(
            replace(base, job_id="job_deadline", deadline=datetime.now(UTC))
        )


def test_consumed_candidate_and_token_budgets_survive_resume(tmp_path: Path) -> None:
    database = tmp_path / "scheduler.sqlite3"
    scheduler = BoundedScheduler(database, policy(max_tokens=100))
    handle = scheduler.submit(
        request("job_planner_001", kind="PLANNER", candidate_cost=1, token_cost=80)
    )
    handle.complete()

    resumed = BoundedScheduler(database, policy(max_tokens=100))
    assert resumed.consumed("run_001").token_count == 80
    assert resumed.consumed("run_001").candidate_count == 1
    with pytest.raises(BudgetExceededError, match="token"):
        resumed.submit(
            request("job_planner_002", kind="PLANNER", candidate_cost=1, token_cost=21)
        )


def test_cancellation_terminates_worker_and_preserves_partial_log_identity(
    tmp_path: Path,
) -> None:
    worker = FakeWorker()
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    handle = scheduler.submit(request("job_001", worker=worker))

    logs = (artifact("artifact_stdout", "1"), artifact("artifact_stderr", "2"))
    handle.cancel(partial_log_artifacts=logs)

    assert worker.terminated
    record = scheduler.job("job_001")
    assert record.status == "CANCELLED"
    assert record.partial_log_artifacts == logs
    assert [event.event_type for event in scheduler.events("job_001")] == [
        "QUEUED",
        "DISPATCHING",
        "RUNNING",
        "CANCELLATION_REQUESTED",
        "CANCELLED",
    ]


def test_deadline_enforcement_terminates_worker_and_releases_queue(tmp_path: Path) -> None:
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    deadline_log = artifact("artifact_deadline_log", "3")
    first_worker = FakeWorker(termination_logs=(deadline_log,))
    second_worker = FakeWorker()
    first = scheduler.submit(request("job_001", worker=first_worker))
    second = scheduler.submit(request("job_002", worker=second_worker))
    incremental_log = artifact("artifact_incremental_log", "4")
    scheduler.record_partial_logs("job_001", (incremental_log,))

    expired = scheduler.enforce_deadlines(now=first.record.deadline)

    assert expired == ("job_001",)
    assert first_worker.terminated
    assert first.record.status == "CANCELLED"
    assert first.record.error_code == "DEADLINE_EXCEEDED"
    assert first.record.partial_log_artifacts == (incremental_log, deadline_log)
    assert second.record.status == "RUNNING"
    assert second_worker.started
    second.complete()


def test_deadline_sweep_skips_concurrently_completed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    first = scheduler.submit(request("job_001", kind="PLANNER"))
    second = scheduler.submit(request("job_002", kind="PLANNER"))
    original_cancel = scheduler._cancel
    raced = False

    def complete_before_cancel(job_id: str, **kwargs: object) -> bool:
        nonlocal raced
        if not raced:
            raced = True
            first.complete()
        return original_cancel(job_id, **kwargs)

    monkeypatch.setattr(scheduler, "_cancel", complete_before_cancel)
    expired = scheduler.enforce_deadlines(now=second.record.deadline)

    assert first.record.status == "COMPLETED"
    assert expired == ("job_002",)
    assert second.record.status == "CANCELLED"


def test_restart_reconciles_lost_running_worker_without_resetting_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scheduler.sqlite3"
    scheduler = BoundedScheduler(database, policy())
    scheduler.submit(request("job_001", candidate_cost=1, token_cost=50))

    resumed = BoundedScheduler(database, policy())

    assert resumed.job("job_001").status == "QUARANTINED"
    assert resumed.job("job_001").error_code == "WORKER_OWNERSHIP_LOST"
    assert resumed.consumed("run_001").candidate_count == 1
    assert resumed.consumed("run_001").token_count == 50
    second = resumed.submit(request("job_002"))
    assert second.record.status == "QUEUED"
    reconciled = resumed.reconcile_quarantined("job_001", FakeWorker())
    assert reconciled.status == "INFRASTRUCTURE_ERROR"
    assert second.record.status == "RUNNING"
    second.complete()


def test_restart_reattaches_running_and_queued_workers_in_fifo_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scheduler.sqlite3"
    first_worker = FakeWorker()
    second_worker = FakeWorker()
    scheduler = BoundedScheduler(database, policy())
    scheduler.submit(request("job_001", worker=first_worker))
    scheduler.submit(request("job_002", worker=second_worker))

    workers = {"job_001": first_worker, "job_002": second_worker}
    resumed = BoundedScheduler(
        database,
        policy(),
        worker_resolver=lambda record: workers[record.job_id],
    )

    assert resumed.job("job_001").status == "RUNNING"
    assert resumed.job("job_002").status == "QUEUED"
    resumed.handle("job_001").complete()
    assert resumed.job("job_002").status == "RUNNING"
    resumed.handle("job_002").complete()


def test_termination_failure_is_durable_infrastructure_outcome(tmp_path: Path) -> None:
    scheduler = BoundedScheduler(tmp_path / "scheduler.sqlite3", policy())
    worker = FakeWorker(terminate_fails=True)
    handle = scheduler.submit(request("job_001", worker=worker))
    queued = scheduler.submit(request("job_002"))

    handle.cancel(partial_log_artifacts=(artifact("artifact_stdout", "1"),))

    assert handle.record.status == "QUARANTINED"
    assert handle.record.error_code == "WORKER_TERMINATION_UNCONFIRMED"
    assert queued.record.status == "QUEUED"
    assert scheduler.events("job_001")[-1].event_type == "QUARANTINED"

    worker.terminate_fails = False
    reconciled = scheduler.reconcile_quarantined("job_001", worker)
    assert reconciled.status == "INFRASTRUCTURE_ERROR"
    assert queued.record.status == "RUNNING"
    queued.complete()
