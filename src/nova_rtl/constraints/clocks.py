"""Clock inventory construction across elaboration and synthesis boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.analysis import (
    ClockInventory,
    ClockLineageEdge,
    GeneratedClockEntry,
    MasterClockEntry,
)
from nova_rtl.contracts.base import (
    EntityId,
    NonEmptyString,
    NonNegativeFloat,
    StrictContract,
    canonical_sha256,
)


class ObservedGeneratedClock(StrictContract):
    """Raw generated-clock evidence before ancestry is trusted."""

    clock_id: EntityId
    domain_id: EntityId
    master_ancestor_ids: tuple[EntityId, ...]
    source_object: NonEmptyString
    multiply_by: int = Field(strict=True, gt=0)
    divide_by: int = Field(strict=True, gt=0)
    waveform_ns: tuple[NonNegativeFloat, NonNegativeFloat]
    active_consumer_count: int = Field(strict=True, gt=0)


class ClockStageObservation(StrictContract):
    """Complete clock graph observed at one compilation boundary."""

    stage: Literal["POST_ELABORATION", "POST_SYNTHESIS"]
    master_clocks: tuple[MasterClockEntry, ...]
    generated_clocks: tuple[ObservedGeneratedClock, ...]


def _reject(code: str, message: str) -> None:
    raise SafetyPreflightError(code, message)


def _validate_stage(
    observation: ClockStageObservation,
    expected: ClockInventory,
) -> None:
    expected_masters = {item.clock_id: item for item in expected.master_clocks}
    observed_masters = {item.clock_id: item for item in observation.master_clocks}
    if (
        len(observed_masters) != len(observation.master_clocks)
        or set(observed_masters) != set(expected_masters)
        or any(
            observed_masters[clock_id] != expected_masters[clock_id]
            for clock_id in expected_masters
        )
    ):
        _reject(
            "CLOCK_INVENTORY_MISMATCH",
            f"{observation.stage} master-clock inventory differs from expectations",
        )

    expected_generated = {item.clock_id: item for item in expected.generated_clocks}
    observed_generated = {item.clock_id: item for item in observation.generated_clocks}
    if (
        len(observed_generated) != len(observation.generated_clocks)
        or set(observed_generated) != set(expected_generated)
    ):
        _reject(
            "CLOCK_INVENTORY_MISMATCH",
            f"{observation.stage} generated-clock inventory differs from expectations",
        )
    for clock_id, expected_clock in expected_generated.items():
        observed = observed_generated[clock_id]
        if observed.master_ancestor_ids != (expected_clock.master_clock_id,):
            _reject(
                "GENERATED_CLOCK_ANCESTRY_INVALID",
                f"{clock_id} must have exactly the expected master ancestor",
            )
        observed_identity = (
            observed.domain_id,
            observed.source_object,
            observed.multiply_by,
            observed.divide_by,
            observed.waveform_ns,
            observed.active_consumer_count,
        )
        expected_identity = (
            expected_clock.domain_id,
            expected_clock.source_object,
            expected_clock.multiply_by,
            expected_clock.divide_by,
            expected_clock.waveform_ns,
            expected_clock.active_consumer_count,
        )
        if observed_identity != expected_identity:
            _reject(
                "CLOCK_INVENTORY_MISMATCH",
                f"{clock_id} structural clock evidence differs from expectations",
            )


def construct_clock_inventory(
    *,
    candidate_id: str,
    expected: ClockInventory,
    post_elaboration: ClockStageObservation,
    post_synthesis: ClockStageObservation,
) -> ClockInventory:
    """Require identical valid lineage at elaboration and synthesis, then seal it."""

    if post_elaboration.stage != "POST_ELABORATION":
        _reject("CLOCK_STAGE_INVALID", "elaboration evidence has the wrong stage identity")
    if post_synthesis.stage != "POST_SYNTHESIS":
        _reject("CLOCK_STAGE_INVALID", "synthesis evidence has the wrong stage identity")
    _validate_stage(post_elaboration, expected)
    _validate_stage(post_synthesis, expected)

    masters = tuple(sorted(post_synthesis.master_clocks, key=lambda item: item.clock_id))
    generated = tuple(
        GeneratedClockEntry(
            clock_id=item.clock_id,
            domain_id=item.domain_id,
            master_clock_id=item.master_ancestor_ids[0],
            source_object=item.source_object,
            multiply_by=item.multiply_by,
            divide_by=item.divide_by,
            waveform_ns=item.waveform_ns,
            active_consumer_count=item.active_consumer_count,
        )
        for item in sorted(post_synthesis.generated_clocks, key=lambda clock: clock.clock_id)
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
        "candidate_id": candidate_id,
        "expected_master_count": 5,
        "expected_generated_clocks_per_master": (
            expected.expected_generated_clocks_per_master
        ),
        "master_clocks": tuple(item.model_dump(mode="json") for item in masters),
        "generated_clocks": tuple(item.model_dump(mode="json") for item in generated),
        "lineage_edges": tuple(item.model_dump(mode="json") for item in lineage),
        "cross_master_synchronous_relationships": (),
    }
    return ClockInventory(**payload, clock_graph_hash=canonical_sha256(payload))


__all__ = [
    "ClockStageObservation",
    "ObservedGeneratedClock",
    "construct_clock_inventory",
]
