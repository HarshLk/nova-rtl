"""Read-only, stable-ID-scoped evidence access for reasoning components."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from nova_rtl.contracts.base import (
    Classification,
    EvidenceRef,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import JsonScalar
from nova_rtl.planner.interface import PlannerPolicy

_ENTITY_ID = re.compile(r"^[a-z][a-z0-9_]{2,95}$")


class ContextIntegrityError(RuntimeError):
    """Evidence access violated snapshot, role, kind, or policy authority."""


class EvidenceObject(StrictContract):
    """One immutable provider-safe evidence object without a filesystem path."""

    schema_version: Literal[1] = 1
    evidence_ref: EvidenceRef
    classification: Classification
    payload: dict[str, JsonScalar] = Field(min_length=1)
    size_bytes: NonNegativeInt
    token_estimate: NonNegativeInt
    content_hash: HashRef

    @model_validator(mode="after")
    def identity_matches_canonical_bytes(self) -> Self:
        content = canonical_json_bytes(self.payload)
        if self.size_bytes != len(content):
            raise ValueError("evidence size does not match canonical payload bytes")
        expected_tokens = max(1, (len(content) + 3) // 4)
        if self.token_estimate != expected_tokens:
            raise ValueError("evidence token estimate is not canonical")
        if self.content_hash != canonical_sha256(
            self, exclude=frozenset({"content_hash"})
        ):
            raise ValueError("evidence content hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> EvidenceObject:
        payload = dict(values)
        content = canonical_json_bytes(payload["payload"])  # type: ignore[arg-type]
        payload.update(
            schema_version=1,
            size_bytes=len(content),
            token_estimate=max(1, (len(content) + 3) // 4),
        )
        provisional = cls.model_construct(
            **payload, content_hash="sha256:" + "0" * 64
        )
        return cls(
            **payload,
            content_hash=canonical_sha256(
                provisional, exclude=frozenset({"content_hash"})
            ),
        )


class RetrievalAuditRecord(StrictContract):
    """Deterministic record of one evidence access decision."""

    schema_version: Literal[1] = 1
    sequence: NonNegativeInt
    role_id: NonEmptyString
    evidence_id: NonEmptyString
    reason: NonEmptyString
    decision: Literal["GRANTED", "DENIED"]
    reason_code: NonEmptyString
    expected_snapshot_hash: HashRef
    returned_hash: HashRef | None
    returned_size_bytes: NonNegativeInt
    returned_tokens: NonNegativeInt
    record_hash: HashRef

    @model_validator(mode="after")
    def decision_and_identity_are_coherent(self) -> Self:
        if self.decision == "GRANTED" and self.returned_hash is None:
            raise ValueError("granted retrieval requires returned evidence identity")
        if self.decision == "DENIED" and (
            self.returned_hash is not None
            or self.returned_size_bytes != 0
            or self.returned_tokens != 0
        ):
            raise ValueError("denied retrieval cannot expose evidence content")
        if self.record_hash != canonical_sha256(
            self, exclude=frozenset({"record_hash"})
        ):
            raise ValueError("retrieval audit hash is not canonical")
        return self


class InMemoryEvidenceProvider:
    """Verified in-process evidence catalog used by bounded planners."""

    def __init__(
        self,
        objects: Sequence[EvidenceObject],
        *,
        grants: Mapping[str, Sequence[str]],
        policy: PlannerPolicy,
    ) -> None:
        by_id: dict[str, EvidenceObject] = {}
        for item in objects:
            evidence_id = item.evidence_ref.evidence_id
            if evidence_id in by_id:
                raise ValueError(f"duplicate evidence object: {evidence_id}")
            by_id[evidence_id] = item
        self._objects = by_id
        self._grants = {role: frozenset(ids) for role, ids in grants.items()}
        self._policy = policy
        self._audit: list[RetrievalAuditRecord] = []

    @property
    def audit_records(self) -> tuple[RetrievalAuditRecord, ...]:
        return tuple(self._audit)

    def _record(
        self,
        *,
        role_id: str,
        evidence_id: str,
        reason: str,
        expected_snapshot_hash: str,
        decision: Literal["GRANTED", "DENIED"],
        reason_code: str,
        item: EvidenceObject | None = None,
    ) -> None:
        payload = {
            "schema_version": 1,
            "sequence": len(self._audit),
            "role_id": role_id,
            "evidence_id": evidence_id,
            "reason": reason,
            "decision": decision,
            "reason_code": reason_code,
            "expected_snapshot_hash": expected_snapshot_hash,
            "returned_hash": item.content_hash if item else None,
            "returned_size_bytes": item.size_bytes if item else 0,
            "returned_tokens": item.token_estimate if item else 0,
        }
        self._audit.append(
            RetrievalAuditRecord(**payload, record_hash=canonical_sha256(payload))
        )

    def fetch(
        self,
        role_id: str,
        evidence_ids: Sequence[str],
        *,
        expected_snapshot_hash: str,
        reason: str,
    ) -> tuple[EvidenceObject, ...]:
        """Return exact requested objects after every authority check passes."""

        requested = tuple(evidence_ids)
        if len(requested) != len(set(requested)):
            raise ContextIntegrityError("evidence IDs must be unique")
        returned: list[EvidenceObject] = []
        grants = self._grants.get(role_id, frozenset())
        for evidence_id in requested:
            if not _ENTITY_ID.fullmatch(evidence_id):
                self._record(
                    role_id=role_id,
                    evidence_id=evidence_id,
                    reason=reason,
                    expected_snapshot_hash=expected_snapshot_hash,
                    decision="DENIED",
                    reason_code="INVALID_EVIDENCE_ID",
                )
                raise ContextIntegrityError("retrieval requires a stable evidence ID")
            if evidence_id not in grants:
                self._record(
                    role_id=role_id,
                    evidence_id=evidence_id,
                    reason=reason,
                    expected_snapshot_hash=expected_snapshot_hash,
                    decision="DENIED",
                    reason_code="UNAUTHORIZED_EVIDENCE",
                )
                raise ContextIntegrityError(
                    f"evidence {evidence_id} is not authorized for role {role_id}"
                )
            item = self._objects.get(evidence_id)
            if item is None:
                reason_code = "MISSING_EVIDENCE"
            elif item.evidence_ref.snapshot_hash != expected_snapshot_hash:
                reason_code = "STALE_SNAPSHOT"
            elif item.evidence_ref.kind not in self._policy.allowed_evidence_kinds:
                reason_code = "UNAUTHORIZED_KIND"
            elif item.classification not in self._policy.allowed_classifications:
                reason_code = "UNAUTHORIZED_CLASSIFICATION"
            else:
                reason_code = "AUTHORIZED"
            if reason_code != "AUTHORIZED":
                self._record(
                    role_id=role_id,
                    evidence_id=evidence_id,
                    reason=reason,
                    expected_snapshot_hash=expected_snapshot_hash,
                    decision="DENIED",
                    reason_code=reason_code,
                )
                raise ContextIntegrityError(
                    f"evidence {evidence_id} failed {reason_code.lower().replace('_', ' ')}"
                )
            assert item is not None
            self._record(
                role_id=role_id,
                evidence_id=evidence_id,
                reason=reason,
                expected_snapshot_hash=expected_snapshot_hash,
                decision="GRANTED",
                reason_code="AUTHORIZED",
                item=item,
            )
            returned.append(item)
        return tuple(returned)


__all__ = [
    "ContextIntegrityError",
    "EvidenceObject",
    "InMemoryEvidenceProvider",
    "RetrievalAuditRecord",
]
