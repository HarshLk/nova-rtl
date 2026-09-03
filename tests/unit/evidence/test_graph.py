from __future__ import annotations

import json
from pathlib import Path

import pytest
import zstandard

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory, CriticalPathRecord
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.evidence.graph import (
    EvidenceGraphBuildError,
    EvidenceGraphBuildInputs,
    build_evidence_graph,
)
from nova_rtl.evidence.models import (
    AnalysisViewEvidenceIdentity,
    build_evidence_input_identity,
)
from tests.unit.contracts.test_evidence_contracts import (
    cdc_inventory_payload,
    clock_inventory_payload,
)
from tests.unit.evidence.test_source_map import HASH_A, HASH_B, source_map


def hash_ref(digit: str) -> str:
    return "sha256:" + digit * 64


def graph_inputs(*, reverse_paths: bool = False) -> EvidenceGraphBuildInputs:
    clock_inventory = ClockInventory.model_validate(clock_inventory_payload())
    cdc_payload = cdc_inventory_payload()
    cdc_payload["crossings"][0]["source_object"] = "u_lane/_1_/A"
    cdc_payload["crossings"][0]["destination_object"] = "u_sync/_2_/D"
    cdc_payload["inventory_hash"] = canonical_sha256(
        {key: value for key, value in cdc_payload.items() if key != "inventory_hash"}
    )
    cdc_inventory = CDCInventory.model_validate(cdc_payload)
    mapped = source_map()
    span_ids = tuple(item.source_span_id for item in mapped.source_spans)
    paths = (
        CriticalPathRecord(
            path_id="path_compute_001",
            candidate_id="baseline",
            analysis_view_id="asap7_setup",
            path_group="clk_compute",
            launch_clock_id="clk_master_0",
            capture_clock_id="clk_master_0",
            startpoint="cell:u_lane/_1_",
            endpoint="cell:u_lane/_1_",
            arrival_ns=1.0,
            required_ns=0.8,
            slack_ns=-0.2,
            cell_delay_ns=0.8,
            net_delay_ns=0.2,
            logic_depth=2,
            max_fanout=2,
            object_sequence=("pin:u_lane/_1_/A", "pin:u_lane/_1_/Y"),
            source_span_refs=(span_ids[0],),
            raw_report_artifact_id="stage_opensta_setup_stdout",
        ),
        CriticalPathRecord(
            path_id="path_compute_002",
            candidate_id="baseline",
            analysis_view_id="asap7_setup",
            path_group="clk_compute",
            launch_clock_id="clk_master_0",
            capture_clock_id="clk_master_0",
            startpoint="cell:u_sync/_2_",
            endpoint="cell:u_sync/_2_",
            arrival_ns=0.5,
            required_ns=0.4,
            slack_ns=-0.1,
            cell_delay_ns=0.5,
            net_delay_ns=0.0,
            logic_depth=1,
            max_fanout=1,
            object_sequence=("pin:u_sync/_2_/D", "pin:u_sync/_2_/QN"),
            source_span_refs=(span_ids[-1],),
            raw_report_artifact_id="stage_opensta_setup_stdout",
        ),
    )
    identity = build_evidence_input_identity(
        candidate_id="baseline",
        rtl_snapshot_hash=HASH_A,
        design_contract_hash=hash_ref("1"),
        constraint_binding_hash=hash_ref("2"),
        platform_lock_hash=hash_ref("3"),
        synthesis_structure_hash=HASH_B,
        clock_inventory_hash=clock_inventory.clock_graph_hash,
        cdc_inventory_hash=cdc_inventory.inventory_hash,
        protection_policy_hash=hash_ref("4"),
        analysis_views=(
            AnalysisViewEvidenceIdentity(
                analysis_view_id="asap7_setup",
                analysis_view_hash=hash_ref("5"),
                opensta_stage_result_hash=hash_ref("6"),
                openroad_stage_result_hash=hash_ref("7"),
                openroad_metrics_hash=hash_ref("8"),
                physical_area_um2=100.0,
                wirelength_um=200.0,
                congestion_overflow=0.0,
                physical_cell_count=1000,
                physical_register_count=100,
                physical_buffer_count=10,
            ),
        ),
    )
    return EvidenceGraphBuildInputs(
        input_identity=identity,
        source_map=mapped,
        critical_paths=tuple(reversed(paths)) if reverse_paths else paths,
        clock_inventory=clock_inventory,
        cdc_inventory=cdc_inventory,
    )


def test_evidence_graph_is_content_addressed_and_order_independent(tmp_path: Path) -> None:
    first_store = ArtifactStore(tmp_path / "first")
    second_store = ArtifactStore(tmp_path / "second")

    first = build_evidence_graph(graph_inputs(), artifact_store=first_store)
    second = build_evidence_graph(
        graph_inputs(reverse_paths=True), artifact_store=second_store
    )

    assert first.snapshot_hash == second.snapshot_hash
    assert first.graph_artifact.sha256 == second.graph_artifact.sha256
    assert first.source_map_artifact.sha256 == second.source_map_artifact.sha256
    assert first.path_record_artifact.sha256 == second.path_record_artifact.sha256
    assert first.node_counts["TIMING_PATH"] == 2
    assert first.node_counts["SOURCE_SPAN"] > 0
    assert first.edge_counts["TIMING_ARC"] == 2

    compressed = first_store.open_verified(first.graph_artifact).read()
    document = json.loads(zstandard.ZstdDecompressor().decompress(compressed))
    assert document["document_hash"].startswith("sha256:")
    assert tuple(item["node_id"] for item in document["nodes"]) == tuple(
        sorted(item["node_id"] for item in document["nodes"])
    )
    assert {
        reference["snapshot_hash"]
        for node in document["nodes"]
        for reference in node["evidence_refs"]
    } == {first.snapshot_hash}


def test_evidence_graph_preserves_protection_and_traceability(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    snapshot = build_evidence_graph(graph_inputs(), artifact_store=store)
    document = json.loads(
        zstandard.ZstdDecompressor().decompress(
            store.open_verified(snapshot.graph_artifact).read()
        )
    )

    protected = [node for node in document["nodes"] if node["protected"]]
    assert protected
    assert any("CDC" in node["protection_kinds"] for node in protected)
    assert any(edge["kind"] == "MAPS_TO" for edge in document["edges"])
    assert any(edge["kind"] == "CLOCKED_BY" for edge in document["edges"])
    assert any(edge["kind"] == "CONNECTS" for edge in document["edges"])
    assert any(edge["kind"] == "DATA_DEPENDENCY" for edge in document["edges"])
    assert any(node["kind"] == "NET" for node in document["nodes"])
    assert any(node["kind"] == "PHYSICAL_REGION" for node in document["nodes"])
    assert store.open_verified(snapshot.source_map_artifact).read()
    assert store.open_verified(snapshot.path_record_artifact).read()


def test_evidence_graph_rejects_hash_or_source_reference_mismatch(tmp_path: Path) -> None:
    inputs = graph_inputs()
    mismatched_identity = inputs.input_identity.model_copy(
        update={"clock_inventory_hash": hash_ref("f")}
    )
    with pytest.raises(EvidenceGraphBuildError, match="clock inventory hash"):
        build_evidence_graph(
            inputs.model_copy(update={"input_identity": mismatched_identity}),
            artifact_store=ArtifactStore(tmp_path / "hash-mismatch"),
        )
    path_payload = inputs.critical_paths[0].model_dump(mode="json")
    path_payload["source_span_refs"] = ["source_missing"]
    invalid_path = CriticalPathRecord.model_validate(path_payload)
    with pytest.raises(EvidenceGraphBuildError, match="unknown source span"):
        build_evidence_graph(
            inputs.model_copy(
                update={"critical_paths": (invalid_path, *inputs.critical_paths[1:])}
            ),
            artifact_store=ArtifactStore(tmp_path / "span-mismatch"),
        )


def test_evidence_graph_rejects_unresolved_cdc_endpoint(tmp_path: Path) -> None:
    inputs = graph_inputs()
    payload = inputs.cdc_inventory.model_dump(mode="json")
    payload["crossings"][0]["source_object"] = "missing/source"
    payload["inventory_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "inventory_hash"}
    )
    inventory = CDCInventory.model_validate(payload)
    identity_payload = inputs.input_identity.model_dump(mode="json")
    identity_payload["cdc_inventory_hash"] = inventory.inventory_hash
    identity_payload["identity_hash"] = canonical_sha256(
        {key: value for key, value in identity_payload.items() if key != "identity_hash"}
    )

    with pytest.raises(EvidenceGraphBuildError, match="unresolved source endpoint"):
        build_evidence_graph(
            inputs.model_copy(
                update={
                    "cdc_inventory": inventory,
                    "input_identity": type(inputs.input_identity).model_validate(
                        identity_payload
                    ),
                }
            ),
            artifact_store=ArtifactStore(tmp_path / "unresolved-cdc"),
        )
