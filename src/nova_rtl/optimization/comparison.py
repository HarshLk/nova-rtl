"""Independent semantic comparison of parent and candidate safety evidence."""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import HashRef, NonNegativeInt, StrictContract, canonical_sha256
from nova_rtl.contracts.optimization import MappedStructuralEffect
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


def _module_shape(module: dict[str, object]) -> tuple[int, int]:
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ValueError("mapped module cells must be an object")
    drivers: dict[int, str] = {}
    inputs: dict[str, set[int]] = {}
    for cell_name, raw_cell in cells.items():
        if not isinstance(cell_name, str) or not isinstance(raw_cell, dict):
            raise ValueError("mapped cell entry is malformed")
        directions = raw_cell.get("port_directions", {})
        connections = raw_cell.get("connections", {})
        if not isinstance(directions, dict) or not isinstance(connections, dict):
            raise ValueError("mapped cell ports are malformed")
        for port, raw_bits in connections.items():
            if not isinstance(raw_bits, list):
                raise ValueError("mapped cell connection must be a bit list")
            for bit in raw_bits:
                if not isinstance(bit, int):
                    continue
                if directions.get(port) == "output":
                    drivers[bit] = cell_name
                elif directions.get(port) == "input":
                    inputs.setdefault(cell_name, set()).add(bit)
    memo: dict[str, int] = {}

    def depth(cell_name: str, active: frozenset[str] = frozenset()) -> int:
        if cell_name in memo:
            return memo[cell_name]
        if cell_name in active:
            return 0
        value = 1 + max(
            (
                depth(drivers[bit], active | {cell_name})
                for bit in inputs.get(cell_name, set())
                if bit in drivers
            ),
            default=0,
        )
        memo[cell_name] = value
        return value

    return len(cells), max((depth(name) for name in cells), default=0)


def compare_mapped_structure(
    *,
    baseline_netlist: bytes,
    candidate_netlist: bytes,
    baseline_artifact_hash: str,
    candidate_artifact_hash: str,
    target_module: str,
) -> MappedStructuralEffect:
    """Compare the mapped target family without trusting source-level intent."""

    try:
        baseline = json.loads(baseline_netlist)
        candidate = json.loads(candidate_netlist)
        baseline_modules = baseline["modules"]
        candidate_modules = candidate["modules"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("mapped design JSON is malformed") from error
    names = tuple(
        sorted(
            name
            for name in baseline_modules
            if target_module in name and name in candidate_modules
        )
    )
    if not names or {
        name for name in baseline_modules if target_module in name
    } != {name for name in candidate_modules if target_module in name}:
        raise ValueError("mapped target module inventory changed")
    baseline_shapes = {name: _module_shape(baseline_modules[name]) for name in names}
    candidate_shapes = {name: _module_shape(candidate_modules[name]) for name in names}
    baseline_cells = {name: value[0] for name, value in baseline_shapes.items()}
    candidate_cells = {name: value[0] for name, value in candidate_shapes.items()}
    baseline_depths = {name: value[1] for name, value in baseline_shapes.items()}
    candidate_depths = {name: value[1] for name, value in candidate_shapes.items()}
    reduced = sum(candidate_depths[name] < baseline_depths[name] for name in names)
    unchanged = baseline_cells == candidate_cells and baseline_depths == candidate_depths
    status = (
        "DEPTH_REDUCED"
        if reduced
        else "NO_MAPPED_CHANGE"
        if unchanged
        else "CHANGED_NO_DEPTH_REDUCTION"
    )
    payload = {
        "schema_version": 1,
        "baseline_artifact_hash": baseline_artifact_hash,
        "candidate_artifact_hash": candidate_artifact_hash,
        "target_module": target_module,
        "module_names": names,
        "baseline_cell_counts": baseline_cells,
        "candidate_cell_counts": candidate_cells,
        "baseline_max_depths": baseline_depths,
        "candidate_max_depths": candidate_depths,
        "baseline_total_cells": sum(baseline_cells.values()),
        "candidate_total_cells": sum(candidate_cells.values()),
        "cell_delta": sum(candidate_cells.values()) - sum(baseline_cells.values()),
        "reduced_depth_instance_count": reduced,
        "status": status,
    }
    return MappedStructuralEffect(**payload, effect_hash=canonical_sha256(payload))


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


__all__ = [
    "M4StructuralComparison",
    "MappedStructuralEffect",
    "compare_mapped_structure",
    "compare_structural_invariants",
]
