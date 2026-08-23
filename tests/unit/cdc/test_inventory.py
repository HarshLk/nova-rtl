"""Structural CDC inventory and approved-registry matching behavior."""

from __future__ import annotations

import pytest

from nova_rtl.cdc.inventory import (
    CdcCrossingObservation,
    StructuralCDCObservation,
    construct_cdc_inventory,
)
from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.analysis import CdcCrossing, CDCInventory
from nova_rtl.contracts.base import canonical_sha256


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _registry() -> CDCInventory:
    crossing = CdcCrossing(
        crossing_id="schedule_dma_level",
        source_domain_id="schedule",
        destination_domain_id="dma",
        source_object="u_schedule/active_level",
        destination_object="u_dma/external_event",
        signal_class="SINGLE_BIT_CONTROL",
        recognized_pattern="stable_level_two_flop",
        structural_fingerprint=hash_ref("1"),
        protocol_property_refs=("stable_level_two_flop",),
        status="APPROVED",
    )
    payload = {
        "schema_version": 1,
        "candidate_id": "benchmark_tiny",
        "crossings": (crossing.model_dump(mode="json"),),
        "approved_pattern_registry_hash": hash_ref("2"),
        "new_unapproved_count": 0,
        "changed_approved_structure_count": 0,
        "removed_approved_structure_count": 0,
        "ambiguous_count": 0,
    }
    return CDCInventory(**payload, inventory_hash=canonical_sha256(payload))


def _observation(
    registry: CDCInventory,
    *,
    add_combinational: bool = False,
    fingerprint: str | None = None,
) -> StructuralCDCObservation:
    approved = registry.crossings[0]
    observations = [
        CdcCrossingObservation(
            crossing_id=approved.crossing_id,
            source_domain_id=approved.source_domain_id,
            destination_domain_id=approved.destination_domain_id,
            source_object=approved.source_object,
            destination_object=approved.destination_object,
            signal_class=approved.signal_class,
            recognized_pattern=approved.recognized_pattern,
            structural_fingerprint=fingerprint or approved.structural_fingerprint,
            protocol_property_refs=approved.protocol_property_refs,
            is_combinational=False,
        )
    ]
    if add_combinational:
        observations.append(
            CdcCrossingObservation(
                crossing_id="unsafe_schedule_compute_data",
                source_domain_id="schedule",
                destination_domain_id="compute",
                source_object="u_schedule/checksum",
                destination_object="u_compute/direct_data",
                signal_class="MULTI_BIT_DATA",
                recognized_pattern=None,
                structural_fingerprint=hash_ref("8"),
                protocol_property_refs=(),
                is_combinational=True,
            )
        )
    return StructuralCDCObservation(
        stage="POST_SYNTHESIS",
        approved_pattern_registry_hash=registry.approved_pattern_registry_hash,
        crossings=tuple(observations),
    )


def test_matching_structural_cdc_observation_builds_canonical_inventory() -> None:
    registry = _registry()

    inventory = construct_cdc_inventory(
        candidate_id="candidate_001",
        approved_registry=registry,
        observation=_observation(registry),
    )

    assert inventory.candidate_id == "candidate_001"
    assert inventory.crossings[0].status == "APPROVED"
    assert inventory.new_unapproved_count == 0
    assert inventory.changed_approved_structure_count == 0
    assert inventory.removed_approved_structure_count == 0


def test_unapproved_combinational_cdc_edge_is_rejected() -> None:
    registry = _registry()

    with pytest.raises(SafetyPreflightError) as failure:
        construct_cdc_inventory(
            candidate_id="candidate_001",
            approved_registry=registry,
            observation=_observation(registry, add_combinational=True),
        )

    assert failure.value.code == "UNAPPROVED_COMBINATIONAL_CDC"


def test_changed_approved_cdc_fingerprint_is_rejected() -> None:
    registry = _registry()

    with pytest.raises(SafetyPreflightError) as failure:
        construct_cdc_inventory(
            candidate_id="candidate_001",
            approved_registry=registry,
            observation=_observation(registry, fingerprint=hash_ref("9")),
        )

    assert failure.value.code == "CDC_APPROVED_FINGERPRINT_CHANGED"
