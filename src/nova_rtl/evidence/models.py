"""Strict internal records shared by M3 evidence-building stages."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    EvidenceRef,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import JsonScalar
from nova_rtl.contracts.optimization import RootCause

EvidenceAttributeName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$"),
]
Confidence = Annotated[
    float,
    Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False),
]
Percentage = Annotated[
    float,
    Field(strict=True, ge=0.0, le=100.0, allow_inf_nan=False),
]
GraphNodeKind = Literal[
    "MODULE",
    "SOURCE_SPAN",
    "PROCESS",
    "EXPRESSION",
    "PORT",
    "NET",
    "PIN",
    "CELL",
    "REGISTER",
    "MEMORY",
    "CLOCK",
    "GENERATED_CLOCK",
    "RESET",
    "ANALYSIS_VIEW",
    "CONSTRAINT_BINDING",
    "RESOLVED_EXCEPTION",
    "TIMING_PATH",
    "PATH_GROUP",
    "VIOLATION",
    "CDC_STRUCTURE",
    "CONSTRAINT",
    "EXCEPTION",
    "OPPORTUNITY",
    "CANDIDATE",
    "PROPOSAL",
    "EVALUATION",
    "PROOF",
    "FAILURE_EVENT",
    "REPAIR_DIRECTIVE",
    "RECOVERY_DECISION",
    "DIAGNOSIS",
    "RECOMMENDATION",
    "CRITIQUE",
    "DELIBERATION",
]
GraphEdgeKind = Literal[
    "CONTAINS",
    "MAPS_TO",
    "CONNECTS",
    "DATA_DEPENDENCY",
    "TIMING_ARC",
    "LAUNCHES",
    "CAPTURES",
    "CLOCKED_BY",
    "GENERATED_FROM",
    "CROSSES_DOMAIN",
    "PROTECTED_BY",
    "NEAR_PROTECTED_BOUNDARY",
    "IN_ANALYSIS_VIEW",
    "MEMBER_OF_PATH_GROUP",
    "HAS_VIOLATION",
    "RESOLVES_CONSTRAINT",
    "PHYSICALLY_NEAR",
]
EvidenceProvenance = Literal[
    "TOOL_MEASURED",
    "DETERMINISTIC_DERIVED",
    "HEURISTIC_INFERRED",
]
ProtectionKind = Literal["CDC", "CLOCK", "RESET"]


def _require_canonical_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    if values != tuple(sorted(values)):
        raise ValueError(f"{label} must be canonically ordered")
    return values


def _validate_evidence_refs(refs: tuple[EvidenceRef, ...]) -> None:
    evidence_ids = tuple(item.evidence_id for item in refs)
    _require_canonical_unique(evidence_ids, "evidence reference IDs")
    if len({item.snapshot_hash for item in refs}) != 1:
        raise ValueError("evidence references must resolve in one snapshot")


def _validate_attributes(
    value: dict[str, JsonScalar],
) -> dict[str, JsonScalar]:
    for name, item in value.items():
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"evidence attribute {name} must be finite")
    return dict(sorted(value.items()))


class AnalysisViewEvidenceIdentity(StrictContract):
    """One required analysis view and its exact OpenSTA evidence identity."""

    analysis_view_id: EntityId
    analysis_view_hash: HashRef
    opensta_stage_result_hash: HashRef


class EvidenceInputIdentity(StrictContract):
    """Complete immutable M2-to-M3 hash boundary."""

    candidate_id: EntityId
    rtl_snapshot_hash: HashRef
    design_contract_hash: HashRef
    constraint_binding_hash: HashRef
    platform_lock_hash: HashRef
    synthesis_structure_hash: HashRef
    clock_inventory_hash: HashRef
    cdc_inventory_hash: HashRef
    protection_policy_hash: HashRef
    analysis_views: tuple[AnalysisViewEvidenceIdentity, ...] = Field(min_length=1)
    identity_hash: HashRef

    @model_validator(mode="after")
    def identity_is_canonical_and_self_hashed(self) -> Self:
        view_ids = tuple(item.analysis_view_id for item in self.analysis_views)
        _require_canonical_unique(view_ids, "analysis view IDs")
        expected_hash = canonical_sha256(self, exclude=frozenset({"identity_hash"}))
        if self.identity_hash != expected_hash:
            raise ValueError("identity_hash does not match the canonical evidence input identity")
        return self


def build_evidence_input_identity(
    *,
    candidate_id: str,
    rtl_snapshot_hash: str,
    design_contract_hash: str,
    constraint_binding_hash: str,
    platform_lock_hash: str,
    synthesis_structure_hash: str,
    clock_inventory_hash: str,
    cdc_inventory_hash: str,
    protection_policy_hash: str,
    analysis_views: Sequence[AnalysisViewEvidenceIdentity],
) -> EvidenceInputIdentity:
    """Normalize required views and produce their canonical aggregate identity."""

    ordered_views = tuple(sorted(analysis_views, key=lambda item: item.analysis_view_id))
    payload = {
        "candidate_id": candidate_id,
        "rtl_snapshot_hash": rtl_snapshot_hash,
        "design_contract_hash": design_contract_hash,
        "constraint_binding_hash": constraint_binding_hash,
        "platform_lock_hash": platform_lock_hash,
        "synthesis_structure_hash": synthesis_structure_hash,
        "clock_inventory_hash": clock_inventory_hash,
        "cdc_inventory_hash": cdc_inventory_hash,
        "protection_policy_hash": protection_policy_hash,
        "analysis_views": tuple(item.model_dump(mode="json") for item in ordered_views),
    }
    return EvidenceInputIdentity(**payload, identity_hash=canonical_sha256(payload))


class SourceSpanRecord(StrictContract):
    """Repository-relative RTL source location with protection context."""

    source_span_id: EntityId
    rtl_snapshot_hash: HashRef
    relative_path: str
    start_line: int = Field(strict=True, ge=1)
    start_column: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    end_column: int = Field(strict=True, ge=1)
    owner_hierarchy: NonEmptyString
    source_text_hash: HashRef
    mapping_confidence: Confidence
    protected: bool
    protection_kinds: tuple[ProtectionKind, ...]

    @field_validator("relative_path")
    @classmethod
    def path_is_normalized_and_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        has_control = any(ord(character) < 32 or ord(character) == 127 for character in value)
        if (
            not value
            or "\\" in value
            or has_control
            or path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or value != path.as_posix()
        ):
            raise ValueError("source path must be a normalized relative path")
        return value

    @field_validator("protection_kinds")
    @classmethod
    def protection_kinds_are_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _require_canonical_unique(value, "protection_kinds")

    @model_validator(mode="after")
    def range_and_protection_are_coherent(self) -> Self:
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("source span end coordinate cannot precede its start coordinate")
        if self.protected != bool(self.protection_kinds):
            raise ValueError("protected must match the presence of protection_kinds")
        return self


class EvidenceNode(StrictContract):
    """One normalized, provenance-bearing graph node."""

    node_id: EntityId
    candidate_id: EntityId
    kind: GraphNodeKind
    semantic_id: EntityId
    attributes: dict[EvidenceAttributeName, JsonScalar]
    provenance: EvidenceProvenance
    confidence: Confidence
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    protected: bool
    protection_kinds: tuple[ProtectionKind, ...]

    @field_validator("attributes")
    @classmethod
    def attributes_are_canonical(
        cls, value: dict[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        return _validate_attributes(value)

    @field_validator("protection_kinds")
    @classmethod
    def protection_kinds_are_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _require_canonical_unique(value, "protection_kinds")

    @model_validator(mode="after")
    def evidence_and_protection_are_coherent(self) -> Self:
        _validate_evidence_refs(self.evidence_refs)
        if self.protected != bool(self.protection_kinds):
            raise ValueError("protected must match the presence of protection_kinds")
        if self.provenance != "HEURISTIC_INFERRED" and self.confidence != 1.0:
            raise ValueError("measured and deterministic graph nodes require confidence 1.0")
        return self


class EvidenceEdge(StrictContract):
    """One typed graph relationship with explicit evidence provenance."""

    edge_id: EntityId
    kind: GraphEdgeKind
    source_node_id: EntityId
    target_node_id: EntityId
    attributes: dict[EvidenceAttributeName, JsonScalar]
    provenance: EvidenceProvenance
    confidence: Confidence
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    @field_validator("attributes")
    @classmethod
    def attributes_are_canonical(
        cls, value: dict[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        return _validate_attributes(value)

    @model_validator(mode="after")
    def relationship_is_coherent(self) -> Self:
        if self.source_node_id == self.target_node_id:
            raise ValueError("evidence graph edges cannot be self-referential")
        _validate_evidence_refs(self.evidence_refs)
        if self.provenance != "HEURISTIC_INFERRED" and self.confidence != 1.0:
            raise ValueError("measured and deterministic graph edges require confidence 1.0")
        return self


class PathFeatureVector(StrictContract):
    """Measured and derived features used by clustering and opportunity ranking."""

    worst_slack_ns: FiniteFloat
    tns_share_percent: Percentage
    affected_endpoints: NonNegativeInt
    cell_delay_fraction: Confidence
    net_delay_fraction: Confidence
    logic_depth: NonNegativeInt
    max_fanout: NonNegativeInt
    mux_depth: NonNegativeInt
    boolean_depth: NonNegativeInt
    comparator_depth: NonNegativeInt
    arithmetic_depth: NonNegativeInt
    repeated_predicate_count: NonNegativeInt
    reconvergence_count: NonNegativeInt
    physical_dominance: Confidence
    source_mapping_confidence: Confidence
    protection_distance: NonNegativeInt | None
    estimated_proof_cost: Confidence

    @model_validator(mode="after")
    def delay_fractions_are_complete(self) -> Self:
        if abs(self.cell_delay_fraction + self.net_delay_fraction - 1.0) > 1e-6:
            raise ValueError("cell and net delay fractions must sum to 1.0")
        return self


class PathCluster(StrictContract):
    """Canonical membership and feature identity for one shared critical cone."""

    cluster_id: EntityId
    candidate_id: EntityId
    evidence_snapshot_hash: HashRef
    target_domain: EntityId
    worst_analysis_view_id: EntityId
    path_ids: tuple[EntityId, ...] = Field(min_length=1)
    analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    dominant_object_ids: tuple[EntityId, ...] = Field(min_length=1)
    source_span_ids: tuple[EntityId, ...]
    protected_neighbor_ids: tuple[EntityId, ...]
    features: PathFeatureVector
    root_causes: tuple[RootCause, ...] = Field(min_length=1)
    cluster_hash: HashRef

    @field_validator(
        "path_ids",
        "analysis_view_ids",
        "dominant_object_ids",
        "source_span_ids",
        "protected_neighbor_ids",
    )
    @classmethod
    def members_are_canonical(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        field_name = getattr(info, "field_name", "cluster members")
        return _require_canonical_unique(value, field_name)

    @model_validator(mode="after")
    def identity_is_self_hashed(self) -> Self:
        if self.worst_analysis_view_id not in self.analysis_view_ids:
            raise ValueError("worst analysis view must be a cluster analysis view")
        categories = tuple(item.category for item in self.root_causes)
        if len(categories) != len(set(categories)):
            raise ValueError("root-cause categories must be unique")
        if tuple(item.confidence for item in self.root_causes) != tuple(
            sorted((item.confidence for item in self.root_causes), reverse=True)
        ):
            raise ValueError("root causes must be ordered by descending confidence")
        expected_hash = canonical_sha256(self, exclude=frozenset({"cluster_hash"}))
        if self.cluster_hash != expected_hash:
            raise ValueError("cluster_hash does not match the canonical path cluster")
        return self


__all__ = [
    "AnalysisViewEvidenceIdentity",
    "EvidenceEdge",
    "EvidenceInputIdentity",
    "EvidenceNode",
    "PathCluster",
    "PathFeatureVector",
    "SourceSpanRecord",
    "build_evidence_input_identity",
]
