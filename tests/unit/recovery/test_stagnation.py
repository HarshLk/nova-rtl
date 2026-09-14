from __future__ import annotations

from types import SimpleNamespace

from nova_rtl.contracts.recovery import FailureEvent
from nova_rtl.recovery.fingerprint import (
    FailureObservation,
    FingerprintEvidence,
    detect_stagnation,
    fingerprint,
    semantic_similarity,
)
from nova_rtl.recovery.policy import load_recovery_policy


def _policy():
    from pathlib import Path

    return load_recovery_policy(
        Path(__file__).resolve().parents[3] / "config/policy/recovery_rules.yaml"
    )


def _failure(family: str = "TIMING_NO_GAIN") -> FailureEvent:
    return FailureEvent.model_construct(
        failure_event_id="failure_timing",
        failure_family=family,
    )


def _candidate(candidate_id: str, parent: str = "baseline"):
    return SimpleNamespace(
        candidate_id=candidate_id,
        parent_candidate_id=parent,
        transform_fingerprint="transform:v1:priority_mux",
    )


def _evidence(**overrides: object) -> FingerprintEvidence:
    values: dict[str, object] = {
        "target_cone_fingerprint": "cone:v1:scheduler_mux",
        "operation_family": "LOGIC_RESTRUCTURE",
        "operation": "RESTRUCTURE_PRIORITY_MUX",
        "parameters": {"fan_in": 8},
        "ast_delta_tokens": ("if_chain", "balanced_mux"),
        "mapped_delta_tokens": ("mux8", "mux4x2"),
        "ancestor_lineage": ("baseline",),
        "formal_counterexample_fingerprint": None,
        "metric_response_class": "NO_MEANINGFUL_GAIN",
    }
    values.update(overrides)
    return FingerprintEvidence.model_validate(values)


def test_semantic_fingerprint_is_deterministic_and_weighted() -> None:
    first = fingerprint(_candidate("candidate_one"), _failure(), _evidence())
    second = fingerprint(_candidate("candidate_one"), _failure(), _evidence())
    different_cone = fingerprint(
        _candidate("candidate_two"),
        _failure(),
        _evidence(target_cone_fingerprint="cone:v1:dma_mux"),
    )

    assert first == second
    assert semantic_similarity(first, second) == 1.0
    assert semantic_similarity(first, different_cone) == 0.75


def test_two_similar_no_progress_failures_trigger_stagnation() -> None:
    previous = fingerprint(_candidate("candidate_one"), _failure(), _evidence())
    current = fingerprint(_candidate("candidate_two"), _failure(), _evidence())
    history = (
        FailureObservation(
            fingerprint=previous,
            wns_improvement_ns=0.005,
            area_change_percent=0.05,
        ),
    )

    assert detect_stagnation(
        current,
        history,
        wns_improvement_ns=0.004,
        area_change_percent=0.02,
        policy=_policy(),
    )


def test_progress_or_different_failure_breaks_stagnation() -> None:
    previous = fingerprint(_candidate("candidate_one"), _failure(), _evidence())
    current = fingerprint(_candidate("candidate_two"), _failure(), _evidence())
    history = (
        FailureObservation(
            fingerprint=previous,
            wns_improvement_ns=0.02,
            area_change_percent=0.05,
        ),
    )

    assert not detect_stagnation(
        current,
        history,
        wns_improvement_ns=0.004,
        area_change_percent=0.02,
        policy=_policy(),
    )


def test_formal_counterexample_and_metric_response_are_semantic_boundaries() -> None:
    first = fingerprint(
        _candidate("candidate_one"),
        _failure("FORMAL_SEMANTIC_FAILURE"),
        _evidence(
            formal_counterexample_fingerprint="counterexample:v1:first",
            metric_response_class="FORMAL_FAILURE",
        ),
    )
    different_counterexample = fingerprint(
        _candidate("candidate_two"),
        _failure("FORMAL_SEMANTIC_FAILURE"),
        _evidence(
            formal_counterexample_fingerprint="counterexample:v1:second",
            metric_response_class="FORMAL_FAILURE",
        ),
    )
    different_response = fingerprint(
        _candidate("candidate_three"),
        _failure(),
        _evidence(metric_response_class="AREA_REGRESSION"),
    )
    timing = fingerprint(_candidate("candidate_four"), _failure(), _evidence())

    assert semantic_similarity(first, different_counterexample) < 0.85
    assert semantic_similarity(timing, different_response) < 0.85
