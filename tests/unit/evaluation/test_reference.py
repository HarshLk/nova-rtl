from __future__ import annotations

import pytest

from nova_rtl.evaluation.reference import ReferenceInputs, seal_reference_candidate


def _hash(seed: str) -> str:
    return "sha256:" + seed * 64


def _inputs() -> ReferenceInputs:
    return ReferenceInputs(
        run_id="run_full",
        profile="full",
        master_clock_count=5,
        generated_clock_count=105,
        mapped_cell_count=54335,
        candidate_run_id="run_full",
        candidate_id="candidate_primary",
        candidate_status="PASS",
        candidate_classification="VALID_NEGATIVE_RESULT",
        selection_class="PRIMARY_STRICT",
        correctness_contract="STRICT_SEQ_EQUIV",
        source_hash=_hash("a"),
        proof_snapshot_hash=_hash("a"),
        patch_hash=_hash("b"),
        constraint_hash=_hash("c"),
        binding_hash=_hash("d"),
        clock_hash=_hash("e"),
        cdc_hash=_hash("f"),
        formal_hash=_hash("1"),
        platform_hash=_hash("2"),
        recipe_hash=_hash("3"),
        required_view_ids=("asap7_hold", "asap7_setup"),
        completed_view_ids=("asap7_hold", "asap7_setup"),
        strict_proof_outcome="PASS",
        unresolved_sdc_selectors=0,
        endpoint_coverage_complete=True,
        binding_status="EQUIVALENT",
        new_unapproved_cdc_crossings=0,
        changed_approved_cdc_structures=0,
    )


def test_full_reference_candidate_is_exactly_sealed_and_accepted() -> None:
    result = seal_reference_candidate(_inputs())

    assert result.acceptance.status == "PASS"
    assert result.seal.candidate_id == "candidate_primary"
    assert result.acceptance.optimization_outcome == "VALID_NEGATIVE_RESULT"


def test_reference_rejects_cross_profile_or_failed_candidate() -> None:
    with pytest.raises(ValueError, match="same full reference run"):
        seal_reference_candidate(_inputs().model_copy(update={"candidate_run_id": "run_tiny"}))
    with pytest.raises(ValueError, match="passing candidate bundle"):
        seal_reference_candidate(_inputs().model_copy(update={"candidate_status": "FAIL"}))
