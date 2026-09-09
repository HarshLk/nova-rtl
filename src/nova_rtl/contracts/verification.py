"""Constraint-binding and formal-verification contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)

CanonicalObjectId = Annotated[str, StringConstraints(min_length=1)]
ProofStrategy = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,95}$"),
]
ProofOutcome = Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]


class ResolvedConstraintCommand(StrictContract):
    command_id: EntityId
    normalized_command_hash: HashRef
    resolved_object_ids: tuple[CanonicalObjectId, ...] = Field(min_length=1)
    resolved_object_set_hash: HashRef

    @model_validator(mode="after")
    def resolved_set_is_sorted_unique_and_hashed(self) -> Self:
        if self.resolved_object_ids != tuple(sorted(self.resolved_object_ids)):
            raise ValueError("resolved object IDs must be sorted")
        if len(self.resolved_object_ids) != len(set(self.resolved_object_ids)):
            raise ValueError("resolved object IDs must be unique")
        expected_hash = canonical_sha256(
            {"resolved_object_ids": list(self.resolved_object_ids)}
        )
        if self.resolved_object_set_hash != expected_hash:
            raise ValueError("resolved_object_set_hash does not match resolved object IDs")
        return self


class ConstraintCoverage(StrictContract):
    sequential_endpoints_total: NonNegativeInt
    timed_endpoints: NonNegativeInt
    reviewed_exception_endpoints: NonNegativeInt
    unresolved_selectors: NonNegativeInt

    @model_validator(mode="after")
    def coverage_is_complete(self) -> Self:
        if self.unresolved_selectors != 0:
            raise ValueError("unresolved_selectors must be zero")
        if self.timed_endpoints + self.reviewed_exception_endpoints != (
            self.sequential_endpoints_total
        ):
            raise ValueError(
                "timed_endpoints + reviewed_exception_endpoints must equal "
                "sequential_endpoints_total"
            )
        return self


class ConstraintBindingManifest(StrictContract):
    """Effective SDC selector binding and endpoint coverage for one snapshot."""

    schema_version: Literal[1] = 1
    binding_manifest_id: EntityId
    candidate_id: EntityId
    sdc_hash: HashRef
    netlist_snapshot_hash: HashRef
    analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    resolved_commands: tuple[ResolvedConstraintCommand, ...] = Field(min_length=1)
    coverage: ConstraintCoverage
    effective_binding_hash: HashRef
    comparison_to_baseline: Literal[
        "EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"
    ]
    reviewed_mapping_refs: tuple[EntityId, ...]

    @field_validator("analysis_view_ids", "reviewed_mapping_refs")
    @classmethod
    def id_sequences_are_sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)):
            raise ValueError("identity sequences must be sorted")
        if len(value) != len(set(value)):
            raise ValueError("identity sequences must be unique")
        return value

    @model_validator(mode="after")
    def binding_identity_and_mapping_are_consistent(self) -> Self:
        command_ids = tuple(item.command_id for item in self.resolved_commands)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("resolved command IDs must be unique")
        if self.comparison_to_baseline == "EQUIVALENT" and self.reviewed_mapping_refs:
            raise ValueError("EQUIVALENT binding cannot declare reviewed mappings")
        if (
            self.comparison_to_baseline == "APPROVED_SEMANTIC_REMAP"
            and not self.reviewed_mapping_refs
        ):
            raise ValueError("APPROVED_SEMANTIC_REMAP requires reviewed mapping refs")
        expected_hash = canonical_sha256(self, exclude=frozenset({"effective_binding_hash"}))
        if self.effective_binding_hash != expected_hash:
            raise ValueError("effective_binding_hash does not match canonical binding identity")
        return self


class FormalModelContract(StrictContract):
    """Immutable functional/formal identity and multi-clock assumption model."""

    schema_version: Literal[1] = 1
    formal_model_contract_id: EntityId
    candidate_id: EntityId
    functional_rtl_hash: HashRef
    parameter_hash: HashRef
    gold_snapshot_hash: HashRef
    gate_snapshot_hash: HashRef | None
    property_manifest_hash: HashRef
    master_clock_model: Literal["INDEPENDENT_SHARED_GOLD_GATE_EVENTS"]
    generated_clock_model: Literal["DERIVED_FROM_PROTECTED_DIVIDER_STATE"]
    multiclock_enabled: Literal[True]
    reset_assumption_hash: HashRef
    environment_assumption_hash: HashRef
    proof_scope_policy: Literal["WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED"]
    behavioral_elaboration_delta: Literal["NONE"]

    @model_validator(mode="after")
    def candidate_has_a_gate_snapshot(self) -> Self:
        if self.candidate_id != "baseline" and self.gate_snapshot_hash is None:
            raise ValueError("candidate FormalModelContract requires gate_snapshot_hash")
        return self


class ProofPartition(StrictContract):
    partition_id: EntityId
    status: ProofOutcome
    runtime_ms: NonNegativeInt
    strategy: ProofStrategy
    artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def artifacts_are_unique(self) -> Self:
        artifact_ids = tuple(item.artifact_id for item in self.artifact_refs)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("proof partition artifact IDs must be unique")
        return self


class CompositionClosureManifest(StrictContract):
    """Evidence that a local proof closes over the exact delivered RTL snapshot."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    parent_full_snapshot_hash: HashRef
    candidate_full_snapshot_hash: HashRef
    changed_source_paths: tuple[str, ...] = Field(min_length=1)
    changed_span_ids: tuple[EntityId, ...] = Field(min_length=1)
    changed_parent_hashes: dict[str, HashRef]
    changed_candidate_hashes: dict[str, HashRef]
    unchanged_source_hashes: dict[str, HashRef]
    proof_plan_hash: HashRef
    proof_gold_hash: HashRef
    proof_gate_hash: HashRef
    proof_top: NonEmptyString
    parameterizations: tuple[NonEmptyString, ...] = Field(min_length=1)
    boundary_inputs: tuple[NonEmptyString, ...] = Field(min_length=1)
    boundary_outputs: tuple[NonEmptyString, ...] = Field(min_length=1)
    state_elements: tuple[NonEmptyString, ...]
    assumption_hashes: tuple[HashRef, ...]
    discharge_obligations: tuple[NonEmptyString, ...] = Field(min_length=1)
    manifest_hash: HashRef

    @field_validator(
        "changed_source_paths",
        "changed_span_ids",
        "parameterizations",
        "boundary_inputs",
        "boundary_outputs",
        "state_elements",
        "assumption_hashes",
        "discharge_obligations",
    )
    @classmethod
    def sequences_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("composition manifest sequences must be sorted and unique")
        return value

    @field_validator(
        "changed_parent_hashes",
        "changed_candidate_hashes",
        "unchanged_source_hashes",
    )
    @classmethod
    def source_maps_are_canonical(cls, value: dict[str, str]) -> dict[str, str]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def closure_and_hash_are_coherent(self) -> Self:
        changed = set(self.changed_source_paths)
        if (
            set(self.changed_parent_hashes) != changed
            or set(self.changed_candidate_hashes) != changed
        ):
            raise ValueError("composition changed-source maps must cover the exact edit set")
        if changed & set(self.unchanged_source_hashes):
            raise ValueError("changed and unchanged composition sources must be disjoint")
        if any(
            self.changed_parent_hashes[path] == self.changed_candidate_hashes[path]
            for path in changed
        ):
            raise ValueError("composition changed-source hashes must differ")
        if self.manifest_hash != canonical_sha256(self, exclude=frozenset({"manifest_hash"})):
            raise ValueError("composition manifest hash is not canonical")
        return self


class ProofResult(StrictContract):
    """Formal outcome, scope, partition evidence, and exact proved identities."""

    schema_version: Literal[1] = 1
    proof_result_id: EntityId
    run_id: EntityId
    candidate_id: EntityId
    contract: Literal["STRICT_SEQ_EQUIV", "RETIMING_EQUIV", "LATENCY_AWARE"]
    outcome: ProofOutcome
    formal_model_contract_id: EntityId
    gold_hash: HashRef
    gate_hash: HashRef
    proof_scope: Literal["WHOLE_DESIGN", "COMPOSITIONALLY_CLOSED"]
    partitions: tuple[ProofPartition, ...] = Field(min_length=1)
    composition_manifest_artifact_id: EntityId | None
    assumption_hashes: tuple[HashRef, ...]
    counterexample_artifact_id: EntityId | None
    raw_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    runtime_ms: NonNegativeInt

    @field_validator("assumption_hashes")
    @classmethod
    def assumptions_are_sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)):
            raise ValueError("assumption hashes must be sorted")
        if len(value) != len(set(value)):
            raise ValueError("assumption hashes must be unique")
        return value

    @model_validator(mode="after")
    def outcome_scope_and_artifacts_are_coherent(self) -> Self:
        partition_ids = tuple(item.partition_id for item in self.partitions)
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("proof partition IDs must be unique")
        if self.outcome == "PASS" and any(item.status != "PASS" for item in self.partitions):
            raise ValueError("PASS outcome requires every partition to PASS")
        if self.outcome == "FAIL" and not any(item.status == "FAIL" for item in self.partitions):
            raise ValueError("FAIL outcome requires a failed partition")
        if self.outcome == "INCONCLUSIVE" and not any(
            item.status == "INCONCLUSIVE" for item in self.partitions
        ):
            raise ValueError("INCONCLUSIVE outcome requires an inconclusive partition")
        if self.outcome == "INFRASTRUCTURE_ERROR" and not any(
            item.status == "INFRASTRUCTURE_ERROR" for item in self.partitions
        ):
            raise ValueError(
                "INFRASTRUCTURE_ERROR outcome requires an infrastructure-error partition"
            )

        raw_ids = tuple(item.artifact_id for item in self.raw_artifacts)
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("raw proof artifact IDs must be unique")
        raw_id_set = set(raw_ids)
        raw_by_id = {item.artifact_id: item for item in self.raw_artifacts}
        if any(
            raw_by_id.get(artifact.artifact_id) != artifact
            for partition in self.partitions
            for artifact in partition.artifact_refs
        ):
            raise ValueError("every partition artifact must resolve exactly in raw_artifacts")
        if self.proof_scope == "COMPOSITIONALLY_CLOSED":
            if self.composition_manifest_artifact_id is None:
                raise ValueError("composition manifest is required for closed compositional proof")
            if self.composition_manifest_artifact_id not in raw_id_set:
                raise ValueError("composition manifest must resolve in raw proof artifacts")
        elif self.composition_manifest_artifact_id is not None:
            raise ValueError("whole-design proof cannot declare a composition manifest")
        if (
            self.counterexample_artifact_id is not None
            and self.counterexample_artifact_id not in raw_id_set
        ):
            raise ValueError("counterexample must resolve in raw proof artifacts")
        if self.outcome == "PASS" and self.counterexample_artifact_id is not None:
            raise ValueError("PASS outcome cannot reference a counterexample")

        partition_runtime = sum(item.runtime_ms for item in self.partitions)
        if self.runtime_ms < partition_runtime:
            raise ValueError("proof runtime cannot be shorter than aggregate partition runtime")
        return self


__all__ = [
    "CompositionClosureManifest",
    "ConstraintBindingManifest",
    "FormalModelContract",
    "ProofResult",
]
