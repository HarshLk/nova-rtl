"""Normalize typed stage outcomes into one authoritative failure event."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from nova_rtl.contracts.base import EvidenceRef, canonical_sha256
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.recovery import FailureEvent, FailureFamily
from nova_rtl.recovery.policy import RecoveryPolicyRegistry

CLASSIFIER_VERSION = "m7-classifier-v1"

DIAGNOSTIC_FAMILIES: dict[str, FailureFamily] = {
    "ANALYSIS_VIEW_MISMATCH": "ANALYSIS_VIEW_OR_ACTIVITY_MISMATCH",
    "ACTIVITY_IDENTITY_MISMATCH": "ANALYSIS_VIEW_OR_ACTIVITY_MISMATCH",
    "ADAPTER_PARSE_ERROR": "ADAPTER_OR_PARSER_ERROR",
    "CONSTRAINT_BINDING_DELTA": "CONSTRAINT_BINDING_DELTA",
    "CDC_INVARIANT_DELTA": "CDC_INVARIANT_DELTA",
    "FORMAL_MODEL_MISMATCH": "FORMAL_MODEL_MISMATCH",
    "INVALID_PROPOSAL_SCHEMA": "INVALID_PROPOSAL_SCHEMA",
    "UNKNOWN_TRANSFORM": "UNKNOWN_OR_INAPPLICABLE_TRANSFORM",
    "INAPPLICABLE_TRANSFORM": "UNKNOWN_OR_INAPPLICABLE_TRANSFORM",
    "PROTECTED_STRUCTURE_VIOLATION": "PROTECTED_STRUCTURE_VIOLATION",
    "RTL_PARSE_ERROR": "RTL_PARSE_OR_ELAB_FAILURE",
    "RTL_ELABORATION_ERROR": "RTL_PARSE_OR_ELAB_FAILURE",
    "FAST_SYNTH_STRUCTURAL_FAILURE": "FAST_SYNTH_STRUCTURAL_FAILURE",
    "FORMAL_COUNTEREXAMPLE": "FORMAL_SEMANTIC_FAILURE",
    "FORMAL_INCONCLUSIVE": "FORMAL_INCONCLUSIVE",
    "TIMING_NO_GAIN": "TIMING_NO_GAIN",
    "TIMING_REGRESSION": "TIMING_REGRESSION",
    "CRITICAL_PATH_MIGRATION": "CRITICAL_PATH_MIGRATION",
    "HOLD_REGRESSION": "HOLD_REGRESSION",
    "AREA_POLICY_EXCEEDED": "AREA_POLICY_VIOLATION",
    "POWER_POLICY_EXCEEDED": "POWER_POLICY_VIOLATION",
    "PHYSICAL_CORRELATION_MISS": "PHYSICAL_CORRELATION_MISS",
    "CONGESTION_RISK": "CONGESTION_OR_ROUTABILITY_RISK",
    "VALID_BUT_DOMINATED": "VALID_BUT_DOMINATED",
}

EVIDENCE_KIND = {
    "CONSTRAINT_BINDING_DELTA": "BINDING",
    "CDC_INVARIANT_DELTA": "CDC",
    "FORMAL_MODEL_MISMATCH": "FORMAL",
    "FORMAL_SEMANTIC_FAILURE": "FORMAL",
    "FORMAL_INCONCLUSIVE": "FORMAL",
    "RTL_PARSE_OR_ELAB_FAILURE": "SOURCE_SPAN",
    "FAST_SYNTH_STRUCTURAL_FAILURE": "CONE",
    "TIMING_NO_GAIN": "METRIC",
    "TIMING_REGRESSION": "METRIC",
    "CRITICAL_PATH_MIGRATION": "PATH",
    "HOLD_REGRESSION": "METRIC",
    "AREA_POLICY_VIOLATION": "METRIC",
    "POWER_POLICY_VIOLATION": "METRIC",
    "PHYSICAL_CORRELATION_MISS": "PHYSICAL",
    "CONGESTION_OR_ROUTABILITY_RISK": "PHYSICAL",
}


class ClassificationError(ValueError):
    pass


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _metric_delta(candidate: Any, baseline: Any) -> dict[str, float]:
    if candidate is None or baseline is None:
        return {}
    output: dict[str, float] = {}
    candidate_metrics = getattr(candidate, "per_view_metrics", {})
    baseline_metrics = getattr(baseline, "per_view_metrics", {})
    for view_id in sorted(set(candidate_metrics) & set(baseline_metrics)):
        for field in ("setup_wns_ns", "hold_wns_ns", "mapped_area_um2", "physical_area_um2"):
            after = getattr(candidate_metrics[view_id], field, None)
            before = getattr(baseline_metrics[view_id], field, None)
            if after is not None and before is not None:
                output[f"{view_id}_{field}_delta"] = float(after - before)
    return output


def classify(
    stage_result: StageResult,
    candidate: Any,
    baseline: Any,
    policy: RecoveryPolicyRegistry,
) -> FailureEvent:
    """Classify exactly one non-pass result using normalized codes and fixed priority."""

    if stage_result.status == "PASS":
        raise ClassificationError("failure classifier accepts only a non-pass StageResult")

    if stage_result.status == "INFRASTRUCTURE_ERROR":
        family: FailureFamily = "INFRASTRUCTURE_TRANSIENT"
    else:
        matches = {
            DIAGNOSTIC_FAMILIES[item.code]
            for item in stage_result.diagnostics
            if item.code in DIAGNOSTIC_FAMILIES
        }
        family = (
            min(matches, key=lambda item: policy.rules[item].priority)
            if matches
            else "ADAPTER_OR_PARSER_ERROR"
        )

    rule = policy.rules[family]
    ambiguous = stage_result.status != "INFRASTRUCTURE_ERROR" and not matches
    subject_type = "INFRASTRUCTURE" if family == "INFRASTRUCTURE_TRANSIENT" else "CANDIDATE"
    subject_id = (
        stage_result.run_id if subject_type == "INFRASTRUCTURE" else stage_result.candidate_id
    )
    snapshot_hash = stage_result.input_hashes.rtl_snapshot
    if snapshot_hash is None:
        raise ClassificationError("stage result lacks RTL snapshot identity")
    artifact_id = stage_result.raw_artifacts[0].artifact_id
    evidence_kinds = rule.required_evidence_kinds or (
        EVIDENCE_KIND.get(family, "HISTORY"),
    )
    evidence = tuple(
        EvidenceRef(
            evidence_id=_stable_id(
                "evidence", stage_result.stage_result_id, family, kind
            ),
            kind=kind,
            artifact_id=artifact_id,
            json_pointer=f"/diagnostics/{kind.lower()}",
            snapshot_hash=snapshot_hash,
        )
        for kind in evidence_kinds
    )
    metric_delta = _metric_delta(candidate, baseline)
    event_id = _stable_id(
        "failure",
        stage_result.stage_result_id,
        family,
        policy.policy_hash,
        canonical_sha256(metric_delta),
        *(item.evidence_id for item in evidence),
    )
    return FailureEvent(
        failure_event_id=event_id,
        run_id=stage_result.run_id,
        subject_type=subject_type,
        subject_id=subject_id,
        candidate_id=stage_result.candidate_id,
        proposal_id=getattr(candidate, "proposal_id", None),
        parent_candidate_id=getattr(candidate, "parent_candidate_id", None),
        opportunity_id=getattr(candidate, "opportunity_id", None),
        failed_stage=stage_result.stage,
        analysis_view_id=stage_result.analysis_view_id,
        failure_family=family,
        failure_scope=subject_type,
        repairability="HUMAN_REVIEW" if ambiguous else rule.repairability,
        severity=rule.severity,
        retryable=False if ambiguous else rule.retryable,
        constraint_hash_verified=getattr(stage_result.input_hashes, "constraints", None)
        is not None,
        analysis_view_hash_verified=(
            stage_result.analysis_view_id is None
            or getattr(stage_result.input_hashes, "analysis_view", None) is not None
        ),
        constraint_binding_status=(
            "FORBIDDEN_DELTA" if family == "CONSTRAINT_BINDING_DELTA" else "NOT_CHECKED"
        ),
        protected_structure_status=(
            "CHANGED"
            if family in {"CDC_INVARIANT_DELTA", "PROTECTED_STRUCTURE_VIOLATION"}
            else "UNKNOWN"
        ),
        metric_delta=metric_delta,
        primary_evidence_refs=evidence,
        raw_stage_result_ref=stage_result.stage_result_id,
        classifier_version=CLASSIFIER_VERSION,
    )


__all__ = ["CLASSIFIER_VERSION", "ClassificationError", "classify"]
