from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_run
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import StageInputHashes
from nova_rtl.orchestrator.state import (
    IllegalTransitionError,
    RunOrchestrator,
    StaleStateError,
    StateIntegrityError,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def payload(digit: str = "1") -> StageInputHashes:
    return StageInputHashes(
        rtl_snapshot=hash_ref(digit),
        design_contract=hash_ref("2"),
        constraints=hash_ref("3"),
        constraint_binding=hash_ref("4"),
        analysis_view=None,
        power_activity=None,
        platform_lock=hash_ref("5"),
        tool_recipe=hash_ref("6"),
        formal_model=None,
        parent_stage_result=None,
        extensions={},
    )


@pytest.fixture
def persistence(tmp_path: Path) -> tuple[ArtifactStore, ExperimentLedger]:
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = ExperimentLedger(
        tmp_path / "experiment-ledger.sqlite3", artifact_store=store
    )
    return store, ledger


@pytest.fixture
def orchestrator(
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> RunOrchestrator:
    store, ledger = persistence
    return RunOrchestrator(
        ledger=ledger,
        artifact_store=store,
        policy_hash=hash_ref("a"),
    )


def test_run_state_machine_is_durable_and_emits_payload_bound_events(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, ledger = persistence

    first = orchestrator.transition("run_001", "CREATED", "INGESTING", payload())
    second = orchestrator.transition("run_001", "INGESTING", "BASELINING", payload("7"))

    reopened = RunOrchestrator(
        ledger=ExperimentLedger(ledger.database_path, artifact_store=store),
        artifact_store=store,
        policy_hash=hash_ref("a"),
    )
    assert reopened.get_state("run_001") == second
    assert first.sequence == 1
    assert second.sequence == 2
    events = tuple(ledger.iter_events("run_001"))
    assert [(item.prior_state, item.new_state) for item in events] == [
        ("CREATED", "INGESTING"),
        ("INGESTING", "BASELINING"),
    ]
    assert events[-1].payload_artifact == second.payload_artifact
    assert store.open_verified(second.payload_artifact).read()


def test_run_state_machine_rejects_skipping_baseline(
    orchestrator: RunOrchestrator,
) -> None:
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())

    with pytest.raises(IllegalTransitionError, match="INGESTING.*SEARCHING"):
        orchestrator.transition("run_001", "INGESTING", "SEARCHING", payload())


def test_compare_and_set_rejects_stale_writer_without_appending_event(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    _store, ledger = persistence
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())

    with pytest.raises(StaleStateError, match="expected CREATED.*INGESTING"):
        orchestrator.transition("run_001", "CREATED", "INGESTING", payload())

    assert len(tuple(ledger.iter_events("run_001"))) == 1


def test_candidate_transitions_are_separate_and_recovery_creates_new_identity(
    orchestrator: RunOrchestrator,
) -> None:
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())
    proposed = orchestrator.create_candidate("run_001", "cand_001", payload())
    validated = orchestrator.transition_candidate(
        "run_001", "cand_001", "PROPOSED", "VALIDATED", payload("7")
    )
    orchestrator.transition_candidate(
        "run_001", "cand_001", "VALIDATED", "RECOVERY_ELIGIBLE", payload("7")
    )
    child = orchestrator.create_recovery_child(
        "run_001",
        parent_candidate_id="cand_001",
        candidate_id="cand_002",
        payload=payload("8"),
    )

    assert proposed.state == "PROPOSED"
    assert validated.state == "VALIDATED"
    assert child.state == "PROPOSED"
    assert child.recovery_parent_candidate_id == "cand_001"
    with pytest.raises(StaleStateError, match="already exists"):
        orchestrator.create_recovery_child(
            "run_001",
            parent_candidate_id="cand_001",
            candidate_id="cand_001",
            payload=payload("9"),
        )


def test_candidate_transition_requires_pinned_policy_and_recovery_eligible_parent(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, ledger = persistence
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())
    orchestrator.create_candidate("run_001", "cand_001", payload())
    changed = RunOrchestrator(
        ledger=ledger, artifact_store=store, policy_hash=hash_ref("b")
    )
    with pytest.raises(StaleStateError, match="policy"):
        changed.transition_candidate(
            "run_001", "cand_001", "PROPOSED", "VALIDATED", payload()
        )
    with pytest.raises(IllegalTransitionError, match="RECOVERY_ELIGIBLE"):
        orchestrator.create_recovery_child(
            "run_001",
            parent_candidate_id="cand_001",
            candidate_id="cand_002",
            payload=payload(),
        )
    with pytest.raises(IllegalTransitionError, match="RECOVERY_ELIGIBLE"):
        orchestrator.create_candidate(
            "run_001",
            "cand_003",
            payload(),
            recovery_parent_candidate_id="cand_001",
        )


def test_run_policy_is_pinned_and_artifact_store_must_match_ledger(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
    tmp_path: Path,
) -> None:
    store, ledger = persistence
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())

    changed_policy = RunOrchestrator(
        ledger=ledger,
        artifact_store=store,
        policy_hash=hash_ref("b"),
    )
    with pytest.raises(StaleStateError, match="policy"):
        changed_policy.transition("run_001", "INGESTING", "BASELINING", payload())

    with pytest.raises(ValueError, match="artifact store"):
        RunOrchestrator(
            ledger=ledger,
            artifact_store=ArtifactStore(tmp_path / "other-artifacts"),
            policy_hash=hash_ref("a"),
        )


def test_candidate_indexed_run_identity_is_verified(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    _store, ledger = persistence
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())
    orchestrator.create_candidate("run_001", "cand_001", payload())
    with sqlite3.connect(ledger.database_path) as connection:
        connection.execute(
            "UPDATE candidate_states SET run_id = ? WHERE candidate_id = ?",
            ("run_corrupt", "cand_001"),
        )

    with pytest.raises(StateIntegrityError, match="index mismatch"):
        orchestrator.transition_candidate(
            "run_001", "cand_001", "PROPOSED", "VALIDATED", payload()
        )


def test_observation_event_is_atomic_replayable_and_does_not_change_candidate_state(
    orchestrator: RunOrchestrator,
    persistence: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    _store, ledger = persistence
    orchestrator.transition("run_001", "CREATED", "INGESTING", payload())
    proposed = orchestrator.create_candidate("run_001", "cand_001", payload("7"))

    event = orchestrator.record_event(
        run_id="run_001",
        event_type="GATE_STARTED",
        entity_type="CANDIDATE_GATE",
        entity_id="cand_001",
        payload=payload("8"),
        status="PASS",
        error_code=None,
    )

    assert event.sequence == proposed.sequence + 1
    assert event.prior_state is None
    assert event.new_state is None
    assert orchestrator.get_candidate_state("cand_001") == proposed
    replayed = replay_run(ledger, "run_001")
    assert replayed[-1] == event
