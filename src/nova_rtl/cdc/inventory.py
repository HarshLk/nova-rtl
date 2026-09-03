"""Structural CDC inventory construction and approved-registry matching."""

from __future__ import annotations

from typing import Literal

from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.analysis import CdcCrossing, CDCInventory
from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    StrictContract,
    canonical_sha256,
)


class CdcCrossingObservation(StrictContract):
    """Raw structural crossing evidence before registry approval."""

    crossing_id: EntityId
    source_domain_id: EntityId
    destination_domain_id: EntityId
    source_object: NonEmptyString
    destination_object: NonEmptyString
    signal_class: Literal[
        "SINGLE_BIT_CONTROL",
        "MULTI_BIT_DATA",
        "PULSE",
        "FIFO_POINTER",
        "RESET",
    ]
    recognized_pattern: EntityId | None
    structural_fingerprint: HashRef
    protocol_property_refs: tuple[EntityId, ...]
    is_combinational: bool


class StructuralCDCObservation(StrictContract):
    """Complete post-synthesis crossing set and registry identity."""

    stage: Literal["POST_ELABORATION", "POST_SYNTHESIS"]
    approved_pattern_registry_hash: HashRef
    crossings: tuple[CdcCrossingObservation, ...]


def _reject(code: str, message: str) -> None:
    raise SafetyPreflightError(code, message)


def construct_cdc_inventory(
    *,
    candidate_id: str,
    approved_registry: CDCInventory,
    observation: StructuralCDCObservation,
) -> CDCInventory:
    """Seal only an exact, non-combinational match to the approved CDC registry."""

    if observation.stage != "POST_SYNTHESIS":
        _reject("CDC_STAGE_INVALID", "CDC evidence must come from post-synthesis structure")
    if observation.approved_pattern_registry_hash != (
        approved_registry.approved_pattern_registry_hash
    ):
        _reject("CDC_REGISTRY_HASH_MISMATCH", "approved CDC registry identity changed")
    if any(
        (
            approved_registry.new_unapproved_count,
            approved_registry.changed_approved_structure_count,
            approved_registry.removed_approved_structure_count,
            approved_registry.ambiguous_count,
        )
    ) or any(item.status != "APPROVED" for item in approved_registry.crossings):
        _reject("CDC_REGISTRY_INVALID", "approved CDC registry is not a clean baseline")

    approved = {item.crossing_id: item for item in approved_registry.crossings}
    observed = {item.crossing_id: item for item in observation.crossings}
    if len(observed) != len(observation.crossings):
        _reject("CDC_INVENTORY_AMBIGUOUS", "observed CDC crossing identities are duplicated")
    extra = sorted(set(observed) - set(approved))
    if extra:
        if any(observed[crossing_id].is_combinational for crossing_id in extra):
            _reject(
                "UNAPPROVED_COMBINATIONAL_CDC",
                f"new combinational cross-domain edges: {', '.join(extra)}",
            )
        _reject("UNAPPROVED_CDC_CROSSING", f"new CDC crossings: {', '.join(extra)}")
    missing = sorted(set(approved) - set(observed))
    if missing:
        _reject("CDC_APPROVED_STRUCTURE_REMOVED", f"removed CDC crossings: {', '.join(missing)}")

    sealed: list[CdcCrossing] = []
    for crossing_id in sorted(approved):
        baseline = approved[crossing_id]
        current = observed[crossing_id]
        if current.structural_fingerprint != baseline.structural_fingerprint:
            _reject(
                "CDC_APPROVED_FINGERPRINT_CHANGED",
                f"approved CDC structural fingerprint changed: {crossing_id}",
            )
        current_identity = (
            current.source_domain_id,
            current.destination_domain_id,
            current.source_object,
            current.destination_object,
            current.signal_class,
            current.recognized_pattern,
            current.protocol_property_refs,
            current.is_combinational,
        )
        baseline_identity = (
            baseline.source_domain_id,
            baseline.destination_domain_id,
            baseline.source_object,
            baseline.destination_object,
            baseline.signal_class,
            baseline.recognized_pattern,
            baseline.protocol_property_refs,
            False,
        )
        if current_identity != baseline_identity:
            _reject("CDC_REGISTRY_MISMATCH", f"CDC structure changed: {crossing_id}")
        sealed.append(baseline)

    payload = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "crossings": tuple(item.model_dump(mode="json") for item in sealed),
        "approved_pattern_registry_hash": (
            approved_registry.approved_pattern_registry_hash
        ),
        "new_unapproved_count": 0,
        "changed_approved_structure_count": 0,
        "removed_approved_structure_count": 0,
        "ambiguous_count": 0,
    }
    return CDCInventory(**payload, inventory_hash=canonical_sha256(payload))


__all__ = [
    "CdcCrossingObservation",
    "StructuralCDCObservation",
    "construct_cdc_inventory",
]
