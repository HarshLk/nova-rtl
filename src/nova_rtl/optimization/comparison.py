"""Independent semantic comparison of parent and candidate safety evidence."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import HashRef, NonNegativeInt, StrictContract, canonical_sha256
from nova_rtl.contracts.verification import ConstraintBindingManifest


def _dump(item: object) -> object:
    dump = getattr(item, "model_dump", None)
    return dump(mode="json") if callable(dump) else item


def _binding_semantics(binding: ConstraintBindingManifest) -> dict[str, object]:
    return {
        "sdc_hash": binding.sdc_hash,
        "analysis_view_ids": binding.analysis_view_ids,
        "resolved_commands": tuple(_dump(item) for item in binding.resolved_commands),
        "coverage": _dump(binding.coverage),
    }


def _clock_semantics(clocks: ClockInventory) -> dict[str, object]:
    return {
        "expected_master_count": clocks.expected_master_count,
        "expected_generated_clocks_per_master": clocks.expected_generated_clocks_per_master,
        "master_clocks": tuple(_dump(item) for item in clocks.master_clocks),
        "generated_clocks": tuple(_dump(item) for item in clocks.generated_clocks),
        "lineage_edges": tuple(_dump(item) for item in clocks.lineage_edges),
        "cross_master_synchronous_relationships": tuple(
            _dump(item) for item in clocks.cross_master_synchronous_relationships
        ),
    }


def _cdc_semantics(cdc: CDCInventory) -> dict[str, object]:
    return {
        "approved_pattern_registry_hash": cdc.approved_pattern_registry_hash,
        "crossings": tuple(_dump(item) for item in cdc.crossings),
    }


def _crossing_map(cdc: CDCInventory) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in cdc.crossings:
        dumped = _dump(item)
        if not isinstance(dumped, dict):
            raise ValueError("CDC crossing evidence must be structured")
        result[str(dumped["crossing_id"])] = dumped
    return result


class M4StructuralComparison(StrictContract):
    """Raw identities plus candidate-independent semantic invariant identities."""

    schema_version: Literal[1] = 1
    baseline_binding_hash: HashRef
    candidate_binding_hash: HashRef
    binding_semantic_hash: HashRef
    binding_status: Literal["EQUIVALENT", "APPROVED_SEMANTIC_REMAP", "FORBIDDEN_DELTA"]
    unresolved_constraint_selectors: NonNegativeInt
    unconstrained_endpoints: NonNegativeInt
    baseline_clock_graph_hash: HashRef
    candidate_clock_graph_hash: HashRef
    clock_semantic_hash: HashRef
    clock_status: Literal["UNCHANGED", "CHANGED"]
    baseline_cdc_inventory_hash: HashRef
    candidate_cdc_inventory_hash: HashRef
    cdc_semantic_hash: HashRef
    cdc_status: Literal["UNCHANGED", "CHANGED"]
    new_or_unapproved_cdc_crossings: NonNegativeInt
    changed_approved_cdc_structures: NonNegativeInt
    removed_approved_cdc_structures: NonNegativeInt
    comparison_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.comparison_hash != canonical_sha256(self, exclude=frozenset({"comparison_hash"})):
            raise ValueError("M4 structural comparison hash is not canonical")
        return self

    @property
    def safe(self) -> bool:
        return (
            self.binding_status in {"EQUIVALENT", "APPROVED_SEMANTIC_REMAP"}
            and self.unresolved_constraint_selectors == 0
            and self.unconstrained_endpoints == 0
            and self.clock_status == "UNCHANGED"
            and self.cdc_status == "UNCHANGED"
            and self.new_or_unapproved_cdc_crossings == 0
            and self.changed_approved_cdc_structures == 0
            and self.removed_approved_cdc_structures == 0
        )


def compare_structural_invariants(
    *,
    baseline_binding: ConstraintBindingManifest,
    candidate_binding: ConstraintBindingManifest,
    baseline_clocks: ClockInventory,
    candidate_clocks: ClockInventory,
    baseline_cdc: CDCInventory,
    candidate_cdc: CDCInventory,
) -> M4StructuralComparison:
    """Reconstruct all M4 invariant deltas from the canonical stage contracts."""

    baseline_binding_semantics = canonical_sha256(_binding_semantics(baseline_binding))
    candidate_binding_semantics = canonical_sha256(_binding_semantics(candidate_binding))
    if baseline_binding_semantics != candidate_binding_semantics:
        binding_status = "FORBIDDEN_DELTA"
    elif baseline_binding.effective_binding_hash == candidate_binding.effective_binding_hash:
        binding_status = "EQUIVALENT"
    else:
        binding_status = "APPROVED_SEMANTIC_REMAP"

    baseline_clock_semantics = canonical_sha256(_clock_semantics(baseline_clocks))
    candidate_clock_semantics = canonical_sha256(_clock_semantics(candidate_clocks))
    clock_status = (
        "UNCHANGED" if baseline_clock_semantics == candidate_clock_semantics else "CHANGED"
    )

    baseline_cdc_semantics = canonical_sha256(_cdc_semantics(baseline_cdc))
    candidate_cdc_semantics = canonical_sha256(_cdc_semantics(candidate_cdc))
    cdc_status = "UNCHANGED" if baseline_cdc_semantics == candidate_cdc_semantics else "CHANGED"
    baseline_crossings = _crossing_map(baseline_cdc)
    candidate_crossings = _crossing_map(candidate_cdc)
    new_unapproved = sum(
        item.get("status") != "APPROVED"
        for crossing_id, item in candidate_crossings.items()
        if crossing_id not in baseline_crossings
    )
    changed_approved = sum(
        candidate_crossings[crossing_id] != item
        for crossing_id, item in baseline_crossings.items()
        if crossing_id in candidate_crossings and item.get("status") == "APPROVED"
    )
    removed_approved = sum(
        crossing_id not in candidate_crossings and item.get("status") == "APPROVED"
        for crossing_id, item in baseline_crossings.items()
    )
    coverage = candidate_binding.coverage
    unconstrained = max(
        coverage.sequential_endpoints_total
        - coverage.timed_endpoints
        - coverage.reviewed_exception_endpoints,
        0,
    )
    payload = {
        "schema_version": 1,
        "baseline_binding_hash": baseline_binding.effective_binding_hash,
        "candidate_binding_hash": candidate_binding.effective_binding_hash,
        "binding_semantic_hash": candidate_binding_semantics,
        "binding_status": binding_status,
        "unresolved_constraint_selectors": coverage.unresolved_selectors,
        "unconstrained_endpoints": unconstrained,
        "baseline_clock_graph_hash": baseline_clocks.clock_graph_hash,
        "candidate_clock_graph_hash": candidate_clocks.clock_graph_hash,
        "clock_semantic_hash": candidate_clock_semantics,
        "clock_status": clock_status,
        "baseline_cdc_inventory_hash": baseline_cdc.inventory_hash,
        "candidate_cdc_inventory_hash": candidate_cdc.inventory_hash,
        "cdc_semantic_hash": candidate_cdc_semantics,
        "cdc_status": cdc_status,
        "new_or_unapproved_cdc_crossings": max(
            new_unapproved, candidate_cdc.new_unapproved_count, candidate_cdc.ambiguous_count
        ),
        "changed_approved_cdc_structures": max(
            changed_approved, candidate_cdc.changed_approved_structure_count
        ),
        "removed_approved_cdc_structures": max(
            removed_approved, candidate_cdc.removed_approved_structure_count
        ),
    }
    return M4StructuralComparison(**payload, comparison_hash=canonical_sha256(payload))


__all__ = ["M4StructuralComparison", "compare_structural_invariants"]
