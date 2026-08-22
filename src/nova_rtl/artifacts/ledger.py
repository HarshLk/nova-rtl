"""Transactional append-only SQLite authority for events and experiments."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import EntityId, StrictContract, canonical_json_bytes
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.reporting import ExperimentRecord


class LedgerError(RuntimeError):
    """Base error for ledger persistence and validation."""


class LedgerAppendError(LedgerError):
    """Raised when an append would rewrite identity or ordering history."""


class LedgerIntegrityError(LedgerError):
    """Raised when persisted bytes disagree with indexed ledger identity."""


class ExperimentLedger:
    """Append-only event and experiment ledger with validated canonical readback."""

    def __init__(
        self,
        database_path: Path,
        *,
        artifact_store: ArtifactStore | None = None,
        _read_only: bool = False,
    ) -> None:
        self.database_path = database_path.resolve()
        self.artifact_store = artifact_store
        self._read_only = _read_only
        if _read_only:
            if not self.database_path.is_file():
                raise LedgerError(f"ledger database does not exist: {self.database_path}")
        else:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @classmethod
    def open_existing(
        cls,
        database_path: Path,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> ExperimentLedger:
        """Open an existing ledger through read-only SQLite connections."""

        return cls(database_path, artifact_store=artifact_store, _read_only=True)

    def append_event(self, event: RunEvent) -> None:
        """Append an event only when its ID is new and sequence strictly increases."""

        if self._read_only:
            raise LedgerAppendError("ledger is read-only")
        payload, payload_hash = self._encode(event)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM run_events WHERE event_id = ?", (event.event_id,)
                ).fetchone():
                    raise LedgerAppendError(
                        f"duplicate event ID is not append-only: {event.event_id}"
                    )
                last_row = connection.execute(
                    "SELECT MAX(sequence) FROM run_events WHERE run_id = ?",
                    (event.run_id,),
                ).fetchone()
                last_sequence = last_row[0] if last_row is not None else None
                if last_sequence is not None and event.sequence <= last_sequence:
                    if event.sequence == last_sequence:
                        raise LedgerAppendError(
                            f"duplicate event sequence for {event.run_id}: {event.sequence}"
                        )
                    raise LedgerAppendError(
                        "event sequence must strictly increase for "
                        f"{event.run_id}: {event.sequence} <= {last_sequence}"
                    )
                connection.execute(
                    """
                    INSERT INTO run_events(
                        event_id, run_id, sequence, canonical_json, canonical_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.event_id, event.run_id, event.sequence, payload, payload_hash),
                )
        except sqlite3.IntegrityError as error:
            raise LedgerAppendError(f"event append rejected: {error}") from error
        except sqlite3.DatabaseError as error:
            raise LedgerAppendError("event append database operation failed") from error

    def append_record(self, record: ExperimentRecord) -> None:
        """Append one immutable experiment identity in ledger insertion order."""

        if self._read_only:
            raise LedgerAppendError("ledger is read-only")
        payload, payload_hash = self._encode(record)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM experiment_records WHERE experiment_record_id = ?",
                    (record.experiment_record_id,),
                ).fetchone():
                    raise LedgerAppendError(
                        "duplicate experiment record ID is not append-only: "
                        f"{record.experiment_record_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO experiment_records(
                        experiment_record_id, run_id, canonical_json, canonical_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        record.experiment_record_id,
                        record.run_id,
                        payload,
                        payload_hash,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise LedgerAppendError(f"experiment record append rejected: {error}") from error
        except sqlite3.DatabaseError as error:
            raise LedgerAppendError(
                "experiment record append database operation failed"
            ) from error

    def iter_events(self, run_id: EntityId) -> Iterator[RunEvent]:
        """Return validated events ordered solely by their replay-authority sequence."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, run_id, sequence, canonical_json, canonical_sha256
                    FROM run_events
                    WHERE run_id = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise LedgerIntegrityError("ledger database operation failed") from error
        for event_id, stored_run_id, sequence, payload, payload_hash in rows:
            event = self._decode(payload, payload_hash, RunEvent)
            if (
                event.event_id != event_id
                or event.run_id != stored_run_id
                or event.sequence != sequence
            ):
                raise LedgerIntegrityError(
                    f"event index identity disagrees with payload: {event_id}"
                )
            yield event

    def iter_records(self, run_id: EntityId) -> Iterator[ExperimentRecord]:
        """Return validated experiment records in immutable append order."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT experiment_record_id, run_id, canonical_json, canonical_sha256
                    FROM experiment_records
                    WHERE run_id = ?
                    ORDER BY append_index ASC
                    """,
                    (run_id,),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise LedgerIntegrityError("ledger database operation failed") from error
        for record_id, stored_run_id, payload, payload_hash in rows:
            record = self._decode(payload, payload_hash, ExperimentRecord)
            if (
                record.experiment_record_id != record_id
                or record.run_id != stored_run_id
            ):
                raise LedgerIntegrityError(
                    f"experiment index identity disagrees with payload: {record_id}"
                )
            yield record

    def run_ids(self) -> tuple[EntityId, ...]:
        """Return sorted run identities that have replay-authority events."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT DISTINCT run_id FROM run_events ORDER BY run_id ASC"
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise LedgerIntegrityError("ledger database operation failed") from error
        if any(not isinstance(row[0], str) for row in rows):
            raise LedgerIntegrityError("ledger run identity column types are invalid")
        return tuple(row[0] for row in rows)

    @staticmethod
    def _encode(value: StrictContract) -> tuple[bytes, str]:
        payload = canonical_json_bytes(value)
        return payload, sha256(payload).hexdigest()

    @staticmethod
    def _decode(
        payload: bytes,
        expected_hash: str,
        contract_type: type[RunEvent] | type[ExperimentRecord],
    ) -> RunEvent | ExperimentRecord:
        if not isinstance(payload, bytes) or not isinstance(expected_hash, str):
            raise LedgerIntegrityError("ledger canonical payload column types are invalid")
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise LedgerIntegrityError("ledger canonical payload hash column is invalid")
        if sha256(payload).hexdigest() != expected_hash:
            raise LedgerIntegrityError("ledger canonical payload hash does not match")
        try:
            value = contract_type.model_validate_json(payload)
        except ValueError as error:
            raise LedgerIntegrityError("ledger canonical payload is invalid") from error
        if canonical_json_bytes(value) != payload:
            raise LedgerIntegrityError("ledger payload is not canonical JSON")
        return value

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            uri = f"{self.database_path.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, timeout=30.0, uri=True)
        else:
            connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        if not self._read_only:
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    canonical_json BLOB NOT NULL,
                    canonical_sha256 TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS run_events_by_run_sequence
                    ON run_events(run_id, sequence);

                CREATE TABLE IF NOT EXISTS experiment_records (
                    append_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_record_id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL,
                    canonical_json BLOB NOT NULL,
                    canonical_sha256 TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS experiment_records_by_run_append
                    ON experiment_records(run_id, append_index);

                CREATE TRIGGER IF NOT EXISTS experiment_records_index_identity_guard
                BEFORE INSERT ON experiment_records
                WHEN EXISTS(
                    SELECT 1 FROM experiment_records
                    WHERE append_index = NEW.append_index
                )
                BEGIN
                    SELECT RAISE(
                        ABORT, 'experiment record append index is append-only'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS run_events_append_guard
                BEFORE INSERT ON run_events
                BEGIN
                    SELECT CASE
                        WHEN EXISTS(
                            SELECT 1 FROM run_events WHERE event_id = NEW.event_id
                        )
                        THEN RAISE(ABORT, 'run_events are append-only')
                        WHEN EXISTS(
                            SELECT 1 FROM run_events WHERE run_id = NEW.run_id
                        ) AND NEW.sequence <= (
                            SELECT MAX(sequence) FROM run_events WHERE run_id = NEW.run_id
                        )
                        THEN RAISE(ABORT, 'run event sequence must strictly increase')
                    END;
                END;

                CREATE TRIGGER IF NOT EXISTS experiment_records_append_guard
                BEFORE INSERT ON experiment_records
                WHEN EXISTS(
                    SELECT 1 FROM experiment_records
                    WHERE experiment_record_id = NEW.experiment_record_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'experiment_records are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS experiment_records_order_guard
                AFTER INSERT ON experiment_records
                WHEN EXISTS(
                    SELECT 1 FROM experiment_records
                    WHERE append_index != NEW.append_index
                ) AND NEW.append_index <= (
                    SELECT MAX(append_index) FROM experiment_records
                    WHERE append_index != NEW.append_index
                )
                BEGIN
                    SELECT RAISE(
                        ABORT, 'experiment record append index must strictly increase'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS run_events_no_update
                BEFORE UPDATE ON run_events
                BEGIN
                    SELECT RAISE(ABORT, 'run_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS run_events_no_delete
                BEFORE DELETE ON run_events
                BEGIN
                    SELECT RAISE(ABORT, 'run_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS experiment_records_no_update
                BEFORE UPDATE ON experiment_records
                BEGIN
                    SELECT RAISE(ABORT, 'experiment_records are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS experiment_records_no_delete
                BEFORE DELETE ON experiment_records
                BEGIN
                    SELECT RAISE(ABORT, 'experiment_records are append-only');
                END;
                    """
                )
        except sqlite3.DatabaseError as error:
            raise LedgerError("ledger database initialization failed") from error


__all__ = [
    "ExperimentLedger",
    "LedgerAppendError",
    "LedgerError",
    "LedgerIntegrityError",
]
