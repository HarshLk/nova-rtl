from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.planning import (
    ContextRequest,
    CouncilRequest,
    CouncilResult,
    CouncilTrace,
    CritiqueDisposition,
    CritiqueReport,
    DiagnosisReport,
    PlannerRequest,
    PlannerResult,
    ProposalShortlist,
    ProviderResult,
    RoleContextPack,
    TransformRecommendation,
)


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def artifact_payload(
    artifact_id: str, uri: str, digit: str, classification: str = "INTERNAL"
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "uri": uri,
        "sha256": hash_ref(digit),
        "media_type": "application/json",
        "size_bytes": 100,
        "created_at": "2026-08-21T10:00:00Z",
        "producer_stage_result_id": "stage_planner_001",
        "classification": classification,
    }


def evidence_payload(evidence_id: str, kind: str = "PATH") -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "artifact_id": "artifact_evidence_graph",
        "json_pointer": f"/nodes/{evidence_id}",
        "snapshot_hash": hash_ref("a"),
    }


def planner_request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "planner_request_id": "planner_request_001",
        "run_id": "run_001",
        "parent_candidate_id": "baseline",
        "opportunity_id": "path_cluster_0042",
        "evidence_snapshot_hash": hash_ref("a"),
        "design_contract_hash": hash_ref("b"),
        "policy_hash": hash_ref("c"),
        "transform_registry_hash": hash_ref("d"),
        "authorized_evidence_ids": ["path_0042", "source_expr_105"],
        "planner_mode": "AGENT_COUNCIL",
        "proposal_limit": 3,
        "token_budget": 12000,
        "latency_budget_ms": 60000,
        "deadline": "2026-08-21T10:05:00Z",
        "deterministic_seed": 42,
        "required_output_schema_name": "optimization-proposal",
        "required_output_schema_version": 2,
    }


def context_request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "context_request_id": "context_request_timing_001",
        "planner_request_id": "planner_request_001",
        "role": "timing_forensics",
        "common_envelope_hash": hash_ref("1"),
        "private_evidence_ids": ["path_0042"],
        "retrieval_allowlist": ["path_0042", "source_expr_105"],
        "snapshot_hash": hash_ref("a"),
        "token_budget": 3000,
        "redaction_policy_hash": hash_ref("2"),
    }


def context_pack_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "role_context_pack_id": "context_pack_timing_001",
        "context_request_id": "context_request_timing_001",
        "role": "timing_forensics",
        "common_envelope_artifact": artifact_payload(
            "artifact_common_envelope",
            "artifact://runs/run_001/planner/common-envelope.json",
            "1",
        ),
        "common_envelope_hash": hash_ref("1"),
        "private_pack_artifact": artifact_payload(
            "artifact_private_timing",
            "artifact://runs/run_001/planner/private/timing.json",
            "3",
            "RESTRICTED_RTL",
        ),
        "private_pack_hash": hash_ref("3"),
        "retrieval_grants": ["path_0042", "source_expr_105"],
        "rendered_message_artifact": artifact_payload(
            "artifact_rendered_timing",
            "artifact://runs/run_001/planner/rendered/timing.json",
            "4",
            "RESTRICTED_RTL",
        ),
        "rendered_message_hash": hash_ref("4"),
        "estimated_tokens": 2200,
        "created_at": "2026-08-21T10:00:00Z",
    }


def provider_result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider_result_id": "provider_result_001",
        "planner_request_id": "planner_request_001",
        "context_request_id": "context_request_timing_001",
        "status": "PASS",
        "provider_id": "openai",
        "model_id": "gpt-5.4",
        "provider_configuration_hash": hash_ref("5"),
        "prompt_hash": hash_ref("6"),
        "requested_schema_name": "diagnosis-report",
        "requested_schema_version": 1,
        "structured_output_artifact": artifact_payload(
            "artifact_provider_output",
            "artifact://runs/run_001/planner/provider/output.json",
            "7",
        ),
        "structured_output_hash": hash_ref("7"),
        "input_tokens": 2000,
        "output_tokens": 500,
        "latency_ms": 1200,
        "error_code": None,
        "completed_at": "2026-08-21T10:00:02Z",
    }


def planner_result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "planner_result_id": "planner_result_001",
        "run_id": "run_001",
        "opportunity_id": "path_cluster_0042",
        "planner_mode": "AGENT_COUNCIL",
        "status": "PASS",
        "proposal_ids": ["opt_017"],
        "rejected_output_diagnostics": [],
        "context_pack_hashes": [hash_ref("3")],
        "council_result_id": "council_result_001",
        "provider_id": "openai",
        "model_id": "gpt-5.4",
        "prompt_hash": hash_ref("6"),
        "output_schema_hash": hash_ref("8"),
        "input_tokens": 4000,
        "output_tokens": 900,
        "latency_ms": 3000,
        "fallback_used": False,
        "upstream_provider_result_id": None,
    }


def council_request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "council_request_id": "council_request_001",
        "planner_request_id": "planner_request_001",
        "council_route_id": "council_route_default",
        "council_route_hash": hash_ref("9"),
        "blinded_proposer_roles": ["timing_forensics", "logic_specialist"],
        "critic_roles": ["formal_critic", "ppa_critic"],
        "chair_role": "optimization_chair",
        "context_policy_hash": hash_ref("2"),
        "fan_out_limit": 4,
        "fan_in_limit": 4,
        "aggregate_token_budget": 12000,
        "aggregate_latency_budget_ms": 60000,
        "deadline": "2026-08-21T10:05:00Z",
        "event_stream_id": "event_stream_council_001",
    }


def critique_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "critique_report_id": "critique_formal_001",
        "critic_role": "formal_critic",
        "critic_class": "FORMAL",
        "proposal_card_id": "proposal_card_017",
        "objections": [
            {
                "objection_id": "objection_reset_001",
                "severity": "MANDATORY",
                "category": "RESET_SEMANTICS",
                "message": "Preserve reset release behavior",
                "evidence_refs": [evidence_payload("source_expr_105", "SOURCE_SPAN")],
                "requested_disposition": "ACCEPT",
            }
        ],
    }


def disposition_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "critique_disposition_id": "disposition_reset_001",
        "objection_id": "objection_reset_001",
        "action": "ACCEPT",
        "reason_code": "RESET_PRESERVED_BY_PRECONDITION",
        "resulting_proposal_id": "opt_017",
        "chair_evidence_refs": [evidence_payload("source_expr_105", "SOURCE_SPAN")],
    }


def ppa_critique_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "critique_report_id": "critique_ppa_001",
        "critic_role": "ppa_critic",
        "critic_class": "PPA",
        "proposal_card_id": "proposal_card_017",
        "objections": [
            {
                "objection_id": "objection_area_001",
                "severity": "ADVISORY",
                "category": "AREA_GROWTH",
                "message": "Guard the mapped-area budget",
                "evidence_refs": [evidence_payload("path_0042")],
                "requested_disposition": "ACCEPT",
            }
        ],
    }


def ppa_disposition_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "critique_disposition_id": "disposition_area_001",
        "objection_id": "objection_area_001",
        "action": "ACCEPT",
        "reason_code": "AREA_ABORT_CONDITION_PRESENT",
        "resulting_proposal_id": "opt_017",
        "chair_evidence_refs": [evidence_payload("path_0042")],
    }


def council_result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "council_result_id": "council_result_001",
        "council_request_id": "council_request_001",
        "status": "PASS",
        "proposal_shortlist": {
            "schema_version": 1,
            "proposal_shortlist_id": "shortlist_001",
            "status": "PASS",
            "ordered_proposal_ids": ["opt_017"],
            "fallback_eligible": False,
            "unresolved_mandatory_finding_count": 0,
        },
        "council_trace_id": "council_trace_001",
        "run_id": "run_001",
        "opportunity_id": "path_cluster_0042",
        "snapshot_hash": hash_ref("a"),
        "policy_hash": hash_ref("c"),
        "route_reason_codes": ["HIGH_IMPACT_PATH"],
        "selected_role_ids": [
            "timing_forensics",
            "logic_specialist",
            "formal_critic",
            "ppa_critic",
            "optimization_chair",
        ],
        "common_safety_envelope_hash": hash_ref("1"),
        "private_role_records": [
            {
                "role_id": "timing_forensics",
                "role_kind": "PROPOSER",
                "private_pack_hash": hash_ref("3"),
                "retrieved_evidence_ids": ["path_0042"],
                "blinded_round": True,
                "structured_submission_artifact": artifact_payload(
                    "artifact_timing_submission",
                    "artifact://runs/run_001/council/timing.json",
                    "b",
                ),
                "input_tokens": 1800,
                "output_tokens": 300,
                "latency_ms": 900,
                "status": "PASS",
            },
            {
                "role_id": "logic_specialist",
                "role_kind": "PROPOSER",
                "private_pack_hash": hash_ref("4"),
                "retrieved_evidence_ids": ["source_expr_105"],
                "blinded_round": True,
                "structured_submission_artifact": artifact_payload(
                    "artifact_logic_submission",
                    "artifact://runs/run_001/council/logic.json",
                    "c",
                ),
                "input_tokens": 1700,
                "output_tokens": 350,
                "latency_ms": 850,
                "status": "PASS",
            },
            {
                "role_id": "formal_critic",
                "role_kind": "CRITIC",
                "private_pack_hash": hash_ref("5"),
                "retrieved_evidence_ids": ["source_expr_105"],
                "blinded_round": True,
                "structured_submission_artifact": artifact_payload(
                    "artifact_formal_critique",
                    "artifact://runs/run_001/council/formal.json",
                    "e",
                ),
                "input_tokens": 1000,
                "output_tokens": 200,
                "latency_ms": 500,
                "status": "PASS",
            },
            {
                "role_id": "ppa_critic",
                "role_kind": "CRITIC",
                "private_pack_hash": hash_ref("6"),
                "retrieved_evidence_ids": ["path_0042"],
                "blinded_round": True,
                "structured_submission_artifact": artifact_payload(
                    "artifact_ppa_critique",
                    "artifact://runs/run_001/council/ppa.json",
                    "f",
                ),
                "input_tokens": 900,
                "output_tokens": 180,
                "latency_ms": 450,
                "status": "PASS",
            },
            {
                "role_id": "optimization_chair",
                "role_kind": "CHAIR",
                "private_pack_hash": hash_ref("7"),
                "retrieved_evidence_ids": ["path_0042", "source_expr_105"],
                "blinded_round": False,
                "structured_submission_artifact": artifact_payload(
                    "artifact_chair_decision",
                    "artifact://runs/run_001/council/chair.json",
                    "0",
                ),
                "input_tokens": 800,
                "output_tokens": 150,
                "latency_ms": 400,
                "status": "PASS",
            },
        ],
        "proposal_cards": [
            {
                "proposal_card_id": "proposal_card_017",
                "proposal_id": "opt_017",
                "normalized_proposal_hash": hash_ref("d"),
            }
        ],
        "critique_reports": [critique_payload(), ppa_critique_payload()],
        "critique_dispositions": [
            disposition_payload(),
            ppa_disposition_payload(),
        ],
        "revision_records": [],
        "final_ordered_proposal_ids": ["opt_017"],
        "total_tokens": 7380,
        "total_latency_ms": 3100,
        "deadline_outcome": "MET",
        "trace_completeness_percent": 100.0,
    }


def test_planner_and_context_requests_cannot_expand_evidence_authority() -> None:
    request = PlannerRequest.model_validate(planner_request_payload())
    assert request.proposal_limit == 3

    context = ContextRequest.model_validate(context_request_payload())
    assert context.private_evidence_ids == ("path_0042",)

    expanded = context_request_payload()
    expanded["private_evidence_ids"] = ["unauthorized_path"]
    with pytest.raises(ValidationError, match="retrieval allowlist"):
        ContextRequest.model_validate(expanded)


def test_context_pack_and_provider_output_hashes_are_exact() -> None:
    pack = RoleContextPack.model_validate(context_pack_payload())
    assert pack.private_pack_hash == pack.private_pack_artifact.sha256
    pack.validate_against(
        ContextRequest.model_validate(context_request_payload()),
        PlannerRequest.model_validate(planner_request_payload()),
    )

    expanded_pack = context_pack_payload()
    expanded_pack["retrieval_grants"] = ["path_0042", "unauthorized_path"]
    with pytest.raises(ValueError, match="retrieval allowlist"):
        RoleContextPack.model_validate(expanded_pack).validate_against(
            ContextRequest.model_validate(context_request_payload()),
            PlannerRequest.model_validate(planner_request_payload()),
        )

    result = ProviderResult.model_validate(provider_result_payload())
    assert result.status == "PASS"

    failed_with_output = provider_result_payload()
    failed_with_output["status"] = "PROVIDER_ERROR"
    failed_with_output["error_code"] = "PROVIDER_UNAVAILABLE"
    with pytest.raises(ValidationError, match="non-pass"):
        ProviderResult.model_validate(failed_with_output)


def test_planner_result_pass_and_fallback_retain_authoritative_lineage() -> None:
    result = PlannerResult.model_validate(planner_result_payload())
    assert result.proposal_ids == ("opt_017",)

    empty_pass = planner_result_payload()
    empty_pass["proposal_ids"] = []
    with pytest.raises(ValidationError, match="PASS"):
        PlannerResult.model_validate(empty_pass)

    untracked_fallback = planner_result_payload()
    untracked_fallback["fallback_used"] = True
    with pytest.raises(ValidationError, match="upstream"):
        PlannerResult.model_validate(untracked_fallback)


def test_planning_subcontracts_are_typed_and_fail_closed() -> None:
    diagnosis = DiagnosisReport.model_validate(
        {
            "schema_version": 1,
            "diagnosis_report_id": "diagnosis_0042",
            "planner_request_id": "planner_request_001",
            "role": "timing_forensics",
            "opportunity_id": "path_cluster_0042",
            "snapshot_hash": hash_ref("a"),
            "ranked_root_causes": [
                {"category": "DEEP_PRIORITY_CHAIN", "confidence": 0.87}
            ],
            "editability": "RTL_EDITABLE",
            "safety_assessment": "SAFE_WITH_PRECONDITIONS",
            "claims": [
                {
                    "claim": "Priority depth dominates cell delay",
                    "evidence_refs": [evidence_payload("path_0042")],
                }
            ],
            "uncertainties": [],
            "disposition": "ACTIONABLE",
        }
    )
    assert diagnosis.disposition == "ACTIONABLE"

    recommendation = TransformRecommendation.model_validate(
        {
            "schema_version": 1,
            "transform_recommendation_id": "recommendation_0042",
            "diagnosis_report_id": "diagnosis_0042",
            "family": "LOGIC_RESTRUCTURE",
            "operation": "RESTRUCTURE_PRIORITY_MUX",
            "target_source_span_id": "source_expr_105",
            "parameters": {"strategy": "BALANCED_PREDECODE"},
            "preconditions": ["NO_PROTECTED_NODE_IN_EDIT_SET"],
            "contract": "STRICT_SEQ_EQUIV",
            "abort_conditions": ["MAPPED_MUX_DEPTH_NOT_REDUCED"],
            "evidence_refs": [evidence_payload("source_expr_105", "SOURCE_SPAN")],
            "expected_structural_direction": "REDUCE_MUX_DEPTH",
        }
    )
    assert recommendation.contract == "STRICT_SEQ_EQUIV"

    critique = CritiqueReport.model_validate(critique_payload())
    disposition = CritiqueDisposition.model_validate(disposition_payload())
    assert disposition.objection_id == critique.objections[0].objection_id

    shortlist = ProposalShortlist.model_validate(
        {
            "schema_version": 1,
            "proposal_shortlist_id": "shortlist_001",
            "status": "PASS",
            "ordered_proposal_ids": ["opt_017"],
            "fallback_eligible": False,
            "unresolved_mandatory_finding_count": 0,
        }
    )
    assert shortlist.ordered_proposal_ids == ("opt_017",)

    unsafe_partial = shortlist.model_dump(mode="json")
    unsafe_partial["status"] = "PARTIAL"
    unsafe_partial["unresolved_mandatory_finding_count"] = 1
    with pytest.raises(ValidationError, match="mandatory findings"):
        ProposalShortlist.model_validate(unsafe_partial)

    trace = CouncilTrace.model_validate(
        {
            "schema_version": 1,
            "council_trace_id": "council_trace_001",
            "selected_role_ids": ["timing_forensics", "formal_critic"],
            "events": [
                {
                    "sequence": 0,
                    "event_type": "ROLE_STARTED",
                    "role_id": "timing_forensics",
                    "timestamp": "2026-08-21T10:00:00Z",
                },
                {
                    "sequence": 1,
                    "event_type": "ROLE_COMPLETED",
                    "role_id": "timing_forensics",
                    "timestamp": "2026-08-21T10:00:01Z",
                },
            ],
            "prompt_hashes": [hash_ref("1")],
            "model_configuration_hashes": [hash_ref("2")],
            "context_pack_hashes": [hash_ref("3")],
            "retrieval_log_hashes": [hash_ref("4")],
            "input_tokens": 100,
            "output_tokens": 20,
            "latency_ms": 1000,
            "deadline_outcome": "MET",
            "cancelled": False,
            "trace_completeness_percent": 100.0,
        }
    )
    assert trace.events[-1].sequence == 1


def test_council_requires_independent_proposers_critics_and_dispositions() -> None:
    request = CouncilRequest.model_validate(council_request_payload())
    assert request.critic_roles == ("formal_critic", "ppa_critic")

    missing_critic = council_request_payload()
    missing_critic["critic_roles"] = ["formal_critic"]
    with pytest.raises(ValidationError, match="mandatory critics"):
        CouncilRequest.model_validate(missing_critic)

    result = CouncilResult.model_validate(council_result_payload())
    assert result.final_ordered_proposal_ids == ("opt_017",)

    undispositioned = deepcopy(council_result_payload())
    undispositioned["critique_dispositions"] = []
    with pytest.raises(ValidationError, match="disposition"):
        CouncilResult.model_validate(undispositioned)

    leaked_pack = deepcopy(council_result_payload())
    records = leaked_pack["private_role_records"]
    assert isinstance(records, list)
    records[1]["private_pack_hash"] = records[0]["private_pack_hash"]
    with pytest.raises(ValidationError, match="private pack"):
        CouncilResult.model_validate(leaked_pack)


def test_cancelled_council_preserves_audit_without_fabricating_proposals() -> None:
    cancelled = council_result_payload()
    cancelled["status"] = "CANCELLED"
    shortlist = cancelled["proposal_shortlist"]
    assert isinstance(shortlist, dict)
    shortlist["status"] = "PARTIAL"
    shortlist["ordered_proposal_ids"] = []
    shortlist["fallback_eligible"] = True
    cancelled["proposal_cards"] = []
    cancelled["critique_reports"] = []
    cancelled["critique_dispositions"] = []
    cancelled["final_ordered_proposal_ids"] = []
    cancelled["deadline_outcome"] = "CANCELLED"
    cancelled["trace_completeness_percent"] = 40.0

    result = CouncilResult.model_validate(cancelled)
    assert result.deadline_outcome == "CANCELLED"
    assert result.final_ordered_proposal_ids == ()


def test_council_non_success_and_mandatory_critic_failure_are_fail_closed() -> None:
    no_safe = council_result_payload()
    no_safe["status"] = "NO_SAFE_PROPOSAL"
    shortlist = no_safe["proposal_shortlist"]
    assert isinstance(shortlist, dict)
    shortlist["status"] = "NO_SAFE_PROPOSAL"
    shortlist["ordered_proposal_ids"] = []
    shortlist["fallback_eligible"] = True
    no_safe["proposal_cards"] = []
    no_safe["critique_reports"] = []
    no_safe["critique_dispositions"] = []
    no_safe["final_ordered_proposal_ids"] = []
    result = CouncilResult.model_validate(no_safe)
    assert result.status == "NO_SAFE_PROPOSAL"

    failed_critic = council_result_payload()
    records = failed_critic["private_role_records"]
    assert isinstance(records, list)
    records[2]["status"] = "ERROR"
    with pytest.raises(ValidationError, match="mandatory critics"):
        CouncilResult.model_validate(failed_critic)
