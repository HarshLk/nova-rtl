from __future__ import annotations

from nova_rtl.contracts.analysis import CriticalPathRecord
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.evidence.paths import ROOT_CAUSE_CATEGORIES, cluster_paths
from nova_rtl.evidence.source_map import SourceMapSnapshot
from tests.unit.evidence.test_source_map import source_map

SNAPSHOT_HASH = "sha256:" + "e" * 64


def mux_source_map() -> SourceMapSnapshot:
    original = source_map()
    objects = []
    for item in original.mapped_objects:
        payload = item.model_dump(mode="json")
        if "u_lane/_1_" in item.semantic_name:
            payload["cell_type"] = "MUX2x1_ASAP7_75t_R"
        objects.append(type(item).model_validate(payload))
    payload = {
        "schema_version": 1,
        "candidate_id": original.candidate_id,
        "rtl_snapshot_hash": original.rtl_snapshot_hash,
        "synthesis_structure_hash": original.synthesis_structure_hash,
        "mapped_objects": tuple(item.model_dump(mode="json") for item in objects),
        "source_spans": tuple(
            item.model_dump(mode="json") for item in original.source_spans
        ),
    }
    return SourceMapSnapshot(
        candidate_id=original.candidate_id,
        rtl_snapshot_hash=original.rtl_snapshot_hash,
        synthesis_structure_hash=original.synthesis_structure_hash,
        mapped_objects=tuple(objects),
        source_spans=original.source_spans,
        source_map_hash=canonical_sha256(payload),
    )


def seeded_lane_source_map() -> SourceMapSnapshot:
    original = source_map()
    objects = []
    for item in original.mapped_objects:
        payload = item.model_dump(mode="json")
        if "u_lane/_1_" in item.semantic_name:
            payload["cell_type"] = "NAND2x1_ASAP7_75t_R"
            payload["module_type"] = "$paramod\\timing_opportunity_lane\\FAMILY=0"
        objects.append(type(item).model_validate(payload))
    payload = {
        "schema_version": 1,
        "candidate_id": original.candidate_id,
        "rtl_snapshot_hash": original.rtl_snapshot_hash,
        "synthesis_structure_hash": original.synthesis_structure_hash,
        "mapped_objects": tuple(item.model_dump(mode="json") for item in objects),
        "source_spans": tuple(
            item.model_dump(mode="json") for item in original.source_spans
        ),
    }
    return SourceMapSnapshot(
        candidate_id=original.candidate_id,
        rtl_snapshot_hash=original.rtl_snapshot_hash,
        synthesis_structure_hash=original.synthesis_structure_hash,
        mapped_objects=tuple(objects),
        source_spans=original.source_spans,
        source_map_hash=canonical_sha256(payload),
    )


def path(
    path_id: str,
    *,
    view: str,
    slack: float,
    protected: bool = False,
    net_delay: float = 0.2,
) -> CriticalPathRecord:
    mapped = mux_source_map()
    if protected:
        sequence = ("pin:u_sync/_2_/D", "pin:u_sync/_2_/QN")
        spans = tuple(
            sorted(
                {
                    span
                    for item in mapped.mapped_objects
                    if "u_sync/_2_" in item.semantic_name
                    for span in item.source_span_ids
                }
            )
        )
        clock = "clk_master_1"
    else:
        sequence = (
            "pin:u_lane/_1_/A",
            "pin:u_lane/_1_/Y",
            "pin:u_lane/_1_/A",
            "pin:u_lane/_1_/Y",
        )
        spans = tuple(
            sorted(
                {
                    span
                    for item in mapped.mapped_objects
                    if "u_lane/_1_" in item.semantic_name
                    for span in item.source_span_ids
                }
            )
        )
        clock = "clk_master_0"
    arrival = 1.0
    required = arrival + slack
    return CriticalPathRecord(
        path_id=path_id,
        candidate_id="baseline",
        analysis_view_id=view,
        path_group=clock,
        launch_clock_id=clock,
        capture_clock_id=clock,
        startpoint="cell:" + sequence[0].removeprefix("pin:").rsplit("/", 1)[0],
        endpoint="cell:" + sequence[-1].removeprefix("pin:").rsplit("/", 1)[0],
        arrival_ns=arrival,
        required_ns=required,
        slack_ns=slack,
        cell_delay_ns=1.0 - net_delay,
        net_delay_ns=net_delay,
        logic_depth=len(sequence),
        max_fanout=4,
        object_sequence=sequence,
        source_span_refs=spans,
        raw_report_artifact_id="stage_opensta_stdout",
    )


def test_paths_cluster_by_shared_cone_not_report_order() -> None:
    paths = (
        path("path_setup", view="asap7_setup", slack=-0.3),
        path("path_hold", view="asap7_hold", slack=-0.1),
        path("path_cdc", view="asap7_setup", slack=-0.2, protected=True),
    )
    kwargs = {
        "evidence_snapshot_hash": SNAPSHOT_HASH,
        "source_map": mux_source_map(),
        "clock_domain_by_id": {
            "clk_master_0": "domain_compute",
            "clk_master_1": "domain_control",
        },
    }

    first = cluster_paths(paths, **kwargs)
    second = cluster_paths(tuple(reversed(paths)), **kwargs)

    assert first == second
    assert len(first) == 2
    compute = next(item for item in first if item.target_domain == "domain_compute")
    assert compute.path_ids == ("path_hold", "path_setup")
    assert compute.analysis_view_ids == ("asap7_hold", "asap7_setup")
    assert compute.worst_analysis_view_id == "asap7_setup"
    assert compute.features.worst_slack_ns == -0.3
    assert compute.features.affected_endpoints == 1
    assert compute.features.mux_depth == 4
    assert compute.root_causes[0].category == "DEEP_PRIORITY_CHAIN"


def test_protected_cone_is_classified_unsafe() -> None:
    clusters = cluster_paths(
        (path("path_cdc", view="asap7_setup", slack=-0.2, protected=True),),
        evidence_snapshot_hash=SNAPSHOT_HASH,
        source_map=mux_source_map(),
        clock_domain_by_id={"clk_master_1": "domain_control"},
    )

    assert clusters[0].protected_neighbor_ids
    assert clusters[0].features.protection_distance == 0
    assert clusters[0].root_causes[0].category == "CDC_ADJACENT_UNSAFE_TO_EDIT"


def test_seeded_timing_lane_retains_priority_family_after_technology_mapping() -> None:
    clusters = cluster_paths(
        (path("path_seeded", view="asap7_setup", slack=-0.2),),
        evidence_snapshot_hash=SNAPSHOT_HASH,
        source_map=seeded_lane_source_map(),
        clock_domain_by_id={"clk_master_0": "domain_compute"},
    )

    assert clusters[0].root_causes[0].category == "DEEP_PRIORITY_CHAIN"


def test_net_dominated_cone_has_no_default_rtl_diagnosis() -> None:
    clusters = cluster_paths(
        (path("path_wire", view="asap7_setup", slack=-0.2, net_delay=0.8),),
        evidence_snapshot_hash=SNAPSHOT_HASH,
        source_map=mux_source_map(),
        clock_domain_by_id={"clk_master_0": "domain_compute"},
    )

    categories = tuple(item.category for item in clusters[0].root_causes)
    assert categories[0] == "PLACEMENT_OR_WIRE_DOMINATED"


def test_root_cause_vocabulary_is_exact() -> None:
    assert ROOT_CAUSE_CATEGORIES == (
        "DEEP_PRIORITY_CHAIN",
        "UNBALANCED_BOOLEAN_TREE",
        "WIDE_COMPARATOR",
        "SERIAL_ARITHMETIC",
        "HIGH_FANOUT_CONTROL",
        "MUX_AFTER_ARITHMETIC",
        "REPEATED_DECODE",
        "FSM_DECODE_DEPTH",
        "RESOURCE_ARBITRATION",
        "PLACEMENT_OR_WIRE_DOMINATED",
        "CLOCK_OR_CONSTRAINT_ISSUE",
        "CDC_ADJACENT_UNSAFE_TO_EDIT",
        "UNCLASSIFIED",
    )
