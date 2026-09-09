from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.optimization import (
    CandidateRecord,
    OptimizationOpportunity,
    OptimizationProposal,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def self_hash(payload: dict[str, object], field: str) -> str:
    encoded = json.dumps(
        {key: value for key, value in payload.items() if key != field},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def artifact_payload(artifact_id: str, uri: str, digit: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": "application/json",
        "size_bytes": 100,
        "created_at": "2026-08-21T09:00:00Z",
        "producer_stage_result_id": "stage_transform_001",
        "classification": "RESTRICTED_RTL",
    }


def evidence_payload(evidence_id: str, kind: str, digit: str = "a") -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "artifact_id": "artifact_evidence_graph",
        "json_pointer": f"/nodes/{evidence_id}",
        "snapshot_hash": hash_ref(digit),
    }


def opportunity_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "opportunity_id": "path_cluster_0042",
        "parent_candidate_id": "baseline",
        "target_domain": "domain_dma",
        "target_analysis_view_id": "func_setup_slow",
        "affected_analysis_view_ids": ["func_setup_slow"],
        "root_causes": [{"category": "DEEP_PRIORITY_CHAIN", "confidence": 0.87}],
        "severity": {
            "worst_view_id": "func_setup_slow",
            "worst_slack_ns": -0.41,
            "affected_endpoints": 83,
            "tns_share_percent": 21.4,
        },
        "editability": "RTL_EDITABLE",
        "source_spans": ["source_expr_105"],
        "protected_neighbors": ["cdc_boundary_012"],
        "eligible_transform_families": ["LOGIC_RESTRUCTURE"],
        "proof_contracts": ["STRICT_SEQ_EQUIV"],
        "evidence_refs": [
            evidence_payload("path_0042", "PATH"),
            evidence_payload("source_expr_105", "SOURCE_SPAN"),
        ],
    }


def proposal_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "proposal_id": "opt_017",
        "parent_candidate_id": "baseline",
        "opportunity_id": "path_cluster_0042",
        "diagnosis_refs": ["diag_0042"],
        "target": {
            "hierarchy": "u_dma/addr_decode",
            "source_span_id": "source_expr_105",
            "cone_fingerprint": "cone:v3:7ed1",
        },
        "transformation": {
            "operation": "RESTRUCTURE_PRIORITY_MUX",
            "family": "LOGIC_RESTRUCTURE",
            "parameters": {
                "strategy": "BALANCED_PREDECODE",
                "preserve_signed_casts": True,
            },
        },
        "preconditions": [
            "SINGLE_CLOCK_DOMAIN",
            "NO_CDC_NODE_IN_CONE",
            "NO_PROTECTED_NODE_IN_EDIT_SET",
        ],
        "correctness": {
            "contract": "STRICT_SEQ_EQUIV",
            "proof_scope": "u_dma",
            "reset_model": "SYNCHRONOUS_RELEASE",
        },
        "prediction": {
            "timing_direction": "IMPROVE",
            "area_direction": "SMALL_INCREASE",
            "confidence": 0.73,
        },
        "evidence_refs": [
            evidence_payload("path_0042", "PATH"),
            evidence_payload("source_expr_105", "SOURCE_SPAN"),
        ],
        "abort_conditions": [
            "MAPPED_MUX_DEPTH_NOT_REDUCED",
            "AREA_GROWTH_ABOVE_POLICY",
        ],
    }


def metric_payload(view_id: str) -> dict[str, object]:
    unavailable = {
        "hold_wns_ns",
        "hold_tns_ns",
        "physical_area_um2",
        "buffer_count",
        "power_total_uw",
        "wirelength_um",
        "congestion_overflow",
    }
    return {
        "analysis_view_id": view_id,
        "setup_wns_ns": 0.04,
        "setup_tns_ns": 0.0,
        "hold_wns_ns": None,
        "hold_tns_ns": None,
        "failing_endpoints": 0,
        "critical_path_delay_ns": 1.96,
        "estimated_fmax_mhz": 510.2,
        "mapped_area_um2": 220000.0,
        "physical_area_um2": None,
        "cell_count": 50500,
        "register_count": 18342,
        "buffer_count": None,
        "power_total_uw": None,
        "wirelength_um": None,
        "congestion_overflow": None,
        "runtime_ms": 2000,
        "missing_metric_reasons": {key: "not produced by this stage" for key in unavailable},
    }


def candidate_payload() -> dict[str, object]:
    gate_payload: dict[str, object] = {
        "candidate_id": "cand_017",
        "source_hash": hash_ref("1"),
        "required_analysis_view_ids": ["func_setup_slow"],
        "passed_stage_result_ids": ["stage_cand_017_synth", "stage_cand_017_formal"],
        "proof_result_id": "proof_cand_017",
        "proof_outcome": "PASS",
        "proof_contract": "STRICT_SEQ_EQUIV",
        "binding_manifest_id": "binding_cand_017",
        "binding_status": "EQUIVALENT",
        "clock_inventory_id": "clock_inventory_cand_017",
        "clock_inventory_status": "COMPLETE",
        "cdc_inventory_id": "cdc_inventory_cand_017",
        "cdc_inventory_status": "UNCHANGED",
        "summary_hash": hash_ref("0"),
    }
    gate_payload["summary_hash"] = self_hash(gate_payload, "summary_hash")
    return {
        "schema_version": 1,
        "candidate_id": "cand_017",
        "run_id": "run_001",
        "parent_candidate_id": "baseline",
        "lineage_depth": 1,
        "proposal_id": "opt_017",
        "opportunity_id": "path_cluster_0042",
        "rtl_snapshot_artifact": artifact_payload(
            "artifact_cand_017_rtl",
            "artifact://runs/run_001/candidates/cand_017/rtl.json",
            "1",
        ),
        "patch_artifact": artifact_payload(
            "artifact_cand_017_patch",
            "artifact://runs/run_001/candidates/cand_017/patch.json",
            "2",
        ),
        "source_hash": hash_ref("1"),
        "changed_spans": ["source_expr_105"],
        "transform_fingerprint": "transform:v1:913f",
        "required_correctness_contract": "STRICT_SEQ_EQUIV",
        "stage_result_ids": ["stage_cand_017_synth", "stage_cand_017_formal"],
        "per_view_metrics": {"func_setup_slow": metric_payload("func_setup_slow")},
        "proof_result_id": "proof_cand_017",
        "binding_manifest_id": "binding_cand_017",
        "clock_inventory_id": "clock_inventory_cand_017",
        "cdc_inventory_id": "cdc_inventory_cand_017",
        "hard_gate_summary": gate_payload,
        "classification": "FEASIBLE_PARETO",
        "selection_class": "PRIMARY_STRICT",
        "terminal_disposition": "VALIDATED",
        "created_at": "2026-08-21T09:00:00Z",
        "recovery_parent_failure_id": None,
    }


def test_feasible_candidate_accepts_reviewed_semantic_constraint_remap() -> None:
    payload = candidate_payload()
    gate = payload["hard_gate_summary"]
    assert isinstance(gate, dict)
    gate["binding_status"] = "APPROVED_SEMANTIC_REMAP"
    gate["summary_hash"] = self_hash(gate, "summary_hash")

    candidate = CandidateRecord.model_validate(payload)

    assert candidate.hard_gate_summary is not None
    assert candidate.hard_gate_summary.binding_status == "APPROVED_SEMANTIC_REMAP"


def test_editable_opportunity_requires_actionable_same_snapshot_evidence() -> None:
    opportunity = OptimizationOpportunity.model_validate(opportunity_payload())
    assert opportunity.editability == "RTL_EDITABLE"

    empty_spans = opportunity_payload()
    empty_spans["source_spans"] = []
    with pytest.raises(ValidationError, match="editable opportunity"):
        OptimizationOpportunity.model_validate(empty_spans)

    mixed_snapshot = opportunity_payload()
    refs = mixed_snapshot["evidence_refs"]
    assert isinstance(refs, list)
    refs[1]["snapshot_hash"] = hash_ref("b")
    with pytest.raises(ValidationError, match="snapshot"):
        OptimizationOpportunity.model_validate(mixed_snapshot)


def test_proposal_is_advisory_safe_and_evidence_bound() -> None:
    proposal = OptimizationProposal.model_validate(proposal_payload())
    assert proposal.prediction.timing_direction == "IMPROVE"

    unsafe = proposal_payload()
    unsafe["preconditions"] = ["SINGLE_CLOCK_DOMAIN"]
    with pytest.raises(ValidationError, match="NO_PROTECTED_NODE_IN_EDIT_SET"):
        OptimizationProposal.model_validate(unsafe)

    authoritative_metric = proposal_payload()
    prediction = authoritative_metric["prediction"]
    assert isinstance(prediction, dict)
    prediction["setup_wns_ns"] = 0.12
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OptimizationProposal.model_validate(authoritative_metric)


def test_candidate_feasibility_requires_complete_hard_gate_references() -> None:
    candidate = CandidateRecord.model_validate(candidate_payload())
    assert candidate.classification == "FEASIBLE_PARETO"

    missing_proof = candidate_payload()
    missing_proof["proof_result_id"] = None
    with pytest.raises(ValidationError, match="hard-gate"):
        CandidateRecord.model_validate(missing_proof)

    wrong_metric_view = candidate_payload()
    metrics = wrong_metric_view["per_view_metrics"]
    assert isinstance(metrics, dict)
    metrics["func_setup_slow"] = metric_payload("func_hold_fast")
    with pytest.raises(ValidationError, match="metric view"):
        CandidateRecord.model_validate(wrong_metric_view)

    failed_proof = candidate_payload()
    summary = failed_proof["hard_gate_summary"]
    assert isinstance(summary, dict)
    summary["proof_outcome"] = "FAIL"
    summary["summary_hash"] = self_hash(summary, "summary_hash")
    with pytest.raises(ValidationError, match="hard-gate outcomes"):
        CandidateRecord.model_validate(failed_proof)

    incomplete_views = candidate_payload()
    summary = incomplete_views["hard_gate_summary"]
    assert isinstance(summary, dict)
    summary["required_analysis_view_ids"] = ["func_setup_slow", "func_hold_fast"]
    summary["summary_hash"] = self_hash(summary, "summary_hash")
    with pytest.raises(ValidationError, match="required-view metrics"):
        CandidateRecord.model_validate(incomplete_views)


def test_candidate_lineage_and_selection_class_fail_closed() -> None:
    self_parent = candidate_payload()
    self_parent["parent_candidate_id"] = "cand_017"
    with pytest.raises(ValidationError, match="parent"):
        CandidateRecord.model_validate(self_parent)

    wrong_contract = deepcopy(candidate_payload())
    wrong_contract["required_correctness_contract"] = "RETIMING_EQUIV"
    with pytest.raises(ValidationError, match="PRIMARY_STRICT"):
        CandidateRecord.model_validate(wrong_contract)


def test_hard_gate_summary_is_self_hashed_and_source_bound() -> None:
    candidate = candidate_payload()
    summary_payload = candidate["hard_gate_summary"]
    assert isinstance(summary_payload, dict)
    record = CandidateRecord.model_validate(candidate)
    assert record.hard_gate_summary is not None
    assert record.hard_gate_summary.proof_outcome == "PASS"

    tampered = candidate_payload()
    tampered_summary = tampered["hard_gate_summary"]
    assert isinstance(tampered_summary, dict)
    tampered_summary["source_hash"] = hash_ref("9")
    tampered_summary["summary_hash"] = self_hash(tampered_summary, "summary_hash")
    with pytest.raises(ValidationError, match="source hash"):
        CandidateRecord.model_validate(tampered)

    rejected = candidate_payload()
    rejected["classification"] = "REJECTED_CORRECTNESS"
    rejected["hard_gate_summary"] = None
    rejected["source_hash"] = hash_ref("9")
    with pytest.raises(ValidationError, match="source hash"):
        CandidateRecord.model_validate(rejected)


def test_opportunity_rejects_incoherent_severity_and_protected_targets() -> None:
    wrong_view = opportunity_payload()
    severity = wrong_view["severity"]
    assert isinstance(severity, dict)
    severity["worst_view_id"] = "func_hold_fast"
    with pytest.raises(ValidationError, match="severity worst view"):
        OptimizationOpportunity.model_validate(wrong_view)

    protected_target = opportunity_payload()
    protected_target["protected_neighbors"] = ["source_expr_105"]
    with pytest.raises(ValidationError, match="protected"):
        OptimizationOpportunity.model_validate(protected_target)
