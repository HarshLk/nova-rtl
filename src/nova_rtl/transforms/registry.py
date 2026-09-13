"""Versioned transform capability metadata and deterministic resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from nova_rtl.contracts.base import NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.contracts.manifest import CorrectnessContract, JsonScalar
from nova_rtl.contracts.optimization import (
    Fingerprint,
    StableUpperString,
    TransformFamily,
    TransformOperation,
)


class TransformRegistryError(ValueError):
    """A requested capability is absent or the registry is ambiguous."""


class TransformCapabilityMetadata(StrictContract):
    """Stable policy-facing description of one executable transform."""

    schema_version: Literal[1] = 1
    operation: TransformOperation
    family: TransformFamily
    correctness_contract: CorrectnessContract
    implementation_version: Fingerprint
    allowed_ast_node_kinds: tuple[NonEmptyString, ...] = Field(min_length=1)
    prohibited_contexts: tuple[StableUpperString, ...] = Field(min_length=1)
    expected_structural_effects: tuple[StableUpperString, ...] = Field(min_length=1)
    conflicts: tuple[TransformOperation, ...]

    @field_validator(
        "allowed_ast_node_kinds",
        "prohibited_contexts",
        "expected_structural_effects",
        "conflicts",
    )
    @classmethod
    def sequences_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("capability metadata sequences must be unique")
        return value


@runtime_checkable
class TransformCapability(Protocol):
    """The registry-visible portion of a deterministic RTL capability."""

    metadata: TransformCapabilityMetadata
    parameter_model: type[BaseModel]

    def match(self, context: BaseModel) -> BaseModel: ...

    def preflight(self, match: BaseModel, parameters: BaseModel) -> None: ...

    def rewrite(self, match: BaseModel, parameters: BaseModel): ...  # type: ignore[no-untyped-def]

    def fingerprint(self, match: BaseModel, parameters: BaseModel) -> str: ...


class TransformRegistry:
    """Immutable exact-operation registry with a canonical content identity."""

    def __init__(self, capabilities: Sequence[TransformCapability]) -> None:
        by_operation: dict[str, TransformCapability] = {}
        for capability in capabilities:
            operation = capability.metadata.operation
            if operation in by_operation:
                raise TransformRegistryError(f"duplicate transform operation: {operation}")
            by_operation[operation] = capability
        self._capabilities: Mapping[str, TransformCapability] = MappingProxyType(
            dict(sorted(by_operation.items()))
        )
        self._registry_hash = canonical_sha256(
            {
                "schema_version": 1,
                "capabilities": tuple(
                    {
                        "metadata": capability.metadata.model_dump(mode="json"),
                        "parameter_schema_hash": canonical_sha256(
                            capability.parameter_model.model_json_schema()
                        ),
                    }
                    for capability in self._capabilities.values()
                ),
            }
        )

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(self._capabilities)

    @property
    def registry_hash(self) -> str:
        return self._registry_hash

    def resolve(self, operation: str) -> TransformCapability:
        try:
            return self._capabilities[operation]
        except KeyError as error:
            raise TransformRegistryError(
                f"transform operation is not registered: {operation}"
            ) from error

    def get_descriptor(self, operation: str) -> TransformCapabilityMetadata:
        """Return stable policy-facing metadata without exposing implementation state."""

        return self.resolve(operation).metadata

    def validate_parameters(
        self, operation: str, parameters: Mapping[str, JsonScalar]
    ) -> BaseModel:
        return self.resolve(operation).parameter_model.model_validate(dict(parameters))


__all__ = [
    "TransformCapability",
    "TransformCapabilityMetadata",
    "TransformRegistry",
    "TransformRegistryError",
]
