from __future__ import annotations

from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.verification import ConstraintBindingManifest, ConstraintCoverage
from nova_rtl.optimization.comparison import compare_structural_invariants


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _binding(candidate_id: str, *, command_hash: str = "1") -> ConstraintBindingManifest:
    return ConstraintBindingManifest.model_construct(
        schema_version=1,
        binding_manifest_id=f"binding_{candidate_id}",
        candidate_id=candidate_id,
        sdc_hash=_hash("a"),
        netlist_snapshot_hash=_hash("2" if candidate_id == "baseline" else "3"),
        analysis_view_ids=("asap7_hold", "asap7_setup"),
        resolved_commands=(
            {
                "command_id": "create_clock_core",
                "normalized_command_hash": _hash(command_hash),
                "resolved_object_ids": ("port:clk_core",),
                "resolved_object_set_hash": _hash("4"),
            },
        ),
        coverage=ConstraintCoverage(
            sequential_endpoints_total=10,
            timed_endpoints=10,
            reviewed_exception_endpoints=0,
            unresolved_selectors=0,
        ),
        effective_binding_hash=_hash("5" if candidate_id == "baseline" else "6"),
        comparison_to_baseline="EQUIVALENT",
        reviewed_mapping_refs=(),
    )


def _clocks(candidate_id: str, *, consumer_count: int = 1) -> ClockInventory:
    masters = tuple(
        {
            "clock_id": f"clk_{index}",
            "domain_id": f"domain_{index}",
            "source_object": f"port:clk_{index}",
            "period_ns": 10.0 + index,
            "waveform_ns": (0.0, 5.0),
            "active_consumer_count": 1,
        }
        for index in range(5)
    )
    generated = tuple(
        {
            "clock_id": f"gclk_{index}",
            "domain_id": f"domain_{index}",
            "master_clock_id": f"clk_{index}",
            "source_object": f"pin:u_div_{index}/Q",
            "multiply_by": 1,
            "divide_by": 2,
            "waveform_ns": (0.0, 10.0),
            "active_consumer_count": consumer_count,
        }
        for index in range(5)
    )
    edges = tuple(
        {
            "parent_clock_id": f"clk_{index}",
            "child_clock_id": f"gclk_{index}",
            "relationship": "GENERATED_FROM",
        }
        for index in range(5)
    )
    return ClockInventory.model_construct(
        schema_version=1,
        candidate_id=candidate_id,
        expected_master_count=5,
        expected_generated_clocks_per_master=1,
        master_clocks=masters,
        generated_clocks=generated,
        lineage_edges=edges,
        cross_master_synchronous_relationships=(),
        clock_graph_hash=_hash("7" if candidate_id == "baseline" else "8"),
    )


def _cdc(candidate_id: str, *, fingerprint: str = "9") -> CDCInventory:
    return CDCInventory.model_construct(
        schema_version=1,
        candidate_id=candidate_id,
        crossings=(
            {
                "crossing_id": "control_to_dma",
                "source_domain_id": "control",
                "destination_domain_id": "dma",
                "source_object": "u_control/request",
                "destination_object": "u_dma/request_sync",
                "signal_class": "SINGLE_BIT_CONTROL",
                "recognized_pattern": "stable_level_two_flop",
                "structural_fingerprint": _hash(fingerprint),
                "protocol_property_refs": ("prop_level",),
                "status": "APPROVED",
            },
        ),
        approved_pattern_registry_hash=_hash("b"),
        new_unapproved_count=0,
        changed_approved_structure_count=0,
        removed_approved_structure_count=0,
        ambiguous_count=0,
        inventory_hash=_hash("c" if candidate_id == "baseline" else "d"),
    )


def test_structural_comparison_uses_semantics_not_candidate_ids() -> None:
    comparison = compare_structural_invariants(
        baseline_binding=_binding("baseline"),
        candidate_binding=_binding("candidate"),
        baseline_clocks=_clocks("baseline"),
        candidate_clocks=_clocks("candidate"),
        baseline_cdc=_cdc("baseline"),
        candidate_cdc=_cdc("candidate"),
    )

    assert comparison.binding_status == "APPROVED_SEMANTIC_REMAP"
    assert comparison.clock_status == "UNCHANGED"
    assert comparison.cdc_status == "UNCHANGED"
    assert comparison.unresolved_constraint_selectors == 0
    assert comparison.unconstrained_endpoints == 0
    assert comparison.new_or_unapproved_cdc_crossings == 0
    assert comparison.changed_approved_cdc_structures == 0
    assert comparison.comparison_hash == canonical_sha256(
        comparison, exclude=frozenset({"comparison_hash"})
    )


def test_structural_comparison_detects_each_forbidden_semantic_delta() -> None:
    binding_delta = compare_structural_invariants(
        baseline_binding=_binding("baseline"),
        candidate_binding=_binding("candidate", command_hash="f"),
        baseline_clocks=_clocks("baseline"),
        candidate_clocks=_clocks("candidate"),
        baseline_cdc=_cdc("baseline"),
        candidate_cdc=_cdc("candidate"),
    )
    clock_delta = compare_structural_invariants(
        baseline_binding=_binding("baseline"),
        candidate_binding=_binding("candidate"),
        baseline_clocks=_clocks("baseline"),
        candidate_clocks=_clocks("candidate", consumer_count=2),
        baseline_cdc=_cdc("baseline"),
        candidate_cdc=_cdc("candidate"),
    )
    cdc_delta = compare_structural_invariants(
        baseline_binding=_binding("baseline"),
        candidate_binding=_binding("candidate"),
        baseline_clocks=_clocks("baseline"),
        candidate_clocks=_clocks("candidate"),
        baseline_cdc=_cdc("baseline"),
        candidate_cdc=_cdc("candidate", fingerprint="e"),
    )

    assert binding_delta.binding_status == "FORBIDDEN_DELTA"
    assert clock_delta.clock_status == "CHANGED"
    assert cdc_delta.cdc_status == "CHANGED"
