from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.optimization import (
    OptimizationProposal,
    ProposalCorrectness,
    ProposalPrediction,
    ProposalTarget,
    Transformation,
)
from nova_rtl.planner.council import normalize_proposal_cards


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _proposal(identity: str, operation: str) -> OptimizationProposal:
    evidence = EvidenceRef(
        evidence_id="source_span_target",
        kind="SOURCE_SPAN",
        artifact_id="artifact_graph",
        json_pointer="/nodes/source_span_target",
        snapshot_hash=_hash("a"),
    )
    return OptimizationProposal(
        proposal_id=identity,
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        diagnosis_refs=(f"diagnosis_{identity}",),
        target=ProposalTarget(
            hierarchy="nova_top.u_compute",
            source_span_id="source_span_target",
            cone_fingerprint="cone:v1:target",
        ),
        transformation=Transformation(
            operation=operation,
            family="LOGIC_RESTRUCTURING",
            parameters={"max_branches": 8},
        ),
        preconditions=("NO_PROTECTED_NODE_IN_EDIT_SET",),
        correctness=ProposalCorrectness(
            contract="STRICT_SEQ_EQUIV",
            proof_scope="whole_design",
            reset_model="RESET_ASSUMPTIONS_LOCKED",
        ),
        prediction=ProposalPrediction(
            timing_direction="IMPROVE",
            area_direction="NEUTRAL",
            confidence=0.6,
        ),
        evidence_refs=(evidence,),
        abort_conditions=("FORMAL_EQUIVALENCE_FAILURE",),
    )


def test_cards_are_neutral_deduplicated_and_order_independent() -> None:
    first = _proposal("proposal_first", "RESTRUCTURE_PRIORITY_MUX")
    duplicate = _proposal("proposal_duplicate", "RESTRUCTURE_PRIORITY_MUX")
    alternative = _proposal("proposal_alternative", "BALANCE_BOOLEAN_TREE")

    left_cards, left_proposals = normalize_proposal_cards((first, duplicate, alternative))
    right_cards, right_proposals = normalize_proposal_cards((alternative, duplicate, first))

    assert left_cards == right_cards
    assert tuple(item.proposal_id for item in left_proposals) == tuple(
        item.proposal_id for item in right_proposals
    )
    assert len(left_cards) == 2
    assert {item.proposal_id for item in left_cards} == {
        item.proposal_id for item in left_proposals
    }
    assert all("author" not in item.model_dump(mode="json") for item in left_cards)


def test_card_hash_changes_for_a_material_transformation_change() -> None:
    first_cards, _ = normalize_proposal_cards(
        (_proposal("proposal_first", "RESTRUCTURE_PRIORITY_MUX"),)
    )
    second_cards, _ = normalize_proposal_cards(
        (_proposal("proposal_second", "BALANCE_BOOLEAN_TREE"),)
    )

    assert first_cards[0].normalized_proposal_hash != second_cards[0].normalized_proposal_hash
