from __future__ import annotations

from nova_rtl.evaluation.acceptance import AcceptanceEvidence, evaluate_acceptance


def test_full_reference_accepts_trustworthy_negative_optimization_result() -> None:
    evidence = AcceptanceEvidence.build(
        master_clock_count=5,
        generated_clock_count=105,
        mapped_cell_count=54335,
        unresolved_sdc_selectors=0,
        endpoint_coverage_complete=True,
        binding_status="EQUIVALENT",
        new_unapproved_cdc_crossings=0,
        changed_approved_cdc_structures=0,
        strict_proof_contract="STRICT_SEQ_EQUIV",
        strict_proof_outcome="PASS",
        required_views_complete=True,
        delivered_snapshot_hash="sha256:" + "a" * 64,
        proof_snapshot_hash="sha256:" + "a" * 64,
        every_claim_resolves=True,
        replay_verified=True,
        optimization_outcome="VALID_NEGATIVE_RESULT",
    )

    result = evaluate_acceptance(evidence)

    assert result.status == "PASS"
    assert result.optimization_outcome == "VALID_NEGATIVE_RESULT"
    assert "PPA improvement" in result.disclosures[0]


def test_full_reference_fails_closed_on_missing_clock_or_proof_identity() -> None:
    evidence = AcceptanceEvidence.build(
        master_clock_count=4,
        generated_clock_count=105,
        mapped_cell_count=54335,
        unresolved_sdc_selectors=0,
        endpoint_coverage_complete=True,
        binding_status="EQUIVALENT",
        new_unapproved_cdc_crossings=0,
        changed_approved_cdc_structures=0,
        strict_proof_contract="STRICT_SEQ_EQUIV",
        strict_proof_outcome="PASS",
        required_views_complete=True,
        delivered_snapshot_hash="sha256:" + "a" * 64,
        proof_snapshot_hash="sha256:" + "b" * 64,
        every_claim_resolves=True,
        replay_verified=True,
        optimization_outcome="IMPROVED",
    )

    result = evaluate_acceptance(evidence)

    assert result.status == "FAIL"
    assert "MASTER_CLOCK_COUNT" in result.failed_gates
    assert "PROOF_SNAPSHOT_IDENTITY" in result.failed_gates

