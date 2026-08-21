from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nova_rtl.artifacts.ledger import ExperimentLedger, LedgerAppendError
from nova_rtl.artifacts.replay import (
    ReplayArtifactError,
    ReplaySchemaError,
    ReplaySelectionError,
    replay_digest,
    replay_run,
)
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.platform import DoctorCheck


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


class ExternalCallTrap:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, _name: str):
        def forbidden_call(*_args: object, **_kwargs: object) -> None:
            self.calls += 1
            raise AssertionError("replay attempted an external call")

        return forbidden_call


def event(
    store: ArtifactStore,
    *,
    sequence: int,
    timestamp: str,
    run_id: str = "run_001",
    payload_artifact=None,
    payload_schema_name: str = "doctor-check",
    payload_schema_version: int = 1,
) -> RunEvent:
    payload = payload_artifact or store.put_json(
        DoctorCheck(
            name=f"replay-event-{sequence}",
            status="PASS",
            resolved_path=None,
            tool_fingerprint=None,
            artifact_hash=hash_ref(str(sequence)),
            issues=(),
            message="recorded replay payload",
        ),
        classification="INTERNAL",
    )
    payload = payload.model_copy(
        update={"created_at": datetime(2026, 8, 21, tzinfo=UTC)}
    )
    return RunEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": f"event_{run_id}_{sequence:06d}",
            "run_id": run_id,
            "sequence": sequence,
            "event_type": "CANDIDATE_STATE_CHANGED",
            "entity_type": "CANDIDATE",
            "entity_id": "cand_001",
            "timestamp": timestamp,
            "prior_state": "VALIDATED",
            "new_state": "MATERIALIZED",
            "policy_hash": hash_ref("a"),
            "payload_schema_name": payload_schema_name,
            "payload_schema_version": payload_schema_version,
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


@pytest.fixture
def recorded_run(tmp_path: Path) -> tuple[ArtifactStore, ExperimentLedger]:
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = ExperimentLedger(
        tmp_path / "experiment-ledger.sqlite3", artifact_store=store
    )
    ledger.append_event(
        event(
            store,
            sequence=1,
            timestamp="2026-08-21T01:00:00Z",
        )
    )
    ledger.append_event(
        event(
            store,
            sequence=2,
            timestamp="2026-08-21T00:00:00Z",
        )
    )
    return store, ledger


def test_replay_uses_sequence_and_makes_no_model_or_tool_calls(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    _store, ledger = recorded_run
    model = ExternalCallTrap()
    tool = ExternalCallTrap()

    replayed = replay_run(ledger, model_client=model, tool_runner=tool)

    assert [item.sequence for item in replayed] == [1, 2]
    assert model.calls == 0
    assert tool.calls == 0


def test_replay_digest_is_deterministic_for_the_verified_event_stream(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    _store, ledger = recorded_run

    first = replay_digest(replay_run(ledger))
    second = replay_digest(replay_run(ledger))

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == 71


def test_replay_fails_closed_when_a_payload_artifact_is_missing(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, ledger = recorded_run
    first_event = next(ledger.iter_events("run_001"))
    store.blob_path(first_event.payload_artifact).unlink()

    with pytest.raises(
        ReplayArtifactError,
        match=f"{first_event.event_id}.*{first_event.payload_artifact.artifact_id}",
    ):
        replay_run(ledger)


@pytest.mark.parametrize(
    ("schema_name", "schema_version", "message"),
    [
        ("unknown-payload", 1, "unregistered payload schema"),
        ("doctor-check", 2, "payload schema version"),
    ],
)
def test_replay_rejects_unknown_or_mismatched_payload_schema(
    tmp_path: Path,
    schema_name: str,
    schema_version: int,
    message: str,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ledger = ExperimentLedger(
        tmp_path / "experiment-ledger.sqlite3", artifact_store=store
    )
    ledger.append_event(
        event(
            store,
            sequence=1,
            timestamp="2026-08-21T00:00:00Z",
            payload_schema_name=schema_name,
            payload_schema_version=schema_version,
        )
    )

    with pytest.raises(ReplaySchemaError, match=message):
        replay_run(ledger)


def test_replay_rejects_invalid_or_noncanonical_payload_json(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    invalid_ref = store.put_bytes(
        b"{}", media_type="application/json", classification="INTERNAL"
    )
    invalid_ledger = ExperimentLedger(
        tmp_path / "invalid.sqlite3", artifact_store=store
    )
    invalid_ledger.append_event(
        event(
            store,
            sequence=1,
            timestamp="2026-08-21T00:00:00Z",
            payload_artifact=invalid_ref,
        )
    )
    with pytest.raises(ReplaySchemaError, match="does not validate"):
        replay_run(invalid_ledger)

    valid_payload = DoctorCheck(
        name="noncanonical",
        status="PASS",
        resolved_path=None,
        tool_fingerprint=None,
        artifact_hash=hash_ref("1"),
        issues=(),
        message="valid fields with noncanonical encoding",
    )
    noncanonical_ref = store.put_bytes(
        json.dumps(valid_payload.model_dump(mode="json"), indent=2).encode(),
        media_type="application/json",
        classification="INTERNAL",
    )
    noncanonical_ledger = ExperimentLedger(
        tmp_path / "noncanonical.sqlite3", artifact_store=store
    )
    noncanonical_ledger.append_event(
        event(
            store,
            sequence=1,
            timestamp="2026-08-21T00:00:00Z",
            payload_artifact=noncanonical_ref,
        )
    )
    with pytest.raises(ReplaySchemaError, match="not canonical JSON"):
        replay_run(noncanonical_ledger)


def test_replay_requires_explicit_selection_when_ledger_has_multiple_runs(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, ledger = recorded_run
    ledger.append_event(
        event(
            store,
            run_id="run_002",
            sequence=1,
            timestamp="2026-08-21T02:00:00Z",
        )
    )

    with pytest.raises(ReplaySelectionError, match="multiple runs"):
        replay_run(ledger)
    assert [item.run_id for item in replay_run(ledger, run_id="run_002")] == ["run_002"]


def test_replay_can_open_a_recorded_run_without_mutating_it(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, ledger = recorded_run
    database_mtime = ledger.database_path.stat().st_mtime_ns
    readonly_store = ArtifactStore.open_existing(store.root)
    readonly_ledger = ExperimentLedger.open_existing(
        ledger.database_path, artifact_store=readonly_store
    )

    replayed = replay_run(readonly_ledger)

    assert [item.sequence for item in replayed] == [1, 2]
    assert ledger.database_path.stat().st_mtime_ns == database_mtime
    with pytest.raises(LedgerAppendError, match="read-only"):
        readonly_ledger.append_event(replayed[0])


def test_replay_module_cli_prints_the_same_digest_twice(
    recorded_run: tuple[ArtifactStore, ExperimentLedger],
) -> None:
    store, _ledger = recorded_run
    run_directory = store.root.parent
    command = [sys.executable, "-m", "nova_rtl.artifacts.replay", str(run_directory), "--digest"]

    first = subprocess.run(command, check=False, capture_output=True, text=True, env=os.environ)
    second = subprocess.run(command, check=False, capture_output=True, text=True, env=os.environ)

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert first.stdout.startswith("sha256:")


def test_replay_module_cli_normalizes_a_malformed_database(tmp_path: Path) -> None:
    run_directory = tmp_path / "malformed-run"
    ArtifactStore(run_directory / "artifacts")
    (run_directory / "experiment-ledger.sqlite3").write_bytes(b"not a sqlite database")
    command = [sys.executable, "-m", "nova_rtl.artifacts.replay", str(run_directory)]

    result = subprocess.run(
        command, check=False, capture_output=True, text=True, env=os.environ
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("replay failed: ledger database operation failed")
    assert "Traceback" not in result.stderr
