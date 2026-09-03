"""Transactional compare-and-set authority for run and candidate state."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from pydantic import Field

from nova_rtl.analysis_views.aggregation import is_complete_measured_timing_violation
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    HashRef,
    StageInputHashes,
    StrictContract,
    UtcDatetime,
    canonical_json_bytes,
)
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.schema_export import SCHEMA_REGISTRY

RunState = Literal[
    "CREATED",
    "INGESTING",
    "BASELINING",
    "EVIDENCE_BUILD",
    "SEARCHING",
    "FINAL_PHYSICAL",
    "FINAL_VERIFICATION",
    "REPORTING",
    "COMPLETED",
    "FAILED_INPUT",
    "FAILED_BASELINE",
    "BUDGET_EXHAUSTED",
    "CANCELLED",
]
CandidateState = Literal[
    "PROPOSED",
    "VALIDATED",
    "MATERIALIZED",
    "PREFLIGHT",
    "FAST_SYNTH",
    "FORMAL",
    "FULL_STA",
    "PHYSICAL",
    "FEASIBLE",
    "PARETO",
    "DOMINATED",
    "SELECTED",
    "REJECTED",
    "INCONCLUSIVE",
    "INFRASTRUCTURE_ERROR",
    "RECOVERY_ELIGIBLE",
]

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"INGESTING", "FAILED_INPUT", "CANCELLED"}),
    "INGESTING": frozenset({"BASELINING", "FAILED_INPUT", "CANCELLED"}),
    "BASELINING": frozenset(
        {"EVIDENCE_BUILD", "FAILED_BASELINE", "BUDGET_EXHAUSTED", "CANCELLED"}
    ),
    "EVIDENCE_BUILD": frozenset(
        {"SEARCHING", "FAILED_BASELINE", "BUDGET_EXHAUSTED", "CANCELLED"}
    ),
    "SEARCHING": frozenset({"FINAL_PHYSICAL", "BUDGET_EXHAUSTED", "CANCELLED"}),
    "FINAL_PHYSICAL": frozenset(
        {"FINAL_VERIFICATION", "BUDGET_EXHAUSTED", "CANCELLED"}
    ),
    "FINAL_VERIFICATION": frozenset(
        {"REPORTING", "BUDGET_EXHAUSTED", "CANCELLED"}
    ),
    "REPORTING": frozenset({"COMPLETED", "BUDGET_EXHAUSTED", "CANCELLED"}),
}

_CANDIDATE_CHAIN = (
    "PROPOSED",
    "VALIDATED",
    "MATERIALIZED",
    "PREFLIGHT",
    "FAST_SYNTH",
    "FORMAL",
    "FULL_STA",
    "PHYSICAL",
    "FEASIBLE",
)
_EVALUATION_FAILURES = frozenset(
    {"REJECTED", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR", "RECOVERY_ELIGIBLE"}
)
CANDIDATE_TRANSITIONS: dict[str, frozenset[str]] = {
    state: frozenset({next_state}) | (_EVALUATION_FAILURES if index >= 1 else frozenset())
    for index, (state, next_state) in enumerate(
        zip(_CANDIDATE_CHAIN[:-1], _CANDIDATE_CHAIN[1:], strict=True)
    )
}
CANDIDATE_TRANSITIONS["FEASIBLE"] = frozenset({"PARETO", "DOMINATED", "SELECTED"})


class OrchestratorStateError(RuntimeError):
    """Base error for durable state authority failures."""


class IllegalTransitionError(OrchestratorStateError):
    """The requested state edge is not present in the locked state machine."""


class StaleStateError(OrchestratorStateError):
    """A compare-and-set caller supplied a stale expected state."""


class StateIntegrityError(OrchestratorStateError):
    """Persisted current-state bytes disagree with their indexed identity."""


class RunStateRecord(StrictContract):
    schema_version: Literal[1] = 1
    run_id: EntityId
    state: RunState
    sequence: int = Field(strict=True, gt=0)
    policy_hash: HashRef
    payload_artifact: ArtifactRef
    updated_at: UtcDatetime


class CandidateStateRecord(StrictContract):
    schema_version: Literal[1] = 1
    run_id: EntityId
    candidate_id: EntityId
    state: CandidateState
    sequence: int = Field(strict=True, gt=0)
    recovery_parent_candidate_id: EntityId | None
    payload_artifact: ArtifactRef
    updated_at: UtcDatetime


class RunOrchestrator:
    """The sole writer for durable global and candidate state transitions."""

    def __init__(
        self,
        *,
        ledger: ExperimentLedger,
        artifact_store: ArtifactStore,
        policy_hash: HashRef,
    ) -> None:
        if ledger.database_path != ledger.database_path.resolve():
            raise ValueError("ledger path must be normalized")
        if ledger.artifact_store is None or ledger.artifact_store.root != artifact_store.root:
            raise ValueError("orchestrator artifact store must match the ledger artifact store")
        self.ledger = ledger
        self.artifact_store = artifact_store
        self.policy_hash = policy_hash
        self._initialize()

    def transition(
        self,
        run_id: EntityId,
        expected_state: RunState,
        next_state: RunState,
        payload: StrictContract,
    ) -> RunStateRecord:
        """Atomically compare state, append its event, and publish the new current state."""

        self._assert_legal(RUN_TRANSITIONS, expected_state, next_state, "run")
        with self._write_connection() as connection:
            current = self._read_run(connection, run_id)
            actual_state = "CREATED" if current is None else current.state
            if actual_state != expected_state:
                raise StaleStateError(
                    f"run {run_id} expected {expected_state}, current state is {actual_state}"
                )
            if current is not None and current.policy_hash != self.policy_hash:
                raise StaleStateError(f"run {run_id} policy hash differs from persisted policy")
            payload_artifact = self._store_payload(payload)
            sequence = self._next_sequence(connection, run_id)
            timestamp = datetime.now(UTC)
            record = RunStateRecord(
                run_id=run_id,
                state=next_state,
                sequence=sequence,
                policy_hash=self.policy_hash,
                payload_artifact=payload_artifact,
                updated_at=timestamp,
            )
            event = self._event(
                run_id=run_id,
                sequence=sequence,
                entity_type="RUN",
                entity_id=run_id,
                prior_state=expected_state,
                new_state=next_state,
                payload=payload,
                payload_artifact=payload_artifact,
                timestamp=timestamp,
            )
            self._insert_event(connection, event)
            self._upsert_run(connection, record, current is None)
            return record

    def get_state(self, run_id: EntityId) -> RunStateRecord | None:
        with self._read_connection() as connection:
            return self._read_run(connection, run_id)

    def create_candidate(
        self,
        run_id: EntityId,
        candidate_id: EntityId,
        payload: StrictContract,
        *,
        recovery_parent_candidate_id: EntityId | None = None,
    ) -> CandidateStateRecord:
        with self._write_connection() as connection:
            run = self._read_run(connection, run_id)
            if run is None:
                raise StaleStateError(f"run {run_id} must exist before candidates are created")
            if run.policy_hash != self.policy_hash:
                raise StaleStateError(f"run {run_id} policy hash differs from persisted policy")
            if self._read_candidate(connection, candidate_id) is not None:
                raise StaleStateError(f"candidate {candidate_id} already exists")
            if recovery_parent_candidate_id is not None:
                parent = self._read_candidate(connection, recovery_parent_candidate_id)
                if parent is None or parent.run_id != run_id:
                    raise StaleStateError(
                        f"recovery parent {recovery_parent_candidate_id} does not exist in {run_id}"
                    )
                if parent.state != "RECOVERY_ELIGIBLE":
                    raise IllegalTransitionError(
                        f"recovery parent {recovery_parent_candidate_id} is not RECOVERY_ELIGIBLE"
                    )
            payload_artifact = self._store_payload(payload)
            sequence = self._next_sequence(connection, run_id)
            timestamp = datetime.now(UTC)
            record = CandidateStateRecord(
                run_id=run_id,
                candidate_id=candidate_id,
                state="PROPOSED",
                sequence=sequence,
                recovery_parent_candidate_id=recovery_parent_candidate_id,
                payload_artifact=payload_artifact,
                updated_at=timestamp,
            )
            event = self._event(
                run_id=run_id,
                sequence=sequence,
                entity_type="CANDIDATE",
                entity_id=candidate_id,
                prior_state=None,
                new_state="PROPOSED",
                payload=payload,
                payload_artifact=payload_artifact,
                timestamp=timestamp,
            )
            self._insert_event(connection, event)
            self._insert_candidate(connection, record)
            return record

    def create_recovery_child(
        self,
        run_id: EntityId,
        *,
        parent_candidate_id: EntityId,
        candidate_id: EntityId,
        payload: StrictContract,
    ) -> CandidateStateRecord:
        if candidate_id == parent_candidate_id:
            raise StaleStateError("recovery candidate already exists as its parent")
        return self.create_candidate(
            run_id,
            candidate_id,
            payload,
            recovery_parent_candidate_id=parent_candidate_id,
        )

    def transition_candidate(
        self,
        run_id: EntityId,
        candidate_id: EntityId,
        expected_state: CandidateState,
        next_state: CandidateState,
        payload: StrictContract,
    ) -> CandidateStateRecord:
        self._assert_legal(
            CANDIDATE_TRANSITIONS, expected_state, next_state, "candidate"
        )
        with self._write_connection() as connection:
            run = self._read_run(connection, run_id)
            if run is None or run.policy_hash != self.policy_hash:
                raise StaleStateError(
                    f"run {run_id} policy hash differs from persisted policy"
                )
            current = self._read_candidate(connection, candidate_id)
            if current is None or current.run_id != run_id:
                raise StaleStateError(f"candidate {candidate_id} does not exist in {run_id}")
            if current.state != expected_state:
                raise StaleStateError(
                    f"candidate {candidate_id} expected {expected_state}, "
                    f"current state is {current.state}"
                )
            payload_artifact = self._store_payload(payload)
            sequence = self._next_sequence(connection, run_id)
            timestamp = datetime.now(UTC)
            record = CandidateStateRecord(
                run_id=run_id,
                candidate_id=candidate_id,
                state=next_state,
                sequence=sequence,
                recovery_parent_candidate_id=current.recovery_parent_candidate_id,
                payload_artifact=payload_artifact,
                updated_at=timestamp,
            )
            event = self._event(
                run_id=run_id,
                sequence=sequence,
                entity_type="CANDIDATE",
                entity_id=candidate_id,
                prior_state=expected_state,
                new_state=next_state,
                payload=payload,
                payload_artifact=payload_artifact,
                timestamp=timestamp,
            )
            self._insert_event(connection, event)
            self._update_candidate(connection, record, expected_state)
            return record

    @staticmethod
    def can_reuse(cached_stage: object, requested_hashes: StageInputHashes) -> bool:
        """Authorize reuse only for a successful exact StageInputHashes match."""

        return (
            (
                getattr(cached_stage, "status", None) == "PASS"
                or is_complete_measured_timing_violation(cached_stage)  # type: ignore[arg-type]
            )
            and getattr(cached_stage, "input_hashes", None) == requested_hashes
        )

    @staticmethod
    def _assert_legal(
        transitions: dict[str, frozenset[str]],
        expected_state: str,
        next_state: str,
        entity: str,
    ) -> None:
        if next_state not in transitions.get(expected_state, frozenset()):
            raise IllegalTransitionError(
                f"illegal {entity} transition {expected_state} -> {next_state}"
            )

    def _store_payload(self, payload: StrictContract) -> ArtifactRef:
        if not isinstance(payload, StrictContract):
            raise TypeError("transition payload must be a StrictContract")
        self._payload_registration(payload)
        return self.artifact_store.put_json(payload, classification="INTERNAL")

    @staticmethod
    def _payload_registration(payload: StrictContract) -> tuple[str, int]:
        matches = [
            (name, registration.version)
            for name, registration in SCHEMA_REGISTRY.items()
            if type(payload) is registration.model
        ]
        if len(matches) != 1:
            raise ValueError(
                f"transition payload type is not uniquely registered: {type(payload).__name__}"
            )
        return matches[0]

    def _event(
        self,
        *,
        run_id: EntityId,
        sequence: int,
        entity_type: Literal["RUN", "CANDIDATE"],
        entity_id: EntityId,
        prior_state: str | None,
        new_state: str,
        payload: StrictContract,
        payload_artifact: ArtifactRef,
        timestamp: datetime,
    ) -> RunEvent:
        schema_name, schema_version = self._payload_registration(payload)
        event_digest = sha256(
            f"{run_id}\0{sequence}\0{entity_type}\0{entity_id}".encode()
        ).hexdigest()[:32]
        return RunEvent(
            event_id=f"event_{event_digest}",
            run_id=run_id,
            sequence=sequence,
            event_type=f"{entity_type}_STATE_CHANGED",
            entity_type=entity_type,
            entity_id=entity_id,
            timestamp=timestamp,
            prior_state=prior_state,
            new_state=new_state,
            policy_hash=self.policy_hash,
            payload_schema_name=schema_name,
            payload_schema_version=schema_version,
            payload_artifact=payload_artifact,
            duration_ms=0,
            resource_usage={"cpu_time_ms": 0, "wall_time_ms": 0, "peak_rss_bytes": 0},
            status="PASS",
            error_code=None,
        )

    @staticmethod
    def _next_sequence(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: RunEvent) -> None:
        encoded = canonical_json_bytes(event)
        connection.execute(
            """
            INSERT INTO run_events(event_id, run_id, sequence, canonical_json, canonical_sha256)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.run_id,
                event.sequence,
                encoded,
                sha256(encoded).hexdigest(),
            ),
        )

    @classmethod
    def _upsert_run(
        cls,
        connection: sqlite3.Connection,
        record: RunStateRecord,
        is_new: bool,
    ) -> None:
        encoded = canonical_json_bytes(record)
        values = (
            record.run_id,
            record.state,
            record.sequence,
            record.policy_hash,
            encoded,
            sha256(encoded).hexdigest(),
        )
        if is_new:
            connection.execute(
                """
                INSERT INTO run_states(
                    run_id, state, sequence, policy_hash, canonical_json, canonical_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            cursor = connection.execute(
                """
                UPDATE run_states
                SET state = ?, sequence = ?, policy_hash = ?,
                    canonical_json = ?, canonical_sha256 = ?
                WHERE run_id = ? AND sequence < ?
                """,
                (
                    record.state,
                    record.sequence,
                    record.policy_hash,
                    encoded,
                    sha256(encoded).hexdigest(),
                    record.run_id,
                    record.sequence,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleStateError(f"run {record.run_id} changed during transition")

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection, record: CandidateStateRecord
    ) -> None:
        encoded = canonical_json_bytes(record)
        connection.execute(
            """
            INSERT INTO candidate_states(
                candidate_id, run_id, state, sequence, canonical_json, canonical_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.candidate_id,
                record.run_id,
                record.state,
                record.sequence,
                encoded,
                sha256(encoded).hexdigest(),
            ),
        )

    @staticmethod
    def _update_candidate(
        connection: sqlite3.Connection,
        record: CandidateStateRecord,
        expected_state: str,
    ) -> None:
        encoded = canonical_json_bytes(record)
        cursor = connection.execute(
            """
            UPDATE candidate_states
            SET state = ?, sequence = ?, canonical_json = ?, canonical_sha256 = ?
            WHERE candidate_id = ? AND run_id = ? AND state = ?
            """,
            (
                record.state,
                record.sequence,
                encoded,
                sha256(encoded).hexdigest(),
                record.candidate_id,
                record.run_id,
                expected_state,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleStateError(f"candidate {record.candidate_id} changed during transition")

    @classmethod
    def _read_run(
        cls, connection: sqlite3.Connection, run_id: str
    ) -> RunStateRecord | None:
        row = connection.execute(
            """
            SELECT run_id, state, sequence, policy_hash, canonical_json, canonical_sha256
            FROM run_states WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return cls._decode_record(row, RunStateRecord)

    @classmethod
    def _read_candidate(
        cls, connection: sqlite3.Connection, candidate_id: str
    ) -> CandidateStateRecord | None:
        row = connection.execute(
            """
            SELECT candidate_id, run_id, state, sequence, canonical_json, canonical_sha256
            FROM candidate_states WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return cls._decode_record(row, CandidateStateRecord)

    @staticmethod
    def _decode_record(
        row: tuple[object, ...],
        model: type[RunStateRecord] | type[CandidateStateRecord],
    ) -> RunStateRecord | CandidateStateRecord:
        indexed_id = row[0]
        if model is CandidateStateRecord:
            indexed_run_id, indexed_state, indexed_sequence, encoded, expected_hash = row[1:]
        else:
            indexed_run_id = indexed_id
            indexed_state, indexed_sequence, indexed_policy, encoded, expected_hash = row[1:]
        if not isinstance(encoded, bytes) or not isinstance(expected_hash, str):
            raise StateIntegrityError("state storage column types are invalid")
        if sha256(encoded).hexdigest() != expected_hash:
            raise StateIntegrityError(f"state hash mismatch for {indexed_id}")
        try:
            record = model.model_validate_json(encoded)
        except ValueError as error:
            raise StateIntegrityError(f"invalid state payload for {indexed_id}") from error
        record_id = getattr(record, "run_id", None)
        if model is CandidateStateRecord:
            record_id = getattr(record, "candidate_id", None)
        policy_mismatch = (
            model is RunStateRecord
            and isinstance(record, RunStateRecord)
            and record.policy_hash != indexed_policy
        )
        if (
            record_id != indexed_id
            or record.run_id != indexed_run_id
            or record.state != indexed_state
            or record.sequence != indexed_sequence
            or policy_mismatch
            or canonical_json_bytes(record) != encoded
        ):
            raise StateIntegrityError(f"state index mismatch for {indexed_id}")
        return record

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger.database_path, timeout=30.0)
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
                CREATE TABLE IF NOT EXISTS run_states (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    state TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    policy_hash TEXT NOT NULL,
                    canonical_json BLOB NOT NULL,
                    canonical_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_states (
                    candidate_id TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    canonical_json BLOB NOT NULL,
                    canonical_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS candidate_states_by_run
                    ON candidate_states(run_id, candidate_id);
                """
            )


__all__ = [
    "CANDIDATE_TRANSITIONS",
    "RUN_TRANSITIONS",
    "CandidateState",
    "CandidateStateRecord",
    "IllegalTransitionError",
    "OrchestratorStateError",
    "RunOrchestrator",
    "RunState",
    "RunStateRecord",
    "StaleStateError",
    "StateIntegrityError",
]
