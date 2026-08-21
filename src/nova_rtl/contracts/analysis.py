"""Immutable analysis-view and power-activity identities."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    StrictContract,
    canonical_sha256,
)

StableUpperString = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,95}$"),
]
HierarchyPath = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.$/]*$"),
]


class CornerArtifactIdentity(StrictContract):
    id: EntityId
    artifact_hash: HashRef


class AnalysisViewContract(StrictContract):
    """Complete immutable identity for one official timing, physical, or power view."""

    schema_version: Literal[1] = 1
    analysis_view_id: EntityId
    mode: Literal["FUNCTIONAL"]
    check: Literal["SETUP", "HOLD", "POWER"]
    required: bool
    liberty_corner: CornerArtifactIdentity
    rc_corner: CornerArtifactIdentity
    sdc_hash: HashRef
    operating_condition: NonEmptyString
    derate_policy_hash: HashRef
    clock_uncertainty_policy_hash: HashRef
    hard_limits: dict[str, FiniteFloat]
    required_stages: tuple[StableUpperString, ...] = Field(min_length=1)
    power_activity_contract_id: EntityId | None

    @field_validator("required_stages")
    @classmethod
    def stages_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required stages must be unique")
        return value

    @model_validator(mode="after")
    def view_requirements_match_check(self) -> Self:
        limit = {
            "SETUP": "setup_wns_ns",
            "HOLD": "hold_wns_ns",
            "POWER": "power_total_uw",
        }[self.check]
        if limit not in self.hard_limits:
            raise ValueError(f"{self.check} analysis view requires hard limit {limit}")
        if self.check == "POWER":
            if self.power_activity_contract_id is None:
                raise ValueError("POWER analysis view requires power_activity_contract_id")
        elif self.power_activity_contract_id is not None:
            raise ValueError("non-power analysis view cannot declare power_activity_contract_id")

        if self.required and self.check in {"SETUP", "HOLD"}:
            if "OPENSTA_FULL" not in self.required_stages:
                raise ValueError("required timing view must include OPENSTA_FULL")
            if not any(stage.startswith("OPENROAD_") for stage in self.required_stages):
                raise ValueError("required timing view must include a physical stage")
        return self


class PowerActivityContract(StrictContract):
    """Exact activity source and semantics required for comparable power evidence."""

    schema_version: Literal[1] = 1
    power_activity_contract_id: EntityId
    format: Literal["VCD", "SAIF", "VECTORLESS"]
    source_artifact: ArtifactRef | None
    scope: HierarchyPath
    time_window_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    propagation_policy: Literal["ANNOTATED", "PROPAGATED", "VECTORLESS_ESTIMATE"]
    comparability: Literal["OFFICIAL_COMPARABLE", "ESTIMATED_ONLY_NOT_COMPARABLE"]
    contract_hash: HashRef

    @model_validator(mode="after")
    def activity_semantics_and_hash_are_consistent(self) -> Self:
        start, end = self.time_window_ns
        if end <= start:
            raise ValueError("power activity time window end must be greater than start")
        if self.format in {"VCD", "SAIF"}:
            if self.source_artifact is None:
                raise ValueError(f"{self.format} power activity requires source_artifact")
            if self.propagation_policy == "VECTORLESS_ESTIMATE":
                raise ValueError("annotated activity cannot use VECTORLESS_ESTIMATE")
        else:
            if self.source_artifact is not None:
                raise ValueError("VECTORLESS power activity cannot declare source_artifact")
            if self.propagation_policy != "VECTORLESS_ESTIMATE":
                raise ValueError("VECTORLESS power activity requires VECTORLESS_ESTIMATE")
            if self.comparability != "ESTIMATED_ONLY_NOT_COMPARABLE":
                raise ValueError("VECTORLESS power activity is not officially comparable")

        expected_hash = canonical_sha256(self, exclude=frozenset({"contract_hash"}))
        if self.contract_hash != expected_hash:
            raise ValueError("contract_hash does not match canonical power activity identity")
        return self


class CriticalPathRecord(StrictContract):
    """View-keyed normalized timing path with parser arithmetic checks."""

    schema_version: Literal[1] = 1
    path_id: EntityId
    candidate_id: EntityId
    analysis_view_id: EntityId
    path_group: NonEmptyString
    launch_clock_id: EntityId
    capture_clock_id: EntityId
    startpoint: NonEmptyString
    endpoint: NonEmptyString
    arrival_ns: FiniteFloat
    required_ns: FiniteFloat
    slack_ns: FiniteFloat
    cell_delay_ns: NonNegativeFloat
    net_delay_ns: NonNegativeFloat
    logic_depth: NonNegativeInt
    max_fanout: NonNegativeInt
    object_sequence: tuple[NonEmptyString, ...] = Field(min_length=1)
    source_span_refs: tuple[EntityId, ...]
    raw_report_artifact_id: EntityId

    @model_validator(mode="after")
    def timing_arithmetic_and_sequences_are_consistent(self) -> Self:
        expected_slack = self.required_ns - self.arrival_ns
        if abs(self.slack_ns - expected_slack) > 1e-6:
            raise ValueError("slack_ns must equal required_ns - arrival_ns within parser tolerance")
        if len(self.source_span_refs) != len(set(self.source_span_refs)):
            raise ValueError("source_span_refs must be unique")
        return self


class MasterClockEntry(StrictContract):
    clock_id: EntityId
    domain_id: EntityId
    source_object: NonEmptyString
    period_ns: PositiveFloat
    waveform_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    active_consumer_count: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def waveform_fits_period(self) -> Self:
        start, end = self.waveform_ns
        if end <= start or end > self.period_ns:
            raise ValueError("master clock waveform must increase and fit within its period")
        return self


class GeneratedClockEntry(StrictContract):
    clock_id: EntityId
    domain_id: EntityId
    master_clock_id: EntityId
    source_object: NonEmptyString
    multiply_by: int = Field(strict=True, gt=0)
    divide_by: int = Field(strict=True, gt=0)
    waveform_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    active_consumer_count: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def waveform_increases(self) -> Self:
        if self.waveform_ns[1] <= self.waveform_ns[0]:
            raise ValueError("generated clock waveform must increase")
        return self


class ClockLineageEdge(StrictContract):
    parent_clock_id: EntityId
    child_clock_id: EntityId
    relationship: Literal["GENERATED_FROM"]


class CrossMasterSynchronousRelationship(StrictContract):
    first_master_clock_id: EntityId
    second_master_clock_id: EntityId
    declaration_hash: HashRef


class ClockInventory(StrictContract):
    """Complete five-master generated-clock inventory and lineage graph."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    expected_master_count: Literal[5]
    expected_generated_clocks_per_master: int = Field(strict=True, gt=0)
    master_clocks: tuple[MasterClockEntry, ...] = Field(min_length=5)
    generated_clocks: tuple[GeneratedClockEntry, ...] = Field(min_length=1)
    lineage_edges: tuple[ClockLineageEdge, ...] = Field(min_length=1)
    cross_master_synchronous_relationships: tuple[CrossMasterSynchronousRelationship, ...]
    clock_graph_hash: HashRef

    @model_validator(mode="after")
    def graph_is_complete_and_asynchronous(self) -> Self:
        if len(self.master_clocks) != self.expected_master_count:
            raise ValueError("master clock count does not match expected_master_count")
        master_ids = tuple(item.clock_id for item in self.master_clocks)
        generated_ids = tuple(item.clock_id for item in self.generated_clocks)
        if len(master_ids) != len(set(master_ids)):
            raise ValueError("master clock IDs must be unique")
        if len(generated_ids) != len(set(generated_ids)):
            raise ValueError("generated clock IDs must be unique")
        if set(master_ids) & set(generated_ids):
            raise ValueError("master and generated clock IDs must be disjoint")
        if self.cross_master_synchronous_relationships:
            raise ValueError("the five master clocks must remain mutually asynchronous")

        expected_generated_total = (
            self.expected_master_count * self.expected_generated_clocks_per_master
        )
        if len(self.generated_clocks) != expected_generated_total:
            raise ValueError("generated clock count does not match the configured per-master count")
        per_master = {clock_id: 0 for clock_id in master_ids}
        for generated in self.generated_clocks:
            if generated.master_clock_id not in per_master:
                raise ValueError("generated clock must resolve to exactly one master ancestor")
            per_master[generated.master_clock_id] += 1
        if any(
            count != self.expected_generated_clocks_per_master for count in per_master.values()
        ):
            raise ValueError("generated clock count is not uniform per master")

        expected_edges = {
            (item.master_clock_id, item.clock_id) for item in self.generated_clocks
        }
        actual_edges = {
            (item.parent_clock_id, item.child_clock_id) for item in self.lineage_edges
        }
        if len(self.lineage_edges) != len(actual_edges) or actual_edges != expected_edges:
            raise ValueError("lineage edges must give every generated clock one master ancestor")

        expected_hash = canonical_sha256(self, exclude=frozenset({"clock_graph_hash"}))
        if self.clock_graph_hash != expected_hash:
            raise ValueError("clock_graph_hash does not match canonical clock inventory")
        return self


class CdcCrossing(StrictContract):
    crossing_id: EntityId
    source_domain_id: EntityId
    destination_domain_id: EntityId
    source_object: NonEmptyString
    destination_object: NonEmptyString
    signal_class: Literal[
        "SINGLE_BIT_CONTROL",
        "MULTI_BIT_DATA",
        "PULSE",
        "FIFO_POINTER",
        "RESET",
    ]
    recognized_pattern: EntityId | None
    structural_fingerprint: HashRef
    protocol_property_refs: tuple[EntityId, ...]
    status: Literal["APPROVED", "UNAPPROVED", "AMBIGUOUS"]

    @model_validator(mode="after")
    def crossing_is_structurally_coherent(self) -> Self:
        if self.source_domain_id == self.destination_domain_id:
            raise ValueError("CDC crossing must connect distinct domains")
        if self.status == "APPROVED" and self.recognized_pattern is None:
            raise ValueError("approved crossing requires a recognized pattern")
        if len(self.protocol_property_refs) != len(set(self.protocol_property_refs)):
            raise ValueError("protocol_property_refs must be unique")
        return self


class CDCInventory(StrictContract):
    """Typed structural CDC inventory and comparison counts."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    crossings: tuple[CdcCrossing, ...]
    approved_pattern_registry_hash: HashRef
    new_unapproved_count: NonNegativeInt
    changed_approved_structure_count: NonNegativeInt
    removed_approved_structure_count: NonNegativeInt
    ambiguous_count: NonNegativeInt
    inventory_hash: HashRef

    @model_validator(mode="after")
    def counts_and_identity_are_consistent(self) -> Self:
        crossing_ids = tuple(item.crossing_id for item in self.crossings)
        if len(crossing_ids) != len(set(crossing_ids)):
            raise ValueError("CDC crossing IDs must be unique")
        ambiguous = sum(item.status == "AMBIGUOUS" for item in self.crossings)
        unapproved = sum(item.status == "UNAPPROVED" for item in self.crossings)
        if self.ambiguous_count != ambiguous:
            raise ValueError("ambiguous_count must match the inventory")
        if self.new_unapproved_count > unapproved:
            raise ValueError("new_unapproved_count cannot exceed unapproved crossings")
        expected_hash = canonical_sha256(self, exclude=frozenset({"inventory_hash"}))
        if self.inventory_hash != expected_hash:
            raise ValueError("inventory_hash does not match canonical CDC inventory")
        return self


class EvidenceGraphSnapshot(StrictContract):
    """Immutable identity and artifact index for a compressed evidence graph."""

    schema_version: Literal[1] = 1
    snapshot_hash: HashRef
    node_schema_version: Literal[1]
    edge_schema_version: Literal[1]
    node_counts: dict[StableUpperString, NonNegativeInt]
    edge_counts: dict[StableUpperString, NonNegativeInt]
    graph_artifact: ArtifactRef
    source_map_artifact: ArtifactRef
    path_record_artifact: ArtifactRef
    clock_inventory_id: EntityId
    cdc_inventory_id: EntityId
    evidence_resolution_index_hash: HashRef

    @model_validator(mode="after")
    def artifacts_and_identity_are_consistent(self) -> Self:
        artifacts = (
            self.graph_artifact,
            self.source_map_artifact,
            self.path_record_artifact,
        )
        artifact_ids = tuple(item.artifact_id for item in artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("evidence graph artifact IDs must be unique")
        if self.graph_artifact.media_type != "application/zstd":
            raise ValueError("compressed evidence graph must use application/zstd")
        expected_hash = canonical_sha256(self, exclude=frozenset({"snapshot_hash"}))
        if self.snapshot_hash != expected_hash:
            raise ValueError("snapshot_hash does not match canonical evidence graph identity")
        return self


__all__ = [
    "AnalysisViewContract",
    "CDCInventory",
    "ClockInventory",
    "CriticalPathRecord",
    "EvidenceGraphSnapshot",
    "PowerActivityContract",
]
