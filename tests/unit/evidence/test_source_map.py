from __future__ import annotations

from copy import deepcopy

import pytest

from nova_rtl.contracts.analysis import CriticalPathRecord
from nova_rtl.evidence.source_map import (
    SourceMapError,
    build_source_map,
    enrich_critical_paths,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64

RTL_BUNDLE = """`define WIDTH 1
// NOVA_SOURCE rtl/workload/lane.sv
module lane(input logic a, output logic y);
  assign y = a;
endmodule

// NOVA_SOURCE rtl/cdc/cdc_level_sync.sv
module cdc_level_sync(input logic clk, input logic d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule

// NOVA_SOURCE rtl/nebula_top.sv
module nebula_top(input logic clk, input logic data, output logic result);
endmodule
"""

MAPPED_NETLIST = """module lane(a, y);
  AND2x2_ASAP7_75t_R _1_ (
    .A(a),
    .B(a),
    .Y(y)
  );
endmodule
module cdc_level_sync(clk, d, q);
  DFFHQNx1_ASAP7_75t_R _2_ (
    .CLK(clk),
    .D(d),
    .QN(q)
  );
endmodule
module nebula_top(clk, data, result);
  lane u_lane (
    .a(data[0]),
    .y(result)
  );
  cdc_level_sync u_sync (
    .clk(clk),
    .d(data[0]),
    .q(result)
  );
endmodule
"""

MAPPED_DESIGN = {
    "modules": {
        "lane": {
            "attributes": {"src": "inputs/input_rtl_bundle:3.1-5.9"},
            "ports": {
                "a": {"direction": "input", "bits": [2]},
                "y": {"direction": "output", "bits": [3]},
            },
            "cells": {
                "$logic$1": {
                    "type": "AND2x2_ASAP7_75t_R",
                    "attributes": {"src": "inputs/input_rtl_bundle:4.3-4.15"},
                    "port_directions": {"A": "input", "B": "input", "Y": "output"},
                    "connections": {"A": [2], "B": [2], "Y": [3]},
                }
            },
            "netnames": {},
        },
        "cdc_level_sync": {
            "attributes": {"src": "inputs/input_rtl_bundle:8.1-10.9"},
            "ports": {
                "clk": {"direction": "input", "bits": [2]},
                "d": {"direction": "input", "bits": [3]},
                "q": {"direction": "output", "bits": [4]},
            },
            "cells": {
                "$ff$1": {
                    "type": "DFFHQNx1_ASAP7_75t_R",
                    "attributes": {"src": "inputs/input_rtl_bundle:9.3-9.38"},
                    "port_directions": {"CLK": "input", "D": "input", "QN": "output"},
                    "connections": {"CLK": [2], "D": [3], "QN": [4]},
                }
            },
            "netnames": {},
        },
        "nebula_top": {
            "attributes": {"top": "1", "src": "inputs/input_rtl_bundle:13.1-14.9"},
            "ports": {
                "clk": {"direction": "input", "bits": [2]},
                "data": {"direction": "input", "bits": [3, 5, 6]},
                "result": {"direction": "output", "bits": [4]},
            },
            "cells": {
                "u_lane": {
                    "type": "lane",
                    "attributes": {"src": "inputs/input_rtl_bundle:13.1-14.9"},
                    "port_directions": {"a": "input", "y": "output"},
                    "connections": {"a": [3], "y": [4]},
                },
                "u_sync": {
                    "type": "cdc_level_sync",
                    "attributes": {"src": "inputs/input_rtl_bundle:13.1-14.9"},
                    "port_directions": {"clk": "input", "d": "input", "q": "output"},
                    "connections": {"clk": [2], "d": [3], "q": [4]},
                },
            },
            "netnames": {},
        },
    }
}


def source_map():  # type: ignore[no-untyped-def]
    return build_source_map(
        MAPPED_DESIGN,
        mapped_netlist=MAPPED_NETLIST,
        rtl_bundle=RTL_BUNDLE,
        candidate_id="baseline",
        rtl_snapshot_hash=HASH_A,
        synthesis_structure_hash=HASH_B,
        protected_modules=("cdc_level_sync",),
        protected_path_patterns=("rtl/cdc/**",),
    )


def test_source_map_resolves_emitted_yosys_names_to_rtl_spans() -> None:
    snapshot = source_map()
    objects = {item.semantic_name: item for item in snapshot.mapped_objects}
    spans = {item.source_span_id: item for item in snapshot.source_spans}

    lane_pin = objects["pin:u_lane/_1_/Y"]
    lane_span = spans[lane_pin.source_span_ids[0]]
    assert lane_pin.cell_type == "AND2x2_ASAP7_75t_R"
    assert lane_pin.fanout == 0
    assert lane_span.relative_path == "rtl/workload/lane.sv"
    assert (lane_span.start_line, lane_span.end_line) == (2, 2)
    assert lane_span.protected is False
    assert objects["pin:data[2]"].source_span_ids

    sync_pin = objects["pin:u_sync/_2_/D"]
    sync_span = spans[sync_pin.source_span_ids[0]]
    assert sync_pin.protected is True
    assert sync_pin.protection_kinds == ("CDC",)
    assert sync_span.relative_path == "rtl/cdc/cdc_level_sync.sv"
    assert sync_span.protection_kinds == ("CDC",)

    assert snapshot.source_map_hash == source_map().source_map_hash


def test_source_map_enriches_paths_and_preserves_path_identity() -> None:
    path = CriticalPathRecord(
        path_id="path_lane",
        candidate_id="baseline",
        analysis_view_id="asap7_setup",
        path_group="clk_compute",
        launch_clock_id="clk_compute",
        capture_clock_id="clk_compute",
        startpoint="cell:u_lane/_1_",
        endpoint="cell:u_lane/_1_",
        arrival_ns=1.0,
        required_ns=0.8,
        slack_ns=-0.2,
        cell_delay_ns=1.0,
        net_delay_ns=0.0,
        logic_depth=1,
        max_fanout=0,
        object_sequence=("pin:u_lane/_1_/A", "pin:u_lane/_1_/Y"),
        source_span_refs=(),
        raw_report_artifact_id="stage_opensta_stdout",
    )

    enriched = enrich_critical_paths((path,), source_map())[0]

    assert enriched.path_id == path.path_id
    assert len(enriched.source_span_refs) == 1
    assert enriched.max_fanout == 2


def test_source_map_fails_closed_when_json_and_emitted_netlist_diverge() -> None:
    design = deepcopy(MAPPED_DESIGN)
    design["modules"]["lane"]["cells"]["$logic$1"]["type"] = "OR2x2_ASAP7_75t_R"

    with pytest.raises(SourceMapError, match="cell type order"):
        build_source_map(
            design,
            mapped_netlist=MAPPED_NETLIST,
            rtl_bundle=RTL_BUNDLE,
            candidate_id="baseline",
            rtl_snapshot_hash=HASH_A,
            synthesis_structure_hash=HASH_B,
            protected_modules=("cdc_level_sync",),
            protected_path_patterns=("rtl/cdc/**",),
        )


def test_source_map_rejects_noncanonical_json_cell_order_even_for_same_type() -> None:
    design = deepcopy(MAPPED_DESIGN)
    original = design["modules"]["lane"]["cells"]["$logic$1"]
    design["modules"]["lane"]["cells"] = {
        "$logic$2": deepcopy(original),
        "$logic$1": original,
    }
    netlist = MAPPED_NETLIST.replace(
        "  AND2x2_ASAP7_75t_R _1_ (",
        "  AND2x2_ASAP7_75t_R _2_ (\n    .A(a),\n    .B(a),\n    .Y(y)\n  );\n"
        "  AND2x2_ASAP7_75t_R _1_ (",
    )

    with pytest.raises(SourceMapError, match="canonical cell order"):
        build_source_map(
            design,
            mapped_netlist=netlist,
            rtl_bundle=RTL_BUNDLE,
            candidate_id="baseline",
            rtl_snapshot_hash=HASH_A,
            synthesis_structure_hash=HASH_B,
            protected_modules=("cdc_level_sync",),
            protected_path_patterns=("rtl/cdc/**",),
        )


def test_source_map_rejects_same_type_cell_swap_by_connectivity_signature() -> None:
    design = deepcopy(MAPPED_DESIGN["modules"]["lane"])
    design["attributes"]["top"] = "1"
    design["ports"].update(
        {
            "a2": {"direction": "input", "bits": [4]},
            "y2": {"direction": "output", "bits": [5]},
        }
    )
    second = deepcopy(design["cells"]["$logic$1"])
    second["connections"] = {"A": [4], "B": [4], "Y": [5]}
    design["cells"]["$logic$2"] = second
    netlist = """module lane(a, a2, y, y2);
  AND2x2_ASAP7_75t_R _1_ (
    .A(a2),
    .B(a2),
    .Y(y2)
  );
  AND2x2_ASAP7_75t_R _2_ (
    .A(a),
    .B(a),
    .Y(y)
  );
endmodule
"""

    with pytest.raises(SourceMapError, match="connectivity"):
        build_source_map(
            {"modules": {"lane": design}},
            mapped_netlist=netlist,
            rtl_bundle=RTL_BUNDLE,
            candidate_id="baseline",
            rtl_snapshot_hash=HASH_A,
            synthesis_structure_hash=HASH_B,
            protected_modules=(),
            protected_path_patterns=(),
        )


def test_source_map_requires_each_path_to_resolve_to_source() -> None:
    path = CriticalPathRecord(
        path_id="path_unknown",
        candidate_id="baseline",
        analysis_view_id="asap7_setup",
        path_group="clk_compute",
        launch_clock_id="clk_compute",
        capture_clock_id="clk_compute",
        startpoint="cell:unknown",
        endpoint="cell:unknown",
        arrival_ns=1.0,
        required_ns=0.8,
        slack_ns=-0.2,
        cell_delay_ns=1.0,
        net_delay_ns=0.0,
        logic_depth=1,
        max_fanout=0,
        object_sequence=("pin:unknown/A",),
        source_span_refs=(),
        raw_report_artifact_id="stage_opensta_stdout",
    )

    with pytest.raises(SourceMapError, match="no RTL source span"):
        enrich_critical_paths((path,), source_map())
