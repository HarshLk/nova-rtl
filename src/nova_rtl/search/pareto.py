"""Correctness-partitioned deterministic Pareto archive."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import EntityId, HashRef, StrictContract, canonical_sha256
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.reporting import ParetoRecord
from nova_rtl.evaluation.metrics import (
    OFFICIAL_METRIC_VECTOR,
    REQUIRED_COMPARISON_IDENTITIES,
    IncomparableMetricsError,
)


class ParetoArchiveError(ValueError):
    """A Pareto record is duplicated or lacks official comparison evidence."""


_MAXIMIZE = frozenset(
    {"setup_wns_ns", "setup_tns_ns", "hold_wns_ns", "hold_tns_ns"}
)
_MINIMIZE = frozenset(set(OFFICIAL_METRIC_VECTOR) - _MAXIMIZE)


class ParetoUpdate(StrictContract):
    schema_version: Literal[1] = 1
    correctness_contract: CorrectnessContract
    candidate_id: EntityId
    disposition: Literal["FRONTIER", "DOMINATED"]
    frontier_candidate_ids: tuple[EntityId, ...]
    removed_candidate_ids: tuple[EntityId, ...]
    update_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.update_hash != canonical_sha256(self, exclude=frozenset({"update_hash"})):
            raise ValueError("Pareto update hash is not canonical")
        return self


class ParetoArchiveSnapshot(StrictContract):
    schema_version: Literal[1] = 1
    records: tuple[ParetoRecord, ...]
    frontier_candidate_ids: dict[CorrectnessContract, tuple[EntityId, ...]]
    dominance: dict[EntityId, tuple[EntityId, ...]]
    archive_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        ids = tuple(record.candidate_id for record in self.records)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("Pareto snapshot records must be unique and ordered")
        if self.archive_hash != canonical_sha256(self, exclude=frozenset({"archive_hash"})):
            raise ValueError("Pareto archive hash is not canonical")
        return self


def _dominates(left: ParetoRecord, right: ParetoRecord) -> bool:
    no_worse = all(
        left.metric_vector[name] >= right.metric_vector[name]
        if name in _MAXIMIZE
        else left.metric_vector[name] <= right.metric_vector[name]
        for name in OFFICIAL_METRIC_VECTOR
    )
    strictly_better = any(
        left.metric_vector[name] > right.metric_vector[name]
        if name in _MAXIMIZE
        else left.metric_vector[name] < right.metric_vector[name]
        for name in OFFICIAL_METRIC_VECTOR
    )
    return no_worse and strictly_better


class ParetoArchive:
    """Retain immutable records and derive frontiers within exact contract partitions."""

    def __init__(self) -> None:
        self._records: dict[str, ParetoRecord] = {}

    def _partition(self, contract: CorrectnessContract) -> tuple[ParetoRecord, ...]:
        return tuple(
            self._records[key]
            for key in sorted(self._records)
            if self._records[key].correctness_contract == contract
        )

    @staticmethod
    def _validate_record(record: ParetoRecord) -> None:
        supplied_metrics = set(record.metric_vector)
        expected_metrics = set(OFFICIAL_METRIC_VECTOR)
        if supplied_metrics != expected_metrics:
            missing = sorted(expected_metrics - supplied_metrics)
            extra = sorted(supplied_metrics - expected_metrics)
            raise ParetoArchiveError(
                f"official Pareto vector must be complete (missing={missing}, extra={extra})"
            )
        supplied_identities = set(record.comparison_identity_hashes)
        if supplied_identities != REQUIRED_COMPARISON_IDENTITIES:
            raise ParetoArchiveError("official comparison identity set is incomplete")

    @staticmethod
    def _require_same_identity(left: ParetoRecord, right: ParetoRecord) -> None:
        if set(left.required_view_metric_ids) != set(right.required_view_metric_ids):
            raise IncomparableMetricsError("required analysis view IDs differ")
        if left.physical_stage != right.physical_stage:
            raise IncomparableMetricsError("physical_stage differs")
        for name in sorted(REQUIRED_COMPARISON_IDENTITIES):
            if left.comparison_identity_hashes[name] != right.comparison_identity_hashes[name]:
                raise IncomparableMetricsError(f"comparison identity differs: {name}")

    def add(self, record: ParetoRecord) -> ParetoUpdate:
        self._validate_record(record)
        if record.candidate_id in self._records:
            raise ParetoArchiveError(f"candidate already archived: {record.candidate_id}")
        peers = self._partition(record.correctness_contract)
        for peer in peers:
            self._require_same_identity(peer, record)
        before = {item.candidate_id for item in self.frontier(record.correctness_contract)}
        self._records[record.candidate_id] = record
        after_records = self.frontier(record.correctness_contract)
        after = {item.candidate_id for item in after_records}
        removed = tuple(sorted(before - after))
        payload = {
            "schema_version": 1,
            "correctness_contract": record.correctness_contract,
            "candidate_id": record.candidate_id,
            "disposition": "FRONTIER" if record.candidate_id in after else "DOMINATED",
            "frontier_candidate_ids": tuple(sorted(after)),
            "removed_candidate_ids": removed,
        }
        return ParetoUpdate(**payload, update_hash=canonical_sha256(payload))

    def frontier(self, contract: CorrectnessContract) -> tuple[ParetoRecord, ...]:
        records = self._partition(contract)
        return tuple(
            record
            for record in records
            if not any(
                other.candidate_id != record.candidate_id and _dominates(other, record)
                for other in records
            )
        )

    def snapshot(self) -> ParetoArchiveSnapshot:
        records = tuple(self._records[key] for key in sorted(self._records))
        contracts = tuple(sorted({record.correctness_contract for record in records}))
        frontier_ids = {
            contract: tuple(item.candidate_id for item in self.frontier(contract))
            for contract in contracts
        }
        dominance = {
            record.candidate_id: tuple(
                other.candidate_id
                for other in records
                if other.correctness_contract == record.correctness_contract
                and _dominates(record, other)
            )
            for record in records
        }
        payload = {
            "schema_version": 1,
            "records": tuple(record.model_dump(mode="json") for record in records),
            "frontier_candidate_ids": frontier_ids,
            "dominance": dominance,
        }
        return ParetoArchiveSnapshot(
            records=records,
            frontier_candidate_ids=frontier_ids,
            dominance=dominance,
            archive_hash=canonical_sha256(payload),
        )


__all__ = [
    "ParetoArchive",
    "ParetoArchiveError",
    "ParetoArchiveSnapshot",
    "ParetoUpdate",
]
