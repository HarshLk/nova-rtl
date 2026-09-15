from __future__ import annotations

import pytest

from nova_rtl.evaluation.acceptance import classify_failure_injection


@pytest.mark.parametrize(
    ("scenario", "family", "disposition"),
    (
        ("formal_counterexample", "FORMAL_SEMANTIC_FAILURE", "NARROW_TRANSFORM_SCOPE"),
        ("hold_regression", "HOLD_REGRESSION", "LOCAL_PARAMETER_REVISION"),
        ("changed_sdc_binding", "CONSTRAINT_BINDING_DELTA", "REJECT_CANDIDATE"),
        ("new_cdc_crossing", "CDC_INVARIANT_DELTA", "REJECT_CANDIDATE"),
        ("formal_compilation_delta", "FORMAL_MODEL_MISMATCH", "STOP_RUN_OR_REQUEST_HUMAN"),
        ("area_policy_failure", "AREA_POLICY_VIOLATION", "LOCAL_PARAMETER_REVISION"),
        ("path_migration", "CRITICAL_PATH_MIGRATION", "OPPORTUNITY_REANALYSIS"),
        ("physical_noncorrelation", "PHYSICAL_CORRELATION_MISS", "PHYSICAL_ONLY_RECOMMENDATION"),
        ("repeated_non_progress", "REPEATED_NON_PROGRESS", "TARGET_NEW_PATH_CLUSTER"),
        ("transient_adapter_failure", "INFRASTRUCTURE_TRANSIENT", "RETRY_INFRASTRUCTURE"),
    ),
)
def test_required_failure_injection_routes_safely(
    scenario: str, family: str, disposition: str
) -> None:
    result = classify_failure_injection(scenario)

    assert result == (family, disposition)
