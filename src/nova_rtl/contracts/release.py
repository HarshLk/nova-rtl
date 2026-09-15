"""Canonical contracts for M9 evaluation, presentation, replay, and release."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeFloat,
    StrictContract,
    UtcDatetime,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.optimization import SelectionClass

AuthorityClass = Literal[
    "ADVISORY_AI",
    "DETERMINISTIC_POLICY",
    "TRUTH_GATE",
    "MEASURED_EDA",
    "HISTORICAL_EVIDENCE",
    "RECOVERY_DECISION",
]
AblationVariant = Literal["H", "S", "MC", "F0", "F4"]


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


class EvidenceClaim(StrictContract):
    claim_id: EntityId
    label: NonEmptyString
    value: NonEmptyString
    authority: AuthorityClass
    artifact_ids: tuple[EntityId, ...] = Field(min_length=1)
    evidence_ids: tuple[EntityId, ...] = Field(min_length=1)
    comparison_identity_hashes: dict[str, HashRef] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_resolvable(self) -> Self:
        _unique(self.artifact_ids, "claim artifact IDs")
        _unique(self.evidence_ids, "claim evidence IDs")
        if not set(self.evidence_ids).issubset(self.artifact_ids):
            raise ValueError("evidence artifacts must be declared by the claim")
        return self


class ReplayManifest(StrictContract):
    schema_version: Literal[1] = 1
    replay_id: EntityId
    run_id: EntityId
    event_artifact_ids: tuple[EntityId, ...]
    required_artifact_ids: tuple[EntityId, ...] = Field(min_length=1)
    ledger_hash: HashRef
    terminal_state_hash: HashRef
    external_calls_allowed: Literal[False] = False
    manifest_hash: HashRef

    @model_validator(mode="after")
    def replay_is_closed_and_self_hashed(self) -> Self:
        _unique(self.event_artifact_ids, "replay event artifact IDs")
        _unique(self.required_artifact_ids, "replay required artifact IDs")
        if self.manifest_hash != canonical_sha256(
            self, exclude=frozenset({"manifest_hash"})
        ):
            raise ValueError("manifest_hash does not match canonical replay manifest")
        return self


class FrequencySweepTrial(StrictContract):
    trial_id: EntityId
    period_ns: FiniteFloat = Field(gt=0)
    frequency_mhz: FiniteFloat = Field(gt=0)
    setup_status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    hold_status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    setup_stage_result_id: EntityId
    hold_stage_result_id: EntityId
    overlay_hash: HashRef


class FrequencySweepResult(StrictContract):
    schema_version: Literal[1] = 1
    sweep_id: EntityId
    candidate_id: EntityId
    contract_hash: HashRef
    trials: tuple[FrequencySweepTrial, ...] = Field(min_length=1)
    passing_trial_ids: tuple[EntityId, ...]
    fmax_mhz: FiniteFloat | None
    result_hash: HashRef

    @model_validator(mode="after")
    def trials_are_ordered_closed_and_self_hashed(self) -> Self:
        trial_ids = tuple(item.trial_id for item in self.trials)
        _unique(trial_ids, "frequency trial IDs")
        if tuple(item.period_ns for item in self.trials) != tuple(
            sorted(item.period_ns for item in self.trials)
        ):
            raise ValueError("frequency trials must be ordered by increasing period")
        passing = set(self.passing_trial_ids)
        if not passing.issubset(trial_ids):
            raise ValueError("passing sweep IDs must resolve to trials")
        for trial in self.trials:
            if trial.trial_id in passing and (
                trial.setup_status != "PASS" or trial.hold_status != "PASS"
            ):
                raise ValueError("passing sweep point requires setup and hold PASS")
        if bool(passing) != (self.fmax_mhz is not None):
            raise ValueError("fmax_mhz is present exactly when a sweep point passes")
        if passing:
            expected_fmax = max(
                item.frequency_mhz for item in self.trials if item.trial_id in passing
            )
            if self.fmax_mhz != expected_fmax:
                raise ValueError("fmax_mhz must equal the fastest passing trial")
        if self.result_hash != canonical_sha256(self, exclude=frozenset({"result_hash"})):
            raise ValueError("result_hash does not match canonical frequency sweep")
        return self


class AblationComparison(StrictContract):
    schema_version: Literal[1] = 1
    comparison_id: EntityId
    baseline_variant: AblationVariant
    contender_variant: AblationVariant
    baseline_budget_hash: HashRef
    contender_budget_hash: HashRef
    baseline_feasible_yield: NonNegativeFloat
    contender_feasible_yield: NonNegativeFloat
    baseline_best_strict_ppa: FiniteFloat
    contender_best_strict_ppa: FiniteFloat
    schema_valid_rate: NonNegativeFloat = Field(le=1)
    trace_completeness: NonNegativeFloat = Field(le=1)
    median_latency_seconds: NonNegativeFloat
    cost_ratio: NonNegativeFloat
    adopted: bool
    reasons: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def comparison_is_fair(self) -> Self:
        if self.baseline_variant == self.contender_variant:
            raise ValueError("ablation variants must differ")
        if self.baseline_budget_hash != self.contender_budget_hash:
            raise ValueError("ablation variants require identical EDA budgets")
        if self.adopted and self.reasons:
            raise ValueError("an adopted contender cannot retain rejection reasons")
        if not self.adopted and not self.reasons:
            raise ValueError("a non-adopted contender requires reasons")
        return self


class FinalCandidateSeal(StrictContract):
    schema_version: Literal[1] = 1
    candidate_id: EntityId
    selection_class: SelectionClass
    correctness_contract: CorrectnessContract
    source_hash: HashRef
    patch_hash: HashRef
    constraint_hash: HashRef
    binding_hash: HashRef
    clock_hash: HashRef
    cdc_hash: HashRef
    formal_hash: HashRef
    platform_hash: HashRef
    recipe_hash: HashRef
    required_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    completed_view_ids: tuple[EntityId, ...] = Field(min_length=1)
    strict_proof_outcome: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    seal_hash: HashRef

    @field_validator("required_view_ids", "completed_view_ids")
    @classmethod
    def views_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "candidate analysis views")

    @model_validator(mode="after")
    def primary_is_strict_complete_and_self_hashed(self) -> Self:
        if self.selection_class == "PRIMARY_STRICT" and (
            self.correctness_contract != "STRICT_SEQ_EQUIV"
        ):
            raise ValueError("PRIMARY_STRICT requires STRICT_SEQ_EQUIV")
        if self.selection_class == "PRIMARY_STRICT" and self.strict_proof_outcome != "PASS":
            raise ValueError("PRIMARY_STRICT requires a passing strict proof")
        if set(self.required_view_ids) != set(self.completed_view_ids):
            raise ValueError("final candidate must complete the exact required views")
        if self.seal_hash != canonical_sha256(self, exclude=frozenset({"seal_hash"})):
            raise ValueError("seal_hash does not match canonical final candidate")
        return self


class M9SignoffReport(StrictContract):
    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    implementation_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    m8_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    m8_packet_hash: HashRef
    report_bundle_hash: HashRef
    replay_manifest_hash: HashRef
    final_candidate_seal_hash: HashRef
    ablation_hash: HashRef
    frequency_sweep_hash: HashRef
    acceptance_hash: HashRef
    submission_bundle_hash: HashRef
    gate_evidence_hash: HashRef
    created_at: UtcDatetime
    report_hash: HashRef

    @model_validator(mode="after")
    def report_is_self_hashed(self) -> Self:
        if self.report_hash != canonical_sha256(self, exclude=frozenset({"report_hash"})):
            raise ValueError("report_hash does not match canonical M9 report")
        return self


__all__ = [
    "AblationComparison",
    "AuthorityClass",
    "EvidenceClaim",
    "FinalCandidateSeal",
    "FrequencySweepResult",
    "FrequencySweepTrial",
    "M9SignoffReport",
    "ReplayManifest",
]
