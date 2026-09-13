from __future__ import annotations

import pytest

from nova_rtl.contracts.base import EvidenceRef
from nova_rtl.planner.evidence import (
    ContextIntegrityError,
    EvidenceObject,
    InMemoryEvidenceProvider,
)
from nova_rtl.planner.interface import load_planner_policy


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _object(evidence_id: str, *, snapshot: str = "a", kind: str = "SOURCE_SPAN"):
    return EvidenceObject.build(
        evidence_ref=EvidenceRef(
            evidence_id=evidence_id,
            kind=kind,
            artifact_id="artifact_graph",
            json_pointer=f"/nodes/{evidence_id}",
            snapshot_hash=_hash(snapshot),
        ),
        classification="RESTRICTED_RTL",
        payload={"text": "assign ready = valid && grant;"},
    )


def _provider() -> InMemoryEvidenceProvider:
    policy = load_planner_policy(__import__("pathlib").Path("config/policy/planner.yaml"))
    objects = (_object("source_span_a"), _object("source_span_b"))
    return InMemoryEvidenceProvider(
        objects,
        grants={"logic_restructuring": ("source_span_a",)},
        policy=policy,
    )


def test_fetch_returns_only_role_authorized_stable_ids() -> None:
    provider = _provider()

    returned = provider.fetch(
        "logic_restructuring",
        ("source_span_a",),
        expected_snapshot_hash=_hash("a"),
        reason="inspect_target_cone",
    )

    assert tuple(item.evidence_ref.evidence_id for item in returned) == (
        "source_span_a",
    )
    assert provider.audit_records[-1].decision == "GRANTED"
    assert provider.audit_records[-1].returned_hash == returned[0].content_hash


def test_fetch_denies_stale_snapshot_and_records_the_denial() -> None:
    provider = _provider()

    with pytest.raises(ContextIntegrityError, match="snapshot"):
        provider.fetch(
            "logic_restructuring",
            ("source_span_a",),
            expected_snapshot_hash=_hash("b"),
            reason="inspect_target_cone",
        )

    assert provider.audit_records[-1].decision == "DENIED"
    assert provider.audit_records[-1].reason_code == "STALE_SNAPSHOT"


@pytest.mark.parametrize("evidence_id", ("../rtl/top.sv", "*.sv", "/rtl/top.sv"))
def test_fetch_denies_paths_and_globs(evidence_id: str) -> None:
    provider = _provider()

    with pytest.raises(ContextIntegrityError, match="stable evidence ID"):
        provider.fetch(
            "logic_restructuring",
            (evidence_id,),
            expected_snapshot_hash=_hash("a"),
            reason="inspect_target_cone",
        )

    assert provider.audit_records[-1].decision == "DENIED"


def test_fetch_denies_another_roles_private_evidence() -> None:
    provider = _provider()

    with pytest.raises(ContextIntegrityError, match="not authorized"):
        provider.fetch(
            "logic_restructuring",
            ("source_span_b",),
            expected_snapshot_hash=_hash("a"),
            reason="inspect_target_cone",
        )

    assert provider.audit_records[-1].reason_code == "UNAUTHORIZED_EVIDENCE"
