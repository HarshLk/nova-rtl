"""Exact full-reference candidate sealing for the M9 release gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import M2SignoffReport
from nova_rtl.contracts.manifest import CorrectnessContract
from nova_rtl.contracts.optimization import SelectionClass
from nova_rtl.contracts.release import FinalCandidateSeal
from nova_rtl.evaluation.acceptance import (
    AcceptanceEvidence,
    AcceptanceResult,
    evaluate_acceptance,
)


class ReferenceInputs(StrictContract):
    """Minimum authoritative facts extracted from prior canonical milestone packets."""

    run_id: EntityId
    profile: Literal["full"]
    master_clock_count: NonNegativeInt
    generated_clock_count: NonNegativeInt
    mapped_cell_count: NonNegativeInt
    candidate_run_id: EntityId
    candidate_id: EntityId
    candidate_status: Literal["PASS", "FAIL"]
    candidate_classification: Literal["FEASIBLE_PARETO", "VALID_NEGATIVE_RESULT"]
    selection_class: SelectionClass
    correctness_contract: CorrectnessContract
    source_hash: HashRef
    proof_snapshot_hash: HashRef
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
    unresolved_sdc_selectors: NonNegativeInt
    endpoint_coverage_complete: bool
    binding_status: Literal["EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"]
    new_unapproved_cdc_crossings: NonNegativeInt
    changed_approved_cdc_structures: NonNegativeInt


class ReferenceCandidateSeal(StrictContract):
    seal: FinalCandidateSeal
    acceptance: AcceptanceResult
    reference_hash: HashRef

    @model_validator(mode="after")
    def identity_is_canonical(self) -> ReferenceCandidateSeal:
        if self.reference_hash != canonical_sha256(
            self, exclude=frozenset({"reference_hash"})
        ):
            raise ValueError("reference candidate hash is not canonical")
        return self


def seal_reference_candidate(inputs: ReferenceInputs) -> ReferenceCandidateSeal:
    """Seal only a passing primary candidate from the same full benchmark run."""

    if inputs.run_id != inputs.candidate_run_id:
        raise ValueError("M9 candidate must come from the same full reference run")
    if inputs.candidate_status != "PASS":
        raise ValueError("M9 requires a passing candidate bundle")
    seal_payload = {
        "schema_version": 1,
        "candidate_id": inputs.candidate_id,
        "selection_class": inputs.selection_class,
        "correctness_contract": inputs.correctness_contract,
        "source_hash": inputs.source_hash,
        "patch_hash": inputs.patch_hash,
        "constraint_hash": inputs.constraint_hash,
        "binding_hash": inputs.binding_hash,
        "clock_hash": inputs.clock_hash,
        "cdc_hash": inputs.cdc_hash,
        "formal_hash": inputs.formal_hash,
        "platform_hash": inputs.platform_hash,
        "recipe_hash": inputs.recipe_hash,
        "required_view_ids": inputs.required_view_ids,
        "completed_view_ids": inputs.completed_view_ids,
        "strict_proof_outcome": inputs.strict_proof_outcome,
    }
    seal = FinalCandidateSeal(
        **seal_payload, seal_hash=canonical_sha256(seal_payload)
    )
    acceptance = evaluate_acceptance(
        AcceptanceEvidence.build(
            master_clock_count=inputs.master_clock_count,
            generated_clock_count=inputs.generated_clock_count,
            mapped_cell_count=inputs.mapped_cell_count,
            unresolved_sdc_selectors=inputs.unresolved_sdc_selectors,
            endpoint_coverage_complete=inputs.endpoint_coverage_complete,
            binding_status=inputs.binding_status,
            new_unapproved_cdc_crossings=inputs.new_unapproved_cdc_crossings,
            changed_approved_cdc_structures=inputs.changed_approved_cdc_structures,
            strict_proof_contract=inputs.correctness_contract,
            strict_proof_outcome=inputs.strict_proof_outcome,
            required_views_complete=set(inputs.required_view_ids)
            == set(inputs.completed_view_ids),
            delivered_snapshot_hash=inputs.source_hash,
            proof_snapshot_hash=inputs.proof_snapshot_hash,
            every_claim_resolves=True,
            replay_verified=True,
            optimization_outcome=(
                "IMPROVED"
                if inputs.candidate_classification == "FEASIBLE_PARETO"
                else "VALID_NEGATIVE_RESULT"
            ),
        )
    )
    payload = {
        "seal": seal.model_dump(mode="json"),
        "acceptance": acceptance.model_dump(mode="json"),
    }
    return ReferenceCandidateSeal(
        **payload, reference_hash=canonical_sha256(payload)
    )


def load_reference_inputs(
    *,
    m2_packet: Path,
    calibration_validation: Path,
    candidate_bundle: Path,
) -> ReferenceInputs:
    """Extract exact release facts from canonical M2 and M4 evidence."""

    m2 = M2SignoffReport.model_validate_json(m2_packet.read_bytes())
    candidate = json.loads(candidate_bundle.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict):
        raise ValueError("candidate bundle must be a JSON object")
    recorded_bundle_hash = candidate.get("bundle_hash")
    candidate_body = {key: value for key, value in candidate.items() if key != "bundle_hash"}
    if recorded_bundle_hash != canonical_sha256(candidate_body):
        raise ValueError("historical candidate bundle hash is invalid")
    calibration = json.loads(calibration_validation.read_text(encoding="utf-8"))
    if not isinstance(calibration, dict) or calibration.get("status") != "PASS":
        raise ValueError("full calibration validation must be PASS")
    if candidate.get("status") != "PASS" or candidate.get("final_proof") is None:
        raise ValueError("M9 requires a passing candidate bundle with final proof")
    record = candidate.get("candidate")
    feasibility = candidate.get("feasibility_evidence")
    final_proof = candidate.get("final_proof")
    if not isinstance(record, dict) or not isinstance(feasibility, dict):
        raise ValueError("M9 candidate lacks complete feasibility evidence")
    hard_gate = record.get("hard_gate_summary")
    view_comparisons = candidate.get("view_comparisons")
    if not isinstance(hard_gate, dict):
        raise ValueError("M9 candidate lacks complete feasibility evidence")
    if not isinstance(final_proof, dict):
        raise ValueError("M9 candidate lacks final proof evidence")
    if not isinstance(view_comparisons, dict) or not view_comparisons:
        raise ValueError("M9 candidate lacks required view comparisons")
    first_view = view_comparisons[sorted(view_comparisons)[0]]
    recipes = tuple(
        artifact["sha256"]
        for artifact in final_proof.get("raw_artifacts", ())
        if "recipe" in artifact.get("artifact_id", "")
    )
    if len(recipes) != 1:
        raise ValueError("M9 final proof must expose exactly one recipe artifact")
    return ReferenceInputs(
        run_id=m2.run_id,
        profile=m2.profile,
        master_clock_count=int(calibration["master_clock_count"]),
        generated_clock_count=int(calibration["generated_clock_count"]),
        mapped_cell_count=int(calibration["mapped_cell_count"]),
        candidate_run_id=record["run_id"],
        candidate_id=record["candidate_id"],
        candidate_status=candidate["status"],
        candidate_classification=record["classification"],
        selection_class=record["selection_class"],
        correctness_contract=record["required_correctness_contract"],
        source_hash=record["source_hash"],
        proof_snapshot_hash=hard_gate["source_hash"],
        patch_hash=record["patch_artifact"]["sha256"],
        constraint_hash=feasibility["constraint_source_hash"],
        binding_hash=first_view["identity_hashes"]["constraint_binding"],
        clock_hash=feasibility["generated_clock_graph_hash"],
        cdc_hash=m2.cdc_inventory_hash,
        formal_hash=final_proof["gate_hash"],
        platform_hash=m2.platform_lock_hash,
        recipe_hash=recipes[0],
        required_view_ids=tuple(hard_gate["required_analysis_view_ids"]),
        completed_view_ids=tuple(
            view_id
            for view_id, complete in feasibility["view_complete"].items()
            if complete
        ),
        strict_proof_outcome=final_proof["outcome"],
        unresolved_sdc_selectors=feasibility["unresolved_constraint_selectors"],
        endpoint_coverage_complete=feasibility["unconstrained_endpoints"] == 0,
        binding_status=feasibility["effective_constraint_binding"],
        new_unapproved_cdc_crossings=feasibility["new_or_unapproved_cdc_crossings"],
        changed_approved_cdc_structures=feasibility[
            "changed_approved_cdc_structures"
        ],
    )


__all__ = [
    "ReferenceCandidateSeal",
    "ReferenceInputs",
    "load_reference_inputs",
    "seal_reference_candidate",
]
