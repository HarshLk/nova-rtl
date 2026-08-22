from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.recovery import (
    CandidateFailureFingerprint,
    FailureEvent,
    RecoveryAdvice,
    RecoveryDecision,
    RecoveryRequest,
    RecoveryRoutePlan,
    RepairDirective,
    validate_recovery_authority_chain,
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


def evidence_payload(evidence_id: str, kind: str = "PATH") -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "artifact_id": "artifact_evidence_graph",
        "json_pointer": f"/nodes/{evidence_id}",
        "snapshot_hash": hash_ref("a"),
    }


def failure_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "failure_event_id": "fail_017_sta_01",
        "run_id": "run_001",
        "subject_type": "CANDIDATE",
        "subject_id": "cand_017",
        "candidate_id": "cand_017",
        "proposal_id": "opt_017",
        "parent_candidate_id": "baseline",
        "opportunity_id": "path_cluster_0042",
        "failed_stage": "OPENSTA_FULL",
        "analysis_view_id": "func_setup_slow",
        "failure_family": "CRITICAL_PATH_MIGRATION",
        "failure_scope": "CANDIDATE",
        "repairability": "OPPORTUNITY_REANALYSIS",
        "severity": "MEDIUM",
        "retryable": True,
        "constraint_hash_verified": True,
        "analysis_view_hash_verified": True,
        "constraint_binding_status": "EQUIVALENT",
        "protected_structure_status": "UNCHANGED",
        "metric_delta": {
            "target_path_slack_ns": 0.22,
            "global_wns_ns": -0.04,
            "area_percent": 0.8,
        },
        "primary_evidence_refs": [
            evidence_payload("path_delta_017"),
            evidence_payload("timing_summary_017", "METRIC"),
        ],
        "raw_stage_result_ref": "stage_cand_017_opensta",
        "classifier_version": "failure-classifier-v1",
    }


def directive_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repair_directive_id": "repair_017_01",
        "failure_event_id": "fail_017_sta_01",
        "allowed_scope": "NEW_TARGET_OR_TRANSFORM",
        "observed": [
            "original priority path improved",
            "sibling decode is now globally critical",
        ],
        "preserve": [
            "rtl_snapshot_parent_hash",
            "constraint_contract_hash",
            "effective_constraint_binding_hash",
        ],
        "prohibit": [
            "repeat_identical_priority_rewrite",
            "modify_generated_clock_or_sdc",
        ],
        "recommended_actions": ["re-cluster paths around shared decode driver"],
        "recommended_roles": ["timing_forensics", "ppa_critic"],
        "evidence_refs": [evidence_payload("path_delta_017")],
        "compiler_rule_refs": ["OPENSTA_PATH_MIGRATION_V1"],
    }


def route_plan_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "recovery_route_plan_id": "route_plan_017_01",
        "failure_event_id": "fail_017_sta_01",
        "repair_directive_id": "repair_017_01",
        "allowed_actions": ["OPPORTUNITY_REANALYSIS", "REJECT_CANDIDATE"],
        "eligible_roles": ["timing_forensics", "ppa_critic"],
        "excluded_transform_families": ["LOGIC_RESTRUCTURE"],
        "next_parent_candidate_ids": ["baseline"],
        "remaining_family_budget": 1,
        "remaining_lineage_budget": 3,
        "remaining_token_budget": 4000,
        "remaining_latency_budget_ms": 20000,
        "policy_hash": hash_ref("b"),
        "route_plan_hash": hash_ref("0"),
    }
    payload["route_plan_hash"] = self_hash(payload, "route_plan_hash")
    return payload


def recovery_request_payload() -> dict[str, object]:
    failure = FailureEvent.model_validate(failure_payload())
    directive = RepairDirective.model_validate(directive_payload())
    payload: dict[str, object] = {
        "schema_version": 1,
        "recovery_request_id": "recovery_request_017_01",
        "failure_event_id": "fail_017_sta_01",
        "failure_event_hash": canonical_sha256(failure),
        "failure_family": "CRITICAL_PATH_MIGRATION",
        "repair_directive_id": "repair_017_01",
        "repair_directive_hash": canonical_sha256(directive),
        "recovery_route_plan_id": "route_plan_017_01",
        "recovery_route_plan_hash": route_plan_payload()["route_plan_hash"],
        "route_plan": route_plan_payload(),
        "candidate_lineage_ids": ["baseline", "cand_017"],
        "candidate_failure_fingerprint_ids": ["failure_fingerprint_017"],
        "allowed_actions": ["OPPORTUNITY_REANALYSIS"],
        "excluded_transform_families": ["LOGIC_RESTRUCTURE"],
        "eligible_roles": ["timing_forensics", "ppa_critic"],
        "authorized_evidence_ids": ["path_delta_017"],
        "remaining_family_budget": 1,
        "remaining_lineage_budget": 3,
        "remaining_token_budget": 3000,
        "remaining_latency_budget_ms": 15000,
        "policy_hash": hash_ref("b"),
        "deadline": "2026-08-21T10:10:00Z",
        "request_hash": hash_ref("0"),
    }
    payload["request_hash"] = self_hash(payload, "request_hash")
    return payload


def advice_payload() -> dict[str, object]:
    request = recovery_request_payload()
    payload: dict[str, object] = {
        "schema_version": 1,
        "recovery_advice_id": "recovery_advice_017_01",
        "failure_event_id": "fail_017_sta_01",
        "repair_directive_id": "repair_017_01",
        "recovery_request_id": "recovery_request_017_01",
        "recovery_request_hash": request["request_hash"],
        "recovery_route_plan_id": "route_plan_017_01",
        "recovery_route_plan_hash": route_plan_payload()["route_plan_hash"],
        "advisor_mode": "SINGLE_AGENT",
        "selected_role": "timing_forensics",
        "proposed_action": "OPPORTUNITY_REANALYSIS",
        "proposed_parent_candidate_id": "baseline",
        "proposed_excluded_transform_families": ["LOGIC_RESTRUCTURE"],
        "rationale_codes": ["PATH_MIGRATED", "LOCAL_GAIN_GLOBAL_NO_GAIN"],
        "evidence_refs": [evidence_payload("path_delta_017")],
        "uncertainty": 0.18,
        "input_tokens": 1800,
        "output_tokens": 300,
        "latency_ms": 900,
        "output_hash": hash_ref("0"),
    }
    payload["output_hash"] = self_hash(payload, "output_hash")
    return payload


def decision_payload() -> dict[str, object]:
    request = recovery_request_payload()
    payload: dict[str, object] = {
        "schema_version": 1,
        "recovery_decision_id": "recovery_017_01",
        "failure_event_id": "fail_017_sta_01",
        "recovery_request_id": "recovery_request_017_01",
        "recovery_request_hash": request["request_hash"],
        "recovery_route_plan_id": "route_plan_017_01",
        "recovery_route_plan_hash": route_plan_payload()["route_plan_hash"],
        "action": "OPPORTUNITY_REANALYSIS",
        "next_parent_candidate_id": "baseline",
        "invoke_reasoning": True,
        "selected_roles": ["timing_forensics", "ppa_critic"],
        "excluded_transform_families": ["LOGIC_RESTRUCTURE"],
        "remaining_family_budget": 1,
        "remaining_lineage_budget": 3,
        "reason_codes": ["PATH_MIGRATED", "TARGET_TRANSFORM_SUCCEEDED_LOCALLY"],
        "policy_version": "recovery-policy-v1",
        "recovery_advice_id": "recovery_advice_017_01",
        "decision_hash": hash_ref("0"),
    }
    payload["decision_hash"] = self_hash(payload, "decision_hash")
    return payload


def test_failure_event_requires_matching_subject_view_and_retry_semantics() -> None:
    event = FailureEvent.model_validate(failure_payload())
    assert event.failure_family == "CRITICAL_PATH_MIGRATION"

    missing_candidate = failure_payload()
    missing_candidate["candidate_id"] = None
    with pytest.raises(ValidationError, match="candidate_id"):
        FailureEvent.model_validate(missing_candidate)

    missing_view = failure_payload()
    missing_view["analysis_view_id"] = None
    with pytest.raises(ValidationError, match="analysis_view_id"):
        FailureEvent.model_validate(missing_view)

    invalid_retry = failure_payload()
    invalid_retry["repairability"] = "NO_RETRY"
    with pytest.raises(ValidationError, match="NO_RETRY"):
        FailureEvent.model_validate(invalid_retry)


def test_repair_directive_is_machine_checkable_and_evidence_bound() -> None:
    directive = RepairDirective.model_validate(directive_payload())
    assert directive.compiler_rule_refs == ("OPENSTA_PATH_MIGRATION_V1",)

    overlap = directive_payload()
    overlap["prohibit"] = ["constraint_contract_hash"]
    with pytest.raises(ValidationError, match="preserve and prohibit"):
        RepairDirective.model_validate(overlap)


def test_candidate_failure_fingerprint_excludes_raw_proposal_text() -> None:
    fingerprint = CandidateFailureFingerprint.model_validate(
        {
            "schema_version": 1,
            "candidate_failure_fingerprint_id": "failure_fingerprint_017",
            "candidate_id": "cand_017",
            "target_cone_fingerprint": "cone:v3:7ed1",
            "operation_family": "LOGIC_RESTRUCTURE",
            "operation": "RESTRUCTURE_PRIORITY_MUX",
            "parameter_fingerprint": "params:v1:1c2a",
            "ast_diff_fingerprint": "astdiff:v2:913f",
            "mapped_delta_fingerprint": "mapdelta:v2:630b",
            "ancestor_lineage": ["baseline"],
            "failure_family": "CRITICAL_PATH_MIGRATION",
            "formal_counterexample_fingerprint": None,
            "metric_response_class": "LOCAL_GAIN_GLOBAL_NO_GAIN",
        }
    )
    assert fingerprint.ancestor_lineage == ("baseline",)

    raw_text = fingerprint.model_dump(mode="json")
    raw_text["proposal_text"] = "try the same rewrite again"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CandidateFailureFingerprint.model_validate(raw_text)


def test_route_plan_and_request_are_hashed_bounded_authority_envelopes() -> None:
    plan = RecoveryRoutePlan.model_validate(route_plan_payload())
    assert plan.remaining_family_budget == 1

    request = RecoveryRequest.model_validate(recovery_request_payload())
    assert request.allowed_actions == ("OPPORTUNITY_REANALYSIS",)

    tampered = route_plan_payload()
    tampered["remaining_family_budget"] = 2
    with pytest.raises(ValidationError, match="route_plan_hash"):
        RecoveryRoutePlan.model_validate(tampered)

    expanded = recovery_request_payload()
    expanded["allowed_actions"] = ["OPPORTUNITY_REANALYSIS", "SWITCH_TRANSFORM_FAMILY"]
    expanded["request_hash"] = self_hash(expanded, "request_hash")
    with pytest.raises(ValidationError, match="allowed actions"):
        RecoveryRequest.model_validate(expanded)

    increased = recovery_request_payload()
    increased["remaining_family_budget"] = 2
    increased["request_hash"] = self_hash(increased, "request_hash")
    with pytest.raises(ValidationError, match="budget"):
        RecoveryRequest.model_validate(increased)


def test_recovery_advice_remains_advisory_and_self_hashed() -> None:
    advice = RecoveryAdvice.model_validate(advice_payload())
    assert advice.proposed_action == "OPPORTUNITY_REANALYSIS"

    authoritative = advice_payload()
    authoritative["remaining_lineage_budget"] = 99
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RecoveryAdvice.model_validate(authoritative)


def test_recovery_decision_terminal_actions_have_no_next_parent() -> None:
    decision = RecoveryDecision.model_validate(decision_payload())
    assert decision.recovery_advice_id == "recovery_advice_017_01"
    validate_recovery_authority_chain(
        failure_event=FailureEvent.model_validate(failure_payload()),
        repair_directive=RepairDirective.model_validate(directive_payload()),
        request=RecoveryRequest.model_validate(recovery_request_payload()),
        advice=RecoveryAdvice.model_validate(advice_payload()),
        decision=decision,
    )

    terminal = decision_payload()
    terminal["action"] = "REJECT_CANDIDATE"
    terminal["decision_hash"] = self_hash(terminal, "decision_hash")
    with pytest.raises(ValidationError, match="next parent"):
        RecoveryDecision.model_validate(terminal)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("proposed_action", "SWITCH_TRANSFORM_FAMILY", "allowed action"),
        ("selected_role", "fanout_specialist", "eligible role"),
        ("proposed_parent_candidate_id", "cand_unrelated", "parent"),
        ("proposed_excluded_transform_families", [], "excluded transform"),
        ("evidence_refs", [evidence_payload("unauthorized_path")], "evidence"),
        ("input_tokens", 3000, "budget"),
    ],
)
def test_recovery_advice_cannot_escape_route_authority(
    field: str, value: object, message: str
) -> None:
    payload = advice_payload()
    payload[field] = value
    payload["output_hash"] = self_hash(payload, "output_hash")
    advice = RecoveryAdvice.model_validate(payload)

    with pytest.raises(ValueError, match=message):
        validate_recovery_authority_chain(
            failure_event=FailureEvent.model_validate(failure_payload()),
            repair_directive=RepairDirective.model_validate(directive_payload()),
            request=RecoveryRequest.model_validate(recovery_request_payload()),
            advice=advice,
        )


def test_recovery_decision_cannot_increase_budgets_or_bypass_advice() -> None:
    request = RecoveryRequest.model_validate(recovery_request_payload())
    advice = RecoveryAdvice.model_validate(advice_payload())
    payload = decision_payload()
    payload["remaining_lineage_budget"] = request.remaining_lineage_budget + 1
    payload["decision_hash"] = self_hash(payload, "decision_hash")

    with pytest.raises(ValueError, match="budget"):
        validate_recovery_authority_chain(
            failure_event=FailureEvent.model_validate(failure_payload()),
            repair_directive=RepairDirective.model_validate(directive_payload()),
            request=request,
            advice=advice,
            decision=RecoveryDecision.model_validate(payload),
        )


@pytest.mark.parametrize(
    ("failure_family", "unsafe_action"),
    [
        ("INFRASTRUCTURE_TRANSIENT", "LOCAL_PARAMETER_REVISION"),
        ("PROTECTED_STRUCTURE_VIOLATION", "SWITCH_TRANSFORM_FAMILY"),
        ("PROTECTED_STRUCTURE_VIOLATION", "REPAIR_SCHEMA"),
        ("PROTECTED_STRUCTURE_VIOLATION", "REPARTITION_FORMAL_PROOF"),
    ],
)
def test_non_rtl_failures_cannot_route_to_rtl_repair(
    failure_family: str, unsafe_action: str
) -> None:
    failure_data = failure_payload()
    failure_data["failure_family"] = failure_family
    failure = FailureEvent.model_validate(failure_data)

    request_data = recovery_request_payload()
    request_data["failure_event_hash"] = canonical_sha256(failure)
    request_data["failure_family"] = failure_family
    request_data["allowed_actions"] = [unsafe_action]
    route = request_data["route_plan"]
    assert isinstance(route, dict)
    route["allowed_actions"] = [unsafe_action]
    route["route_plan_hash"] = self_hash(route, "route_plan_hash")
    request_data["recovery_route_plan_hash"] = route["route_plan_hash"]
    request_data["request_hash"] = self_hash(request_data, "request_hash")
    with pytest.raises(ValidationError, match="cannot authorize"):
        RecoveryRequest.model_validate(request_data)


def test_planner_failure_has_provider_result_instead_of_fake_stage_result() -> None:
    payload = failure_payload()
    payload.update(
        {
            "subject_type": "PLANNER",
            "subject_id": "planner_request_001",
            "candidate_id": None,
            "proposal_id": None,
            "parent_candidate_id": None,
            "opportunity_id": None,
            "failed_stage": "PLANNER_PROVIDER",
            "analysis_view_id": None,
            "failure_family": "INVALID_PROPOSAL_SCHEMA",
            "failure_scope": "PLANNER",
            "repairability": "LOCAL_REVISION",
            "raw_stage_result_ref": None,
            "provider_result_ref": "provider_result_001",
        }
    )
    event = FailureEvent.model_validate(payload)
    assert event.provider_result_ref == "provider_result_001"

    payload["provider_result_ref"] = None
    with pytest.raises(ValidationError, match="provider_result_ref"):
        FailureEvent.model_validate(payload)
