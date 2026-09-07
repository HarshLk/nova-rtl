"""Fail-closed implementation of the architecture hard-feasibility predicate."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonNegativeFloat,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.optimization import CandidateRecord


class FeasibilityPolicy(StrictContract):
    """Immutable baseline identities and hard limits used for candidate acceptance."""

    schema_version: Literal[1] = 1
    required_analysis_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    baseline_analysis_view_set_hash: HashRef
    baseline_constraint_source_hash: HashRef
    baseline_generated_clock_graph_hash: HashRef
    max_area_growth_percent: NonNegativeFloat
    policy_hash: HashRef

    @field_validator("required_analysis_view_ids")
    @classmethod
    def views_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("required analysis views must be unique and canonically ordered")
        return value

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.policy_hash != canonical_sha256(self, exclude=frozenset({"policy_hash"})):
            raise ValueError("feasibility policy hash is not canonical")
        return self

    @classmethod
    def build(
        cls,
        *,
        required_analysis_view_ids: tuple[str, ...],
        baseline_analysis_view_set_hash: str,
        baseline_constraint_source_hash: str,
        baseline_generated_clock_graph_hash: str,
        max_area_growth_percent: float,
    ) -> FeasibilityPolicy:
        payload = {
            "schema_version": 1,
            "required_analysis_view_ids": tuple(sorted(required_analysis_view_ids)),
            "baseline_analysis_view_set_hash": baseline_analysis_view_set_hash,
            "baseline_constraint_source_hash": baseline_constraint_source_hash,
            "baseline_generated_clock_graph_hash": baseline_generated_clock_graph_hash,
            "max_area_growth_percent": max_area_growth_percent,
        }
        return cls(**payload, policy_hash=canonical_sha256(payload))


class CandidateFeasibilityEvidence(StrictContract):
    """Complete authoritative facts required by the hard-feasibility expression."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    source_hash: HashRef
    mandatory_proof_outcome: Literal["PASS", "FAIL", "INCONCLUSIVE", "INFRASTRUCTURE_ERROR"]
    proof_contract: CorrectnessContract
    analysis_view_set_hash: HashRef
    constraint_source_hash: HashRef
    effective_constraint_binding: Literal[
        "EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"
    ]
    unresolved_constraint_selectors: NonNegativeInt
    generated_clock_graph_hash: HashRef
    new_or_unapproved_cdc_crossings: NonNegativeInt
    changed_approved_cdc_structures: NonNegativeInt
    unconstrained_endpoints: NonNegativeInt
    area_growth_percent: float = Field(strict=True, allow_inf_nan=False)
    view_complete: dict[EntityId, bool] = Field(min_length=1)
    view_hard_limits_pass: dict[EntityId, bool] = Field(min_length=1)
    view_metrics_present: dict[EntityId, bool] = Field(min_length=1)
    no_hard_domain_regression: bool
    evidence_hash: HashRef

    @field_validator("view_complete", "view_hard_limits_pass", "view_metrics_present")
    @classmethod
    def view_maps_are_canonical(cls, value: dict[str, bool]) -> dict[str, bool]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def view_sets_and_hash_are_canonical(self) -> Self:
        view_sets = (
            set(self.view_complete),
            set(self.view_hard_limits_pass),
            set(self.view_metrics_present),
        )
        if len({frozenset(items) for items in view_sets}) != 1:
            raise ValueError("feasibility view maps must cover the same exact view set")
        if self.evidence_hash != canonical_sha256(
            self, exclude=frozenset({"evidence_hash"})
        ):
            raise ValueError("candidate feasibility evidence hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> CandidateFeasibilityEvidence:
        payload = {"schema_version": 1, **values}
        for field_name in (
            "view_complete",
            "view_hard_limits_pass",
            "view_metrics_present",
        ):
            raw = payload.get(field_name)
            if isinstance(raw, dict):
                payload[field_name] = dict(sorted(raw.items()))
        return cls(**payload, evidence_hash=canonical_sha256(payload))


def is_feasible(
    candidate: CandidateRecord,
    evidence: CandidateFeasibilityEvidence,
    policy: FeasibilityPolicy,
) -> bool:
    """Return true only when every mandatory strict-equivalence predicate passes."""

    required_views = set(policy.required_analysis_view_ids)
    return all(
        (
            candidate.candidate_id == evidence.candidate_id,
            candidate.source_hash == evidence.source_hash,
            candidate.selection_class == "PRIMARY_STRICT",
            candidate.required_correctness_contract == "STRICT_SEQ_EQUIV",
            evidence.proof_contract == "STRICT_SEQ_EQUIV",
            evidence.mandatory_proof_outcome == "PASS",
            set(evidence.view_complete) == required_views,
            all(evidence.view_complete.values()),
            all(evidence.view_hard_limits_pass.values()),
            all(evidence.view_metrics_present.values()),
            evidence.analysis_view_set_hash == policy.baseline_analysis_view_set_hash,
            evidence.constraint_source_hash == policy.baseline_constraint_source_hash,
            evidence.effective_constraint_binding
            in {"EQUIVALENT", "APPROVED_SEMANTIC_REMAP"},
            evidence.unresolved_constraint_selectors == 0,
            evidence.generated_clock_graph_hash
            == policy.baseline_generated_clock_graph_hash,
            evidence.new_or_unapproved_cdc_crossings == 0,
            evidence.changed_approved_cdc_structures == 0,
            evidence.unconstrained_endpoints == 0,
            evidence.area_growth_percent <= policy.max_area_growth_percent,
            evidence.no_hard_domain_regression,
        )
    )


__all__ = [
    "CandidateFeasibilityEvidence",
    "FeasibilityPolicy",
    "is_feasible",
]
