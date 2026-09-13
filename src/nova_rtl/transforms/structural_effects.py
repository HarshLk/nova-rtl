"""Fail-closed validation of a transform's declared structural effect."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import HashRef, NonNegativeInt, StrictContract, canonical_sha256
from nova_rtl.contracts.optimization import StableUpperString, TransformOperation
from nova_rtl.transforms.registry import TransformCapabilityMetadata


class StructuralEffectError(ValueError):
    """A candidate did not exhibit every structural change it declared."""


class StructuralEffectObservation(StrictContract):
    """Comparable before/after counters produced by structural analysis."""

    schema_version: Literal[1] = 1
    baseline_depth: NonNegativeInt
    candidate_depth: NonNegativeInt
    baseline_duplicate_expressions: NonNegativeInt
    candidate_duplicate_expressions: NonNegativeInt
    baseline_decode_levels: NonNegativeInt
    candidate_decode_levels: NonNegativeInt


class StructuralEffectResult(StrictContract):
    """Canonical proof that all advertised structural effects were observed."""

    schema_version: Literal[1] = 1
    operation: TransformOperation
    expected_structural_effects: tuple[StableUpperString, ...]
    observation: StructuralEffectObservation
    status: Literal["PASS"] = "PASS"
    result_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        expected = canonical_sha256(self, exclude=frozenset({"result_hash"}))
        if self.result_hash != expected:
            raise ValueError("structural effect result hash is not canonical")
        return self


def _effect_passes(effect: str, observation: StructuralEffectObservation) -> bool:
    if effect in {"BALANCED_PREDECODE", "REDUCE_PRIORITY_DEPTH", "REDUCE_BOOLEAN_DEPTH"}:
        return observation.candidate_depth < observation.baseline_depth
    if effect == "REDUCE_DUPLICATE_PREDICATE":
        return (
            observation.candidate_duplicate_expressions
            < observation.baseline_duplicate_expressions
        )
    if effect == "REDUCE_FSM_DECODE_LEVELS":
        return observation.candidate_decode_levels < observation.baseline_decode_levels
    raise StructuralEffectError(f"unsupported structural effect: {effect}")


def validate_structural_effect(
    descriptor: TransformCapabilityMetadata,
    observation: StructuralEffectObservation,
) -> StructuralEffectResult:
    """Require measured progress for every effect declared by a capability."""

    for effect in descriptor.expected_structural_effects:
        if not _effect_passes(effect, observation):
            raise StructuralEffectError(
                f"declared structural effect was not observed: {effect}"
            )
    payload = {
        "schema_version": 1,
        "operation": descriptor.operation,
        "expected_structural_effects": descriptor.expected_structural_effects,
        "observation": observation.model_dump(mode="json"),
        "status": "PASS",
    }
    return StructuralEffectResult(**payload, result_hash=canonical_sha256(payload))


__all__ = [
    "StructuralEffectError",
    "StructuralEffectObservation",
    "StructuralEffectResult",
    "validate_structural_effect",
]
