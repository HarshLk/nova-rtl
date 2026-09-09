"""Metric-family-specific baseline/candidate identity comparison."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    StageInputHashes,
    StrictContract,
    canonical_sha256,
)

MetricFamily = Literal["TIMING", "POWER"]
_TIMING_IDENTITIES = (
    "platform_lock",
    "tool_recipe",
    "constraints",
    "constraint_binding",
    "analysis_view",
)


class IncomparableResultsError(RuntimeError):
    """Official metrics cannot be compared under their declared identities."""


class ApprovedIdentityRemap(StrictContract):
    """Reviewed equivalence between candidate-specific and semantic identities."""

    identity_name: Literal["constraint_binding"]
    baseline_identity_hash: HashRef
    candidate_identity_hash: HashRef
    semantic_identity_hash: HashRef
    justification: Literal["IDENTICAL_RESOLVED_SELECTORS_AND_ENDPOINT_COVERAGE"]
    remap_hash: HashRef

    @model_validator(mode="after")
    def hash_is_canonical(self) -> Self:
        if self.remap_hash != canonical_sha256(self, exclude=frozenset({"remap_hash"})):
            raise ValueError("semantic identity remap hash is not canonical")
        return self

    @classmethod
    def build(cls, **values: object) -> ApprovedIdentityRemap:
        return cls(**values, remap_hash=canonical_sha256(values))


class ComparabilityResult(StrictContract):
    comparable: Literal[True] = True
    metric_family: MetricFamily
    analysis_view_id: EntityId
    identity_hashes: dict[str, HashRef]
    approved_identity_remaps: tuple[ApprovedIdentityRemap, ...] = ()


def assert_comparable(
    baseline: object,
    candidate: object,
    metric_family: MetricFamily,
    *,
    approved_remaps: tuple[ApprovedIdentityRemap, ...] = (),
) -> ComparabilityResult:
    """Return sealed common identity or raise before metrics can be compared."""

    if metric_family not in {"TIMING", "POWER"}:
        raise ValueError(f"unsupported metric family: {metric_family}")
    baseline_view = getattr(baseline, "analysis_view_id", None)
    candidate_view = getattr(candidate, "analysis_view_id", None)
    if baseline_view is None or baseline_view != candidate_view:
        raise IncomparableResultsError(
            "analysis_view_id is missing or differs between baseline and candidate"
        )
    baseline_hashes = getattr(baseline, "input_hashes", None)
    candidate_hashes = getattr(candidate, "input_hashes", None)
    if not isinstance(baseline_hashes, StageInputHashes) or not isinstance(
        candidate_hashes, StageInputHashes
    ):
        raise IncomparableResultsError("results require typed StageInputHashes identity")
    fields = (
        (*_TIMING_IDENTITIES, "power_activity") if metric_family == "POWER" else _TIMING_IDENTITIES
    )
    common: dict[str, HashRef] = {}
    used_remaps: list[ApprovedIdentityRemap] = []
    for field in fields:
        baseline_value = getattr(baseline_hashes, field)
        candidate_value = getattr(candidate_hashes, field)
        if baseline_value is None or candidate_value is None:
            raise IncomparableResultsError(f"{field} identity is incomplete")
        if baseline_value == candidate_value:
            common[field] = baseline_value
            continue
        remap = next(
            (
                item
                for item in approved_remaps
                if item.identity_name == field
                and item.baseline_identity_hash == baseline_value
                and item.candidate_identity_hash == candidate_value
            ),
            None,
        )
        if remap is None:
            raise IncomparableResultsError(f"{field} identity mismatch")
        common[field] = remap.semantic_identity_hash
        used_remaps.append(remap)
    return ComparabilityResult(
        metric_family=metric_family,
        analysis_view_id=baseline_view,
        identity_hashes=common,
        approved_identity_remaps=tuple(used_remaps),
    )


__all__ = [
    "ApprovedIdentityRemap",
    "ComparabilityResult",
    "IncomparableResultsError",
    "MetricFamily",
    "assert_comparable",
]
