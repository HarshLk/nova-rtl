from __future__ import annotations

from types import SimpleNamespace

import pytest

from nova_rtl.analysis_views.comparability import (
    ApprovedIdentityRemap,
    IncomparableResultsError,
    assert_comparable,
)
from nova_rtl.contracts.base import StageInputHashes


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def hashes(*, power: str | None = None) -> StageInputHashes:
    return StageInputHashes(
        rtl_snapshot=hash_ref("1"),
        design_contract=hash_ref("2"),
        constraints=hash_ref("3"),
        constraint_binding=hash_ref("4"),
        analysis_view=hash_ref("5"),
        power_activity=hash_ref(power) if power else None,
        platform_lock=hash_ref("6"),
        tool_recipe=hash_ref("7"),
        formal_model=None,
        parent_stage_result=None,
        extensions={},
    )


def result(input_hashes: StageInputHashes, view_id: str = "func_setup") -> object:
    return SimpleNamespace(input_hashes=input_hashes, analysis_view_id=view_id)


def test_timing_comparability_requires_complete_identical_identity() -> None:
    baseline = result(hashes())
    candidate = result(hashes())

    comparison = assert_comparable(baseline, candidate, "TIMING")

    assert comparison.comparable
    assert comparison.metric_family == "TIMING"
    assert set(comparison.identity_hashes) == {
        "platform_lock",
        "tool_recipe",
        "constraints",
        "constraint_binding",
        "analysis_view",
    }


@pytest.mark.parametrize(
    "field",
    ["platform_lock", "tool_recipe", "constraints", "constraint_binding", "analysis_view"],
)
def test_timing_comparability_fails_closed_on_identity_mismatch(field: str) -> None:
    baseline_hashes = hashes()
    candidate_hashes = baseline_hashes.model_copy(update={field: hash_ref("8")})

    with pytest.raises(IncomparableResultsError, match=field):
        assert_comparable(result(baseline_hashes), result(candidate_hashes), "TIMING")


def test_power_comparability_additionally_requires_identical_activity() -> None:
    baseline = result(hashes(power="8"), "func_power")
    candidate = result(hashes(power="9"), "func_power")

    with pytest.raises(IncomparableResultsError, match="power_activity"):
        assert_comparable(baseline, candidate, "POWER")

    comparison = assert_comparable(baseline, result(hashes(power="8"), "func_power"), "POWER")
    assert comparison.identity_hashes["power_activity"] == hash_ref("8")


def test_view_label_mismatch_is_incomparable_even_when_hashes_match() -> None:
    with pytest.raises(IncomparableResultsError, match="analysis_view_id"):
        assert_comparable(
            result(hashes(), "func_setup"),
            result(hashes(), "func_hold"),
            "TIMING",
        )


def test_reviewed_semantic_binding_remap_allows_candidate_comparison() -> None:
    baseline_hashes = hashes()
    candidate_hashes = baseline_hashes.model_copy(update={"constraint_binding": hash_ref("8")})
    remap = ApprovedIdentityRemap.build(
        identity_name="constraint_binding",
        baseline_identity_hash=hash_ref("4"),
        candidate_identity_hash=hash_ref("8"),
        semantic_identity_hash=hash_ref("9"),
        justification="IDENTICAL_RESOLVED_SELECTORS_AND_ENDPOINT_COVERAGE",
    )

    comparison = assert_comparable(
        result(baseline_hashes),
        result(candidate_hashes),
        "TIMING",
        approved_remaps=(remap,),
    )

    assert comparison.identity_hashes["constraint_binding"] == hash_ref("9")
    assert comparison.approved_identity_remaps == (remap,)


def test_semantic_remap_must_bind_the_exact_mismatched_identities() -> None:
    baseline_hashes = hashes()
    candidate_hashes = baseline_hashes.model_copy(update={"constraint_binding": hash_ref("8")})
    wrong = ApprovedIdentityRemap.build(
        identity_name="constraint_binding",
        baseline_identity_hash=hash_ref("3"),
        candidate_identity_hash=hash_ref("8"),
        semantic_identity_hash=hash_ref("9"),
        justification="IDENTICAL_RESOLVED_SELECTORS_AND_ENDPOINT_COVERAGE",
    )

    with pytest.raises(IncomparableResultsError, match="constraint_binding"):
        assert_comparable(
            result(baseline_hashes),
            result(candidate_hashes),
            "TIMING",
            approved_remaps=(wrong,),
        )
