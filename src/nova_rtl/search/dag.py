"""Immutable-record candidate DAG with deterministic snapshots."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import EntityId, HashRef, StrictContract, canonical_sha256
from nova_rtl.contracts.optimization import CandidateRecord


class CandidateDagError(ValueError):
    """Candidate lineage is missing, duplicated, or inconsistent."""


def replace_candidate(candidate: CandidateRecord, **changes: Any) -> CandidateRecord:
    """Create a validated replacement while leaving the original record immutable."""

    payload = candidate.model_dump(mode="python")
    payload.update(changes)
    return CandidateRecord.model_validate(payload)


class CandidateDagSnapshot(StrictContract):
    """Canonical immutable view of all candidate records in a search DAG."""

    schema_version: Literal[1] = 1
    root_candidate_id: EntityId
    candidate_ids: tuple[EntityId, ...]
    candidates: tuple[CandidateRecord, ...]
    dag_hash: HashRef

    @model_validator(mode="after")
    def contents_are_ordered_and_self_hashed(self) -> Self:
        ids = tuple(candidate.candidate_id for candidate in self.candidates)
        if ids != self.candidate_ids or ids != tuple(sorted(set(ids))):
            raise ValueError("candidate DAG entries must be unique and canonically ordered")
        expected = canonical_sha256(self, exclude=frozenset({"dag_hash"}))
        if self.dag_hash != expected:
            raise ValueError("candidate DAG hash is not canonical")
        return self


class CandidateDag:
    """Append-only in-memory lineage index whose records remain immutable."""

    def __init__(self, root_candidate_id: EntityId) -> None:
        self._root_candidate_id = root_candidate_id
        self._candidates: dict[str, CandidateRecord] = {}

    def add(self, candidate: CandidateRecord) -> None:
        if candidate.candidate_id in self._candidates:
            raise CandidateDagError(f"candidate already exists: {candidate.candidate_id}")
        parent = candidate.parent_candidate_id
        if parent != self._root_candidate_id and parent not in self._candidates:
            raise CandidateDagError(f"candidate has unknown parent: {parent}")
        expected_depth = (
            1 if parent == self._root_candidate_id else self._candidates[parent].lineage_depth + 1
        )
        if candidate.lineage_depth != expected_depth:
            raise CandidateDagError(
                f"candidate lineage depth {candidate.lineage_depth} does not follow parent"
            )
        self._candidates[candidate.candidate_id] = candidate

    def get(self, candidate_id: EntityId) -> CandidateRecord:
        try:
            return self._candidates[candidate_id]
        except KeyError as error:
            raise CandidateDagError(f"unknown candidate: {candidate_id}") from error

    def snapshot(self) -> CandidateDagSnapshot:
        candidates = tuple(self._candidates[key] for key in sorted(self._candidates))
        payload = {
            "schema_version": 1,
            "root_candidate_id": self._root_candidate_id,
            "candidate_ids": tuple(candidate.candidate_id for candidate in candidates),
            "candidates": tuple(candidate.model_dump(mode="json") for candidate in candidates),
        }
        return CandidateDagSnapshot(
            root_candidate_id=self._root_candidate_id,
            candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
            candidates=candidates,
            dag_hash=canonical_sha256(payload),
        )


__all__ = [
    "CandidateDag",
    "CandidateDagError",
    "CandidateDagSnapshot",
    "replace_candidate",
]
