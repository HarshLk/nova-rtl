"""Post-elaboration and post-synthesis clock-lineage preflight behavior."""

from __future__ import annotations

import pytest

from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.constraints.clocks import (
    ClockStageObservation,
    ObservedGeneratedClock,
    construct_clock_inventory,
)
from nova_rtl.contracts.analysis import (
    ClockInventory,
    ClockLineageEdge,
    GeneratedClockEntry,
    MasterClockEntry,
)
from nova_rtl.contracts.base import canonical_sha256


def _expected_inventory() -> ClockInventory:
    masters = tuple(
        MasterClockEntry(
            clock_id=f"clk_{domain}",
            domain_id=domain,
            source_object=f"nebula_top/clk_{domain}",
            period_ns=period,
            waveform_ns=(0.0, period / 2.0),
            active_consumer_count=2,
        )
        for domain, period in (
            ("ingress", 10.0),
            ("schedule", 14.0),
            ("dma", 18.0),
            ("compute", 22.0),
            ("control", 26.0),
        )
    )
    generated = tuple(
        GeneratedClockEntry(
            clock_id=f"gclk_{master.domain_id}_div_002",
            domain_id=master.domain_id,
            master_clock_id=master.clock_id,
            source_object=f"nebula_top/u_{master.domain_id}_dividers/div2/Q",
            multiply_by=1,
            divide_by=2,
            waveform_ns=(0.0, master.period_ns),
            active_consumer_count=1,
        )
        for master in masters
    )
    lineage = tuple(
        ClockLineageEdge(
            parent_clock_id=item.master_clock_id,
            child_clock_id=item.clock_id,
            relationship="GENERATED_FROM",
        )
        for item in generated
    )
    payload = {
        "schema_version": 1,
        "candidate_id": "benchmark_tiny",
        "expected_master_count": 5,
        "expected_generated_clocks_per_master": 1,
        "master_clocks": tuple(item.model_dump(mode="json") for item in masters),
        "generated_clocks": tuple(item.model_dump(mode="json") for item in generated),
        "lineage_edges": tuple(item.model_dump(mode="json") for item in lineage),
        "cross_master_synchronous_relationships": (),
    }
    return ClockInventory(**payload, clock_graph_hash=canonical_sha256(payload))


def _observation(
    stage: str,
    expected: ClockInventory,
    *,
    wrong_ancestor: bool = False,
    omit_last: bool = False,
) -> ClockStageObservation:
    generated = tuple(
        ObservedGeneratedClock(
            clock_id=item.clock_id,
            domain_id=item.domain_id,
            master_ancestor_ids=(
                ("clk_control",) if wrong_ancestor and index == 0 else (item.master_clock_id,)
            ),
            source_object=item.source_object,
            multiply_by=item.multiply_by,
            divide_by=item.divide_by,
            waveform_ns=item.waveform_ns,
            active_consumer_count=item.active_consumer_count,
        )
        for index, item in enumerate(expected.generated_clocks)
    )
    return ClockStageObservation(
        stage=stage,
        master_clocks=expected.master_clocks,
        generated_clocks=generated[:-1] if omit_last else generated,
    )


def test_matching_elaboration_and_synthesis_build_canonical_clock_inventory() -> None:
    expected = _expected_inventory()

    inventory = construct_clock_inventory(
        candidate_id="candidate_001",
        expected=expected,
        post_elaboration=_observation("POST_ELABORATION", expected),
        post_synthesis=_observation("POST_SYNTHESIS", expected),
    )

    assert inventory.candidate_id == "candidate_001"
    assert inventory.clock_graph_hash == construct_clock_inventory(
        candidate_id="candidate_001",
        expected=expected,
        post_elaboration=_observation("POST_ELABORATION", expected),
        post_synthesis=_observation("POST_SYNTHESIS", expected),
    ).clock_graph_hash
    assert len(inventory.master_clocks) == 5
    assert len(inventory.generated_clocks) == 5


def test_generated_clock_with_wrong_master_ancestor_is_rejected() -> None:
    expected = _expected_inventory()

    with pytest.raises(SafetyPreflightError) as failure:
        construct_clock_inventory(
            candidate_id="candidate_001",
            expected=expected,
            post_elaboration=_observation(
                "POST_ELABORATION", expected, wrong_ancestor=True
            ),
            post_synthesis=_observation("POST_SYNTHESIS", expected),
        )

    assert failure.value.code == "GENERATED_CLOCK_ANCESTRY_INVALID"


def test_synthesis_clock_inventory_cannot_drop_a_generated_clock() -> None:
    expected = _expected_inventory()

    with pytest.raises(SafetyPreflightError) as failure:
        construct_clock_inventory(
            candidate_id="candidate_001",
            expected=expected,
            post_elaboration=_observation("POST_ELABORATION", expected),
            post_synthesis=_observation("POST_SYNTHESIS", expected, omit_last=True),
        )

    assert failure.value.code == "CLOCK_INVENTORY_MISMATCH"
