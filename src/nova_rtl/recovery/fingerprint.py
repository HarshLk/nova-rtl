"""Semantic candidate fingerprints and deterministic stagnation detection."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from pydantic import Field

from nova_rtl.contracts.base import FiniteFloat, StrictContract, canonical_json_bytes
from nova_rtl.contracts.recovery import CandidateFailureFingerprint, FailureEvent
from nova_rtl.recovery.policy import RecoveryPolicyRegistry


class FingerprintEvidence(StrictContract):
    target_cone_fingerprint: str = Field(pattern=r"^[a-z][a-z0-9_]*:v[0-9]+:[A-Za-z0-9._-]+$")
    operation_family: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    operation: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    parameters: dict[str, object]
    ast_delta_tokens: tuple[str, ...]
    mapped_delta_tokens: tuple[str, ...]
    ancestor_lineage: tuple[str, ...] = Field(min_length=1)
    formal_counterexample_fingerprint: str | None
    metric_response_class: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")


class FailureObservation(StrictContract):
    fingerprint: CandidateFailureFingerprint
    wns_improvement_ns: FiniteFloat
    area_change_percent: FiniteFloat


def _fingerprint(kind: str, value: object) -> str:
    digest = sha256(canonical_json_bytes({"value": value})).hexdigest()[:32]
    return f"{kind}:v1:{digest}"


def fingerprint(
    candidate: Any,
    failure: FailureEvent,
    evidence: FingerprintEvidence,
) -> CandidateFailureFingerprint:
    """Build an identity from mechanism-level evidence rather than RTL text."""

    identity = _fingerprint(
        "candidate_failure",
        {
            "candidate_id": candidate.candidate_id,
            "failure_event_id": failure.failure_event_id,
            "schema": "semantic-failure-v1",
        },
    ).split(":")[-1]
    return CandidateFailureFingerprint(
        candidate_failure_fingerprint_id=f"candidate_failure_{identity[:20]}",
        candidate_id=candidate.candidate_id,
        target_cone_fingerprint=evidence.target_cone_fingerprint,
        operation_family=evidence.operation_family,
        operation=evidence.operation,
        parameter_fingerprint=_fingerprint("parameters", evidence.parameters),
        ast_diff_fingerprint=_fingerprint("ast_delta", sorted(evidence.ast_delta_tokens)),
        mapped_delta_fingerprint=_fingerprint(
            "mapped_delta", sorted(evidence.mapped_delta_tokens)
        ),
        ancestor_lineage=evidence.ancestor_lineage,
        failure_family=failure.failure_family,
        formal_counterexample_fingerprint=evidence.formal_counterexample_fingerprint,
        metric_response_class=evidence.metric_response_class,
    )


def semantic_similarity(
    left: CandidateFailureFingerprint,
    right: CandidateFailureFingerprint,
) -> float:
    """Return the provisional M7 weighted mechanism similarity score."""

    score = 0.0
    score += 0.25 * (left.target_cone_fingerprint == right.target_cone_fingerprint)
    score += 0.20 * (
        left.operation_family == right.operation_family and left.operation == right.operation
    )
    score += 0.20 * (left.ast_diff_fingerprint == right.ast_diff_fingerprint)
    score += 0.15 * (left.mapped_delta_fingerprint == right.mapped_delta_fingerprint)
    score += 0.10 * (left.parameter_fingerprint == right.parameter_fingerprint)
    score += 0.10 * (left.ancestor_lineage == right.ancestor_lineage)
    return round(score, 12)


def _no_progress(
    *, wns_improvement_ns: float, area_change_percent: float, policy: RecoveryPolicyRegistry
) -> bool:
    meaningful_wns = wns_improvement_ns > policy.wns_progress_epsilon_ns
    meaningful_area_reduction = -area_change_percent > policy.area_progress_epsilon_percent
    return not meaningful_wns and not meaningful_area_reduction


def detect_stagnation(
    current: CandidateFailureFingerprint,
    history: tuple[FailureObservation, ...],
    *,
    wns_improvement_ns: float,
    area_change_percent: float,
    policy: RecoveryPolicyRegistry,
) -> bool:
    """Detect consecutive similar failures without objective progress."""

    if not _no_progress(
        wns_improvement_ns=wns_improvement_ns,
        area_change_percent=area_change_percent,
        policy=policy,
    ):
        return False
    qualifying = 1
    for observation in reversed(history):
        if observation.fingerprint.failure_family != current.failure_family:
            break
        if semantic_similarity(observation.fingerprint, current) < policy.similarity_threshold:
            break
        if not _no_progress(
            wns_improvement_ns=observation.wns_improvement_ns,
            area_change_percent=observation.area_change_percent,
            policy=policy,
        ):
            break
        qualifying += 1
        if qualifying >= policy.repeated_failure_count:
            return True
    return False


__all__ = [
    "FailureObservation",
    "FingerprintEvidence",
    "detect_stagnation",
    "fingerprint",
    "semantic_similarity",
]
