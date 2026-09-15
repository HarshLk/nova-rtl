"""Executable M9 acceptance gates and required failure-injection matrix."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from nova_rtl.contracts.base import (
    HashRef,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.recovery import FailureFamily, RecoveryAction
from nova_rtl.recovery.router import DEFAULT_ACTION

OptimizationOutcome = Literal["IMPROVED", "VALID_NEGATIVE_RESULT"]


class AcceptanceEvidence(StrictContract):
    master_clock_count: NonNegativeInt
    generated_clock_count: NonNegativeInt
    mapped_cell_count: NonNegativeInt
    unresolved_sdc_selectors: NonNegativeInt
    endpoint_coverage_complete: bool
    binding_status: Literal["EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"]
    new_unapproved_cdc_crossings: NonNegativeInt
    changed_approved_cdc_structures: NonNegativeInt
    strict_proof_contract: CorrectnessContract
    strict_proof_outcome: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    required_views_complete: bool
    delivered_snapshot_hash: HashRef
    proof_snapshot_hash: HashRef
    every_claim_resolves: bool
    replay_verified: bool
    optimization_outcome: OptimizationOutcome
    evidence_hash: HashRef

    @model_validator(mode="after")
    def identity_is_canonical(self) -> AcceptanceEvidence:
        if self.evidence_hash != canonical_sha256(
            self, exclude=frozenset({"evidence_hash"})
        ):
            raise ValueError("acceptance evidence hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> AcceptanceEvidence:
        return cls(**values, evidence_hash=canonical_sha256(values))


class AcceptanceResult(StrictContract):
    status: Literal["PASS", "FAIL"]
    optimization_outcome: OptimizationOutcome
    failed_gates: tuple[str, ...]
    disclosures: tuple[str, ...]
    evidence_hash: HashRef
    result_hash: HashRef

    @model_validator(mode="after")
    def identity_is_canonical(self) -> AcceptanceResult:
        if self.status == "PASS" and self.failed_gates:
            raise ValueError("passing acceptance cannot contain failed gates")
        if self.status == "FAIL" and not self.failed_gates:
            raise ValueError("failed acceptance requires failed gates")
        if self.result_hash != canonical_sha256(self, exclude=frozenset({"result_hash"})):
            raise ValueError("acceptance result hash is not canonical")
        return self


def evaluate_acceptance(evidence: AcceptanceEvidence) -> AcceptanceResult:
    """Run the complete benchmark, safety, proof, report, and replay hard gate."""

    checks = {
        "MASTER_CLOCK_COUNT": evidence.master_clock_count == 5,
        "GENERATED_CLOCK_COUNT": evidence.generated_clock_count == 105,
        "MAPPED_CELL_TARGET": 45_000 <= evidence.mapped_cell_count <= 55_000,
        "SDC_SELECTORS": evidence.unresolved_sdc_selectors == 0,
        "ENDPOINT_COVERAGE": evidence.endpoint_coverage_complete,
        "BINDING_EQUIVALENCE": evidence.binding_status
        in {"EQUIVALENT", "APPROVED_SEMANTIC_REMAP"},
        "CDC_INVARIANTS": evidence.new_unapproved_cdc_crossings == 0
        and evidence.changed_approved_cdc_structures == 0,
        "STRICT_PROOF": evidence.strict_proof_contract == "STRICT_SEQ_EQUIV"
        and evidence.strict_proof_outcome == "PASS",
        "REQUIRED_VIEWS": evidence.required_views_complete,
        "PROOF_SNAPSHOT_IDENTITY": evidence.delivered_snapshot_hash
        == evidence.proof_snapshot_hash,
        "CLAIM_CLOSURE": evidence.every_claim_resolves,
        "OFFLINE_REPLAY": evidence.replay_verified,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    disclosures = (
        ("No PPA improvement is claimed; the measured candidate is a valid negative result.",)
        if evidence.optimization_outcome == "VALID_NEGATIVE_RESULT"
        else ()
    )
    payload = {
        "status": "FAIL" if failed else "PASS",
        "optimization_outcome": evidence.optimization_outcome,
        "failed_gates": failed,
        "disclosures": disclosures,
        "evidence_hash": evidence.evidence_hash,
    }
    return AcceptanceResult(**payload, result_hash=canonical_sha256(payload))


_FAILURE_SCENARIOS: dict[str, FailureFamily] = {
    "formal_counterexample": "FORMAL_SEMANTIC_FAILURE",
    "hold_regression": "HOLD_REGRESSION",
    "changed_sdc_binding": "CONSTRAINT_BINDING_DELTA",
    "new_cdc_crossing": "CDC_INVARIANT_DELTA",
    "formal_compilation_delta": "FORMAL_MODEL_MISMATCH",
    "area_policy_failure": "AREA_POLICY_VIOLATION",
    "path_migration": "CRITICAL_PATH_MIGRATION",
    "physical_noncorrelation": "PHYSICAL_CORRELATION_MISS",
    "repeated_non_progress": "REPEATED_NON_PROGRESS",
    "transient_adapter_failure": "INFRASTRUCTURE_TRANSIENT",
}


def classify_failure_injection(scenario: str) -> tuple[FailureFamily, RecoveryAction]:
    """Return the production recovery family/action for a required M9 injection."""

    try:
        family = _FAILURE_SCENARIOS[scenario]
    except KeyError as error:
        raise ValueError(f"unknown M9 failure injection: {scenario}") from error
    return family, DEFAULT_ACTION[family]


__all__ = [
    "AcceptanceEvidence",
    "AcceptanceResult",
    "classify_failure_injection",
    "evaluate_acceptance",
]
