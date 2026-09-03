from __future__ import annotations

import json
from pathlib import Path

import pytest
import zstandard
from pydantic import ValidationError

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.optimization import OptimizationOpportunity
from nova_rtl.evidence.graph import (
    EvidenceGraphBuildInputs,
    EvidenceGraphDocument,
    build_evidence_graph,
)
from nova_rtl.evidence.opportunities import (
    EvidenceGraphView,
    OpportunityPolicy,
    default_opportunity_policy,
    form_opportunities,
    rank_opportunities,
)
from nova_rtl.evidence.paths import cluster_paths
from tests.unit.evidence.test_graph import graph_inputs
from tests.unit.evidence.test_paths import mux_source_map, path


def opportunity_fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    paths = (
        path("path_setup", view="asap7_setup", slack=-0.3),
        path("path_cdc", view="asap7_setup", slack=-0.2, protected=True),
    )
    base = graph_inputs()
    inputs = EvidenceGraphBuildInputs(
        input_identity=base.input_identity,
        source_map=mux_source_map(),
        critical_paths=paths,
        clock_inventory=base.clock_inventory,
        cdc_inventory=base.cdc_inventory,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    snapshot = build_evidence_graph(inputs, artifact_store=store)
    document = EvidenceGraphDocument.model_validate_json(
        zstandard.ZstdDecompressor().decompress(
            store.open_verified(snapshot.graph_artifact).read()
        )
    )
    clusters = cluster_paths(
        paths,
        evidence_snapshot_hash=snapshot.snapshot_hash,
        source_map=inputs.source_map,
        clock_domain_by_id={
            "clk_master_0": "domain_compute",
            "clk_master_1": "domain_control",
        },
    )
    return EvidenceGraphView(snapshot=snapshot, document=document), clusters


def test_editable_priority_mux_opportunity_is_schema_valid(tmp_path: Path) -> None:
    graph, clusters = opportunity_fixture(tmp_path)
    cluster = next(item for item in clusters if item.target_domain == "domain_compute")

    opportunities = form_opportunities(cluster, graph, default_opportunity_policy())

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert isinstance(opportunity, OptimizationOpportunity)
    assert opportunity.editability == "RTL_EDITABLE"
    assert opportunity.eligible_transform_families == ("RESTRUCTURE_PRIORITY_MUX",)
    assert opportunity.proof_contracts == ("STRICT_SEQ_EQUIV",)
    assert opportunity.source_spans
    assert {ref.snapshot_hash for ref in opportunity.evidence_refs} == {
        graph.snapshot.snapshot_hash
    }
    assert set(opportunity.source_spans).issubset(
        {ref.evidence_id for ref in opportunity.evidence_refs}
    )


def test_protected_cone_never_authorizes_a_transform(tmp_path: Path) -> None:
    graph, clusters = opportunity_fixture(tmp_path)
    cluster = next(item for item in clusters if item.target_domain == "domain_control")

    opportunity = form_opportunities(cluster, graph, default_opportunity_policy())[0]

    assert opportunity.editability == "PROTECTED_OR_UNSAFE"
    assert opportunity.eligible_transform_families == ()
    assert opportunity.proof_contracts == ()
    assert opportunity.protected_neighbors


def test_ranking_is_deterministic_and_not_agent_supplied(tmp_path: Path) -> None:
    graph, clusters = opportunity_fixture(tmp_path)
    policy = default_opportunity_policy()

    first = rank_opportunities(clusters, graph, policy)
    second = rank_opportunities(tuple(reversed(clusters)), graph, policy)

    assert first == second
    assert first.opportunities[0].editability == "RTL_EDITABLE"
    assert first.priority_scores[first.opportunities[0].opportunity_id] > (
        first.priority_scores[first.opportunities[1].opportunity_id]
    )
    serialized = json.loads(first.model_dump_json())
    assert "priority_scores" in serialized
    assert "agent_score" not in serialized


def test_opportunity_policy_is_self_hashed_and_closed_to_known_families() -> None:
    policy = default_opportunity_policy()
    assert policy.policy_hash.startswith("sha256:")

    payload = policy.model_dump(mode="json")
    payload["available_transform_families"] = ["UNKNOWN_TRANSFORM"]
    with pytest.raises(ValidationError, match="registered cause-to-transform mapping"):
        OpportunityPolicy.model_validate(payload)
