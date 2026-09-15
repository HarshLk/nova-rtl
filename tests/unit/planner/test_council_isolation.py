from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import EvidenceRef, canonical_sha256
from nova_rtl.contracts.planning import ContextRequest, PlannerRequest
from nova_rtl.planner.council import (
    CouncilIsolationError,
    build_blinded_proposer_contexts,
    load_council_policy,
)
from nova_rtl.planner.evidence import EvidenceObject, InMemoryEvidenceProvider
from nova_rtl.planner.interface import load_planner_policy


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _inputs(tmp_path: Path):
    planner_policy = load_planner_policy(Path("config/policy/planner.yaml"))
    council_policy = load_council_policy(Path("config/policy/council.yaml"))
    objects = tuple(
        EvidenceObject.build(
            evidence_ref=EvidenceRef(
                evidence_id=evidence_id,
                kind="SOURCE_SPAN",
                artifact_id="artifact_graph",
                json_pointer=f"/nodes/{evidence_id}",
                snapshot_hash=_hash("a"),
            ),
            classification="RESTRICTED_RTL",
            payload={"text": text},
        )
        for evidence_id, text in (
            ("timing_private", "timing-only evidence"),
            ("logic_private", "logic-only evidence"),
        )
    )
    provider = InMemoryEvidenceProvider(
        objects,
        grants={
            "timing_forensics": ("timing_private",),
            "logic_domain_specialist": ("logic_private",),
        },
        policy=planner_policy,
    )
    request = PlannerRequest(
        planner_request_id="planner_request_council",
        run_id="run_council",
        parent_candidate_id="baseline",
        opportunity_id="opportunity_target",
        evidence_snapshot_hash=_hash("a"),
        design_contract_hash=_hash("b"),
        policy_hash=council_policy.policy_hash,
        transform_registry_hash=_hash("c"),
        authorized_evidence_ids=("timing_private", "logic_private"),
        planner_mode="AGENT_COUNCIL",
        proposal_limit=2,
        token_budget=10_000,
        latency_budget_ms=120_000,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        deterministic_seed=7,
        required_output_schema_name="optimization-proposal",
        required_output_schema_version=2,
    )
    envelope = {
        "run_id": request.run_id,
        "parent_candidate_id": request.parent_candidate_id,
        "opportunity_id": request.opportunity_id,
        "evidence_snapshot_hash": request.evidence_snapshot_hash,
        "design_contract_hash": request.design_contract_hash,
        "policy_hash": request.policy_hash,
        "transform_registry_hash": request.transform_registry_hash,
        "required_analysis_view_ids": ["asap7_setup", "asap7_hold"],
        "target_analysis_view_id": "asap7_setup",
        "protected_structure_ids": ["clock_divider_bank", "async_fifo"],
        "allowed_transform_operations": ["RESTRUCTURE_PRIORITY_MUX"],
        "allowed_correctness_contracts": ["STRICT_SEQ_EQUIV"],
        "advisory_only": True,
    }
    requests = tuple(
        ContextRequest(
            context_request_id=f"context_request_{role}",
            planner_request_id=request.planner_request_id,
            role=role,
            common_envelope_hash=canonical_sha256(envelope),
            private_evidence_ids=(evidence_id,),
            retrieval_allowlist=(evidence_id,),
            snapshot_hash=request.evidence_snapshot_hash,
            token_budget=3_000,
            redaction_policy_hash=_hash("d"),
        )
        for role, evidence_id in (
            ("timing_forensics", "timing_private"),
            ("logic_domain_specialist", "logic_private"),
        )
    )
    return request, requests, envelope, provider, ArtifactStore(tmp_path / "artifacts")


def test_proposers_receive_distinct_private_contexts(tmp_path: Path) -> None:
    request, requests, envelope, provider, store = _inputs(tmp_path)

    packs = build_blinded_proposer_contexts(
        requests,
        planner_request=request,
        proposer_roles=("timing_forensics", "logic_domain_specialist"),
        common_envelope=envelope,
        evidence_provider=provider,
        artifact_store=store,
    )

    assert tuple(pack.role for pack in packs) == (
        "timing_forensics",
        "logic_domain_specialist",
    )
    assert len({pack.private_pack_hash for pack in packs}) == 2
    timing = store.open_verified(packs[0].private_pack_artifact).read()
    logic = store.open_verified(packs[1].private_pack_artifact).read()
    assert b"logic-only evidence" not in timing
    assert b"timing-only evidence" not in logic


def test_proposer_private_evidence_must_be_disjoint(tmp_path: Path) -> None:
    request, requests, envelope, provider, store = _inputs(tmp_path)
    overlapping = requests[1].model_copy(
        update={
            "private_evidence_ids": ("timing_private",),
            "retrieval_allowlist": ("timing_private",),
        }
    )

    with pytest.raises(CouncilIsolationError, match="overlap"):
        build_blinded_proposer_contexts(
            (requests[0], overlapping),
            planner_request=request,
            proposer_roles=("timing_forensics", "logic_domain_specialist"),
            common_envelope=envelope,
            evidence_provider=provider,
            artifact_store=store,
        )
