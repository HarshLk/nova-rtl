from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import StrictBool

from nova_rtl.contracts.base import StrictContract
from nova_rtl.transforms.registry import (
    TransformCapabilityMetadata,
    TransformRegistry,
    TransformRegistryError,
)


class Parameters(StrictContract):
    preserve_priority: StrictBool


class Capability:
    parameter_model: ClassVar[type[StrictContract]] = Parameters

    def __init__(
        self, operation: str, version: str = "transform:v1:priority_mux"
    ) -> None:
        self.metadata = TransformCapabilityMetadata(
            operation=operation,
            family="LOGIC_RESTRUCTURING",
            correctness_contract="STRICT_SEQ_EQUIV",
            implementation_version=version,
            allowed_ast_node_kinds=("ConditionalStatement",),
            prohibited_contexts=("CDC_ADJACENT", "CLOCK_LOGIC", "RESET_LOGIC"),
            expected_structural_effects=("REDUCE_PRIORITY_DEPTH",),
            conflicts=(),
        )


def test_registry_resolves_exact_capability_and_validates_parameters() -> None:
    capability = Capability("RESTRUCTURE_PRIORITY_MUX")
    registry = TransformRegistry((capability,))

    assert registry.operations == ("RESTRUCTURE_PRIORITY_MUX",)
    assert registry.resolve("RESTRUCTURE_PRIORITY_MUX") is capability
    assert registry.validate_parameters(
        "RESTRUCTURE_PRIORITY_MUX", {"preserve_priority": True}
    ) == Parameters(preserve_priority=True)

    with pytest.raises(ValueError):
        registry.validate_parameters(
            "RESTRUCTURE_PRIORITY_MUX",
            {"preserve_priority": True, "unreviewed": True},
        )


def test_registry_rejects_unknown_and_duplicate_operations() -> None:
    with pytest.raises(TransformRegistryError, match="duplicate transform operation"):
        TransformRegistry(
            (
                Capability("RESTRUCTURE_PRIORITY_MUX"),
                Capability("RESTRUCTURE_PRIORITY_MUX", "transform:v2:priority_mux"),
            )
        )

    registry = TransformRegistry((Capability("RESTRUCTURE_PRIORITY_MUX"),))
    with pytest.raises(TransformRegistryError, match="not registered"):
        registry.resolve("BALANCE_BOOLEAN_TREE")


def test_registry_hash_is_order_independent_and_metadata_sensitive() -> None:
    priority = Capability("RESTRUCTURE_PRIORITY_MUX")
    boolean = Capability("BALANCE_BOOLEAN_TREE", "transform:v1:boolean_tree")

    first = TransformRegistry((priority, boolean))
    reordered = TransformRegistry((boolean, priority))
    changed = TransformRegistry(
        (priority, Capability("BALANCE_BOOLEAN_TREE", "transform:v2:boolean_tree"))
    )

    assert first.operations == ("BALANCE_BOOLEAN_TREE", "RESTRUCTURE_PRIORITY_MUX")
    assert first.registry_hash == reordered.registry_hash
    assert first.registry_hash != changed.registry_hash
