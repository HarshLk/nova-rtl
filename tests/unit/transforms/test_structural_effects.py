from __future__ import annotations

import pytest

from nova_rtl.transforms.registry import competition_mvp_registry
from nova_rtl.transforms.structural_effects import (
    StructuralEffectError,
    StructuralEffectObservation,
    validate_structural_effect,
)


@pytest.mark.parametrize(
    ("operation", "observation"),
    (
        (
            "RESTRUCTURE_PRIORITY_MUX",
            StructuralEffectObservation(
                baseline_depth=5,
                candidate_depth=3,
                baseline_duplicate_expressions=0,
                candidate_duplicate_expressions=0,
                baseline_decode_levels=0,
                candidate_decode_levels=0,
            ),
        ),
        (
            "BALANCE_BOOLEAN_TREE",
            StructuralEffectObservation(
                baseline_depth=6,
                candidate_depth=3,
                baseline_duplicate_expressions=0,
                candidate_duplicate_expressions=0,
                baseline_decode_levels=0,
                candidate_decode_levels=0,
            ),
        ),
        (
            "FACTOR_COMMON_PREDICATE",
            StructuralEffectObservation(
                baseline_depth=3,
                candidate_depth=3,
                baseline_duplicate_expressions=4,
                candidate_duplicate_expressions=1,
                baseline_decode_levels=0,
                candidate_decode_levels=0,
            ),
        ),
        (
            "FSM_DECODE_RESTRUCTURE",
            StructuralEffectObservation(
                baseline_depth=4,
                candidate_depth=4,
                baseline_duplicate_expressions=0,
                candidate_duplicate_expressions=0,
                baseline_decode_levels=4,
                candidate_decode_levels=1,
            ),
        ),
    ),
)
def test_declared_structural_effects_require_measured_progress(
    operation: str, observation: StructuralEffectObservation
) -> None:
    descriptor = competition_mvp_registry().get_descriptor(operation)

    result = validate_structural_effect(descriptor, observation)

    assert result.status == "PASS"
    assert result.operation == operation
    assert result.result_hash.startswith("sha256:")


def test_missing_predicted_structural_direction_fails_closed() -> None:
    descriptor = competition_mvp_registry().get_descriptor("BALANCE_BOOLEAN_TREE")
    unchanged = StructuralEffectObservation(
        baseline_depth=4,
        candidate_depth=4,
        baseline_duplicate_expressions=0,
        candidate_duplicate_expressions=0,
        baseline_decode_levels=0,
        candidate_decode_levels=0,
    )

    with pytest.raises(StructuralEffectError, match="REDUCE_BOOLEAN_DEPTH"):
        validate_structural_effect(descriptor, unchanged)
