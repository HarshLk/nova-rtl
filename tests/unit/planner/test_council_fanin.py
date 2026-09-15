from pathlib import Path

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.contracts.planning import (
    CouncilRevisionRecord,
    CritiqueDisposition,
    CritiqueObjection,
    CritiqueReport,
    ProposalCard,
)
from nova_rtl.planner.council import CouncilFaninError, build_council_shortlist


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="path_target",
        kind="PATH",
        artifact_id="artifact_graph",
        json_pointer="/paths/path_target",
        snapshot_hash=_hash("a"),
    )


def _critique(critic_class: str, card_id: str, *, objection: bool) -> CritiqueReport:
    role = "formal_critic" if critic_class == "FORMAL" else "ppa_critic"
    objections = ()
    if objection:
        objections = (
            CritiqueObjection(
                objection_id=f"objection_{critic_class.lower()}",
                severity="MANDATORY" if critic_class == "FORMAL" else "ADVISORY",
                category=f"{critic_class}_RISK",
                message="measured evidence requires an explicit chair disposition",
                evidence_refs=(_evidence(),),
                requested_disposition="REVISE" if critic_class == "FORMAL" else "ACCEPT",
            ),
        )
    return CritiqueReport(
        critique_report_id=f"critique_{critic_class.lower()}_{card_id}",
        critic_role=role,
        critic_class=critic_class,
        proposal_card_id=card_id,
        objections=objections,
    )


def _card() -> ProposalCard:
    return ProposalCard(
        proposal_card_id="proposal_card_target",
        proposal_id="proposal_original",
        normalized_proposal_hash=_hash("b"),
    )


def test_chair_must_disposition_every_objection_and_may_revise_once() -> None:
    card = _card()
    critiques = (
        _critique("FORMAL", card.proposal_card_id, objection=True),
        _critique("PPA", card.proposal_card_id, objection=True),
    )
    dispositions = (
        CritiqueDisposition(
            critique_disposition_id="disposition_formal",
            objection_id="objection_formal",
            action="REVISE",
            reason_code="NARROW_EDIT_SCOPE",
            resulting_proposal_id="proposal_revised",
            chair_evidence_refs=(_evidence(),),
        ),
        CritiqueDisposition(
            critique_disposition_id="disposition_ppa",
            objection_id="objection_ppa",
            action="ACCEPT",
            reason_code="MEASURE_IN_CASCADE",
            resulting_proposal_id="proposal_revised",
            chair_evidence_refs=(_evidence(),),
        ),
    )
    revisions = (
        CouncilRevisionRecord(
            revision_id="revision_target",
            source_proposal_id="proposal_original",
            revised_proposal_id="proposal_revised",
            reason_codes=("NARROW_EDIT_SCOPE",),
        ),
    )

    shortlist = build_council_shortlist(
        cards=(card,),
        critiques=critiques,
        dispositions=dispositions,
        revisions=revisions,
        final_proposal_ids=("proposal_revised",),
        policy_path=Path("config/policy/council.yaml"),
    )

    assert shortlist.status == "PASS"
    assert shortlist.ordered_proposal_ids == ("proposal_revised",)
    assert shortlist.unresolved_mandatory_finding_count == 0


def test_undispositioned_objection_fails_closed() -> None:
    card = _card()
    critiques = (
        _critique("FORMAL", card.proposal_card_id, objection=True),
        _critique("PPA", card.proposal_card_id, objection=False),
    )

    with pytest.raises(CouncilFaninError, match="every critic objection"):
        build_council_shortlist(
            cards=(card,),
            critiques=critiques,
            dispositions=(),
            revisions=(),
            final_proposal_ids=("proposal_original",),
            policy_path=Path("config/policy/council.yaml"),
        )


def test_each_card_requires_both_independent_critics() -> None:
    card = _card()

    with pytest.raises(CouncilFaninError, match="both mandatory critics"):
        build_council_shortlist(
            cards=(card,),
            critiques=(_critique("FORMAL", card.proposal_card_id, objection=False),),
            dispositions=(),
            revisions=(),
            final_proposal_ids=("proposal_original",),
            policy_path=Path("config/policy/council.yaml"),
        )
