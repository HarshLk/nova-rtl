from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nova_rtl.artifacts.ledger import (
    ExperimentLedger,
    LedgerAppendError,
    LedgerIntegrityError,
)
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.reporting import ExperimentRecord


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def metric_payload(view_id: str, setup_wns_ns: float) -> dict[str, object]:
    return {
        "analysis_view_id": view_id,
        "setup_wns_ns": setup_wns_ns,
        "setup_tns_ns": -1.0 if setup_wns_ns < 0 else 0.0,
        "hold_wns_ns": 0.02,
        "hold_tns_ns": 0.0,
        "failing_endpoints": 1 if setup_wns_ns < 0 else 0,
        "critical_path_delay_ns": 2.0,
        "estimated_fmax_mhz": 500.0,
        "mapped_area_um2": 1000.0,
        "physical_area_um2": 1100.0,
        "cell_count": 500,
        "register_count": 100,
        "buffer_count": 20,
        "power_total_uw": 250.0,
        "wirelength_um": 5000.0,
        "congestion_overflow": 0.0,
        "runtime_ms": 100,
        "missing_metric_reasons": {},
    }


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def ledger(tmp_path: Path, store: ArtifactStore) -> ExperimentLedger:
    return ExperimentLedger(tmp_path / "experiment-ledger.sqlite3", artifact_store=store)


def event(
    store: ArtifactStore,
    *,
    sequence: int,
    event_id: str | None = None,
    timestamp: str = "2026-08-21T01:00:00Z",
    run_id: str = "run_001",
) -> RunEvent:
    payload = store.put_bytes(
        f'{{"sequence":{sequence}}}'.encode(),
        media_type="application/json",
        classification="INTERNAL",
    )
    return RunEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": event_id or f"event_{sequence:06d}",
            "run_id": run_id,
            "sequence": sequence,
            "event_type": "CANDIDATE_STATE_CHANGED",
            "entity_type": "CANDIDATE",
            "entity_id": "cand_001",
            "timestamp": timestamp,
            "prior_state": "VALIDATED",
            "new_state": "MATERIALIZED",
            "policy_hash": hash_ref("a"),
            "payload_schema_name": "candidate-record",
            "payload_schema_version": 1,
            "payload_artifact": payload,
            "duration_ms": 10,
            "resource_usage": {
                "cpu_time_ms": 5,
                "wall_time_ms": 10,
                "peak_rss_bytes": 1024,
            },
            "status": "PASS",
            "error_code": None,
        }
    )


def experiment(
    *,
    record_id: str,
    candidate_id: str,
    run_id: str = "run_001",
) -> ExperimentRecord:
    return ExperimentRecord.model_validate(
        {
            "schema_version": 1,
            "experiment_record_id": record_id,
            "run_id": run_id,
            "planner_result_id": "planner_result_001",
            "council_result_id": None,
            "opportunity_id": "path_cluster_001",
            "cone_fingerprint": "cone:v1:abc1",
            "proposal_id": "proposal_001",
            "operation": "RESTRUCTURE_PRIORITY_MUX",
            "parameters": {"strategy": "BALANCED_PREDECODE"},
            "parent_candidate_id": "baseline",
            "candidate_id": candidate_id,
            "source_hash": hash_ref("1"),
            "patch_hash": hash_ref("2"),
            "transform_fingerprint": "transform:v1:def2",
            "stage_result_ids": ["stage_synth_001", "stage_formal_001"],
            "comparison_identity_hashes": {
                "analysis_view": hash_ref("3"),
                "constraint_binding": hash_ref("4"),
            },
            "proof_result_id": "proof_001",
            "proof_outcome": "PASS",
            "counterexample_artifact_id": None,
            "before_metrics": {"func_setup_slow": metric_payload("func_setup_slow", -0.1)},
            "after_metrics": {"func_setup_slow": metric_payload("func_setup_slow", 0.1)},
            "failure_event_id": None,
            "repair_directive_id": None,
            "candidate_failure_fingerprint_id": None,
            "recovery_decision_id": None,
            "descendant_outcome": None,
            "role_ids": ["timing_forensics"],
            "model_configuration_hashes": [hash_ref("5")],
            "prompt_hashes": [hash_ref("6")],
            "context_pack_hashes": [hash_ref("7")],
            "input_tokens": 100,
            "output_tokens": 20,
            "planner_latency_ms": 50,
            "eda_runtime_ms": 500,
            "terminal_disposition": "FEASIBLE_PARETO",
            "human_review": None,
            "created_at": "2026-08-21T01:00:00Z",
        }
    )


def test_events_are_returned_by_sequence_not_timestamp(
    ledger: ExperimentLedger, store: ArtifactStore
) -> None:
    ledger.append_event(
        event(store, sequence=1, timestamp="2026-08-21T01:00:00Z")
    )
    ledger.append_event(
        event(store, sequence=2, timestamp="2026-08-21T00:00:00Z")
    )

    assert [item.sequence for item in ledger.iter_events("run_001")] == [1, 2]


def test_event_identity_and_sequence_are_append_only(
    ledger: ExperimentLedger, store: ArtifactStore
) -> None:
    ledger.append_event(event(store, sequence=1, event_id="event_original"))

    with pytest.raises(LedgerAppendError, match="event ID"):
        ledger.append_event(event(store, sequence=2, event_id="event_original"))
    with pytest.raises(LedgerAppendError, match="sequence"):
        ledger.append_event(event(store, sequence=1, event_id="event_duplicate_sequence"))
    with pytest.raises(LedgerAppendError, match="strictly increase"):
        ledger.append_event(event(store, sequence=0, event_id="event_nonmonotonic"))


def test_experiment_records_preserve_append_order_and_reject_duplicate_ids(
    ledger: ExperimentLedger,
) -> None:
    first = experiment(record_id="experiment_001", candidate_id="cand_001")
    second = experiment(record_id="experiment_002", candidate_id="cand_002")
    ledger.append_record(first)
    ledger.append_record(second)

    assert list(ledger.iter_records("run_001")) == [first, second]
    with pytest.raises(LedgerAppendError, match="experiment record ID"):
        ledger.append_record(first)


def test_database_triggers_reject_history_update_and_delete(
    ledger: ExperimentLedger, store: ArtifactStore
) -> None:
    ledger.append_event(event(store, sequence=1))
    ledger.append_record(experiment(record_id="experiment_001", candidate_id="cand_001"))

    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE run_events SET sequence = 2 WHERE event_id = ?",
                ("event_000001",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM experiment_records WHERE experiment_record_id = ?",
                ("experiment_001",),
            )


def test_database_triggers_reject_replace_and_backdated_event_insertion(
    ledger: ExperimentLedger, store: ArtifactStore
) -> None:
    ledger.append_event(event(store, sequence=1))
    ledger.append_record(experiment(record_id="experiment_001", candidate_id="cand_001"))

    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO run_events(
                    event_id, run_id, sequence, canonical_json, canonical_sha256
                )
                SELECT event_id, run_id, sequence, canonical_json, canonical_sha256
                FROM run_events WHERE event_id = ?
                """,
                ("event_000001",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="strictly increase"):
            connection.execute(
                """
                INSERT INTO run_events(
                    event_id, run_id, sequence, canonical_json, canonical_sha256
                )
                SELECT ?, run_id, 0, canonical_json, canonical_sha256
                FROM run_events WHERE event_id = ?
                """,
                ("event_backdated", "event_000001"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO experiment_records(
                    append_index, experiment_record_id, run_id,
                    canonical_json, canonical_sha256
                )
                SELECT append_index, experiment_record_id, run_id,
                       canonical_json, canonical_sha256
                FROM experiment_records WHERE experiment_record_id = ?
                """,
                ("experiment_001",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append index"):
            connection.execute(
                """
                INSERT OR REPLACE INTO experiment_records(
                    append_index, experiment_record_id, run_id,
                    canonical_json, canonical_sha256
                )
                SELECT append_index, ?, run_id, canonical_json, canonical_sha256
                FROM experiment_records WHERE experiment_record_id = ?
                """,
                ("experiment_replacement", "experiment_001"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="strictly increase"):
            connection.execute(
                """
                INSERT INTO experiment_records(
                    append_index, experiment_record_id, run_id,
                    canonical_json, canonical_sha256
                )
                SELECT 0, ?, run_id, canonical_json, canonical_sha256
                FROM experiment_records WHERE experiment_record_id = ?
                """,
                ("experiment_backdated", "experiment_001"),
            )


def test_readback_normalizes_invalid_sqlite_column_types(
    ledger: ExperimentLedger, store: ArtifactStore
) -> None:
    ledger.append_event(event(store, sequence=1))
    with sqlite3.connect(ledger.database_path) as connection:
        connection.execute(
            """
            INSERT INTO run_events(
                event_id, run_id, sequence, canonical_json, canonical_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("event_invalid_payload", "run_001", 2, 42, "not-a-hash"),
        )

    with pytest.raises(LedgerIntegrityError, match="column types"):
        list(ledger.iter_events("run_001"))
