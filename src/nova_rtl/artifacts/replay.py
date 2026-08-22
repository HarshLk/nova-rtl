"""Deterministic offline replay over verified event payload artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from hashlib import sha256
from pathlib import Path

from nova_rtl.artifacts.ledger import ExperimentLedger, LedgerError
from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.contracts.base import EntityId, canonical_json_bytes
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.schema_export import SCHEMA_REGISTRY


class ReplayError(RuntimeError):
    """Base error for deterministic, no-call replay."""


class ReplaySelectionError(ReplayError):
    """Raised when the requested replay run cannot be selected unambiguously."""


class ReplayArtifactError(ReplayError):
    """Raised when an event payload cannot be resolved and verified."""


class ReplaySchemaError(ReplayError):
    """Raised when verified payload bytes violate their declared canonical schema."""


def replay_run(
    ledger: ExperimentLedger,
    run_id: EntityId | None = None,
    *,
    model_client: object | None = None,
    tool_runner: object | None = None,
) -> tuple[RunEvent, ...]:
    """Replay verified events in sequence order without model or tool invocation."""

    del model_client, tool_runner
    available_run_ids = ledger.run_ids()
    selected_run_id = _select_run_id(available_run_ids, run_id)
    store = ledger.artifact_store
    if store is None:
        raise ReplayArtifactError("replay requires an attached artifact store")
    events = tuple(ledger.iter_events(selected_run_id))
    if not events:
        raise ReplaySelectionError(f"run has no replay events: {selected_run_id}")
    previous_sequence: int | None = None
    for event in events:
        if previous_sequence is not None and event.sequence <= previous_sequence:
            raise ReplayError("ledger returned a non-increasing replay sequence")
        previous_sequence = event.sequence
        _validate_event_payload(event, store)
    return events


def replay_digest(events: Iterable[RunEvent]) -> str:
    """Hash the canonical ordered event stream for reproducibility comparison."""

    payload = [event.model_dump(mode="json") for event in events]
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _select_run_id(
    available_run_ids: tuple[EntityId, ...], requested_run_id: EntityId | None
) -> EntityId:
    if requested_run_id is not None:
        if requested_run_id not in available_run_ids:
            raise ReplaySelectionError(f"run is not present in the ledger: {requested_run_id}")
        return requested_run_id
    if not available_run_ids:
        raise ReplaySelectionError("ledger contains no replay runs")
    if len(available_run_ids) > 1:
        raise ReplaySelectionError("ledger contains multiple runs; select one explicitly")
    return available_run_ids[0]


def _validate_event_payload(event: RunEvent, store: ArtifactStore) -> None:
    try:
        payload = store.open_verified(event.payload_artifact).read()
    except ArtifactStoreError as error:
        raise ReplayArtifactError(
            f"event {event.event_id} payload {event.payload_artifact.artifact_id} "
            "failed verification"
        ) from error
    registration = SCHEMA_REGISTRY.get(event.payload_schema_name)
    if registration is None:
        raise ReplaySchemaError(
            f"event {event.event_id} declares unregistered payload schema "
            f"{event.payload_schema_name}"
        )
    if registration.version != event.payload_schema_version:
        raise ReplaySchemaError(
            f"event {event.event_id} payload schema version mismatch: "
            f"declared={event.payload_schema_version}, registered={registration.version}"
        )
    if event.payload_artifact.media_type != "application/json":
        raise ReplaySchemaError(
            f"event {event.event_id} canonical payload must use application/json"
        )
    try:
        value = registration.model.model_validate_json(payload)
    except ValueError as error:
        raise ReplaySchemaError(
            f"event {event.event_id} payload does not validate as "
            f"{event.payload_schema_name} v{event.payload_schema_version}"
        ) from error
    if canonical_json_bytes(value) != payload:
        raise ReplaySchemaError(f"event {event.event_id} payload is not canonical JSON")


def main(argv: Sequence[str] | None = None) -> int:
    """Replay a recorded run directory and optionally print its deterministic digest."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--digest", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        store = ArtifactStore.open_existing(arguments.run_directory / "artifacts")
        ledger = ExperimentLedger.open_existing(
            arguments.run_directory / "experiment-ledger.sqlite3",
            artifact_store=store,
        )
        events = replay_run(ledger, run_id=arguments.run_id)
    except (ArtifactStoreError, LedgerError, ReplayError) as error:
        parser.exit(2, f"replay failed: {error}\n")
    if arguments.digest:
        print(replay_digest(events))
    else:
        print(
            json.dumps(
                [event.model_dump(mode="json") for event in events],
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess exercised
    raise SystemExit(main())


__all__ = [
    "ReplayArtifactError",
    "ReplayError",
    "ReplaySchemaError",
    "ReplaySelectionError",
    "main",
    "replay_digest",
    "replay_run",
]
