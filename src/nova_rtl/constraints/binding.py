"""Canonical SDC selector binding and endpoint-coverage audit."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from nova_rtl.contracts.base import EntityId, HashRef, StrictContract, canonical_sha256
from nova_rtl.contracts.verification import (
    ConstraintBindingManifest,
    ConstraintCoverage,
    ResolvedConstraintCommand,
)

ObjectId = Annotated[str, StringConstraints(min_length=1)]


class SafetyPreflightError(RuntimeError):
    """A cheap safety audit rejected inputs before expensive analysis."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConstraintCommandResolution(StrictContract):
    """Raw resolver output for one normalized SDC command."""

    command_id: EntityId
    normalized_command: ObjectId
    selectors: tuple[ObjectId, ...] = Field(min_length=1)
    resolved_object_ids: tuple[ObjectId, ...]


def audit_constraint_binding(
    *,
    binding_manifest_id: str,
    candidate_id: str,
    sdc_hash: HashRef,
    netlist_snapshot_hash: HashRef,
    analysis_view_hashes: dict[str, HashRef],
    expected_analysis_view_hashes: dict[str, HashRef],
    expected_command_ids: tuple[str, ...],
    command_resolutions: tuple[ConstraintCommandResolution, ...],
    sequential_endpoint_ids: tuple[str, ...],
    timed_endpoint_ids: tuple[str, ...],
    reviewed_exception_endpoint_ids: tuple[str, ...],
    baseline: ConstraintBindingManifest | None,
) -> ConstraintBindingManifest:
    """Resolve canonical bindings and reject identity or coverage drift."""

    if not analysis_view_hashes or analysis_view_hashes != expected_analysis_view_hashes:
        raise SafetyPreflightError(
            "ANALYSIS_VIEW_HASH_MISMATCH",
            "required analysis-view identities are missing or differ from the locked set",
        )
    command_ids = tuple(item.command_id for item in command_resolutions)
    if (
        not expected_command_ids
        or expected_command_ids != tuple(sorted(expected_command_ids))
        or len(expected_command_ids) != len(set(expected_command_ids))
    ):
        raise SafetyPreflightError(
            "CONSTRAINT_COMMAND_IDENTITY_INVALID",
            "expected constraint command identities must be sorted and unique",
        )
    if len(command_ids) != len(set(command_ids)):
        raise SafetyPreflightError(
            "CONSTRAINT_COMMAND_IDENTITY_INVALID",
            "resolved constraint command identities must be unique",
        )
    if set(command_ids) != set(expected_command_ids):
        raise SafetyPreflightError(
            "CONSTRAINT_COMMAND_INVENTORY_INCOMPLETE",
            "resolved command inventory must exactly cover every expected SDC command",
        )
    empty_commands = tuple(
        item.command_id for item in command_resolutions if not item.resolved_object_ids
    )
    if empty_commands:
        raise SafetyPreflightError(
            "CONSTRAINT_SELECTOR_UNRESOLVED",
            f"constraint selectors resolved to empty object sets: {', '.join(empty_commands)}",
        )

    sequential = set(sequential_endpoint_ids)
    timed = set(timed_endpoint_ids)
    excepted = set(reviewed_exception_endpoint_ids)
    duplicated_inputs = any(
        len(items) != len(set(items))
        for items in (
            sequential_endpoint_ids,
            timed_endpoint_ids,
            reviewed_exception_endpoint_ids,
        )
    )
    if (
        duplicated_inputs
        or timed & excepted
        or not timed <= sequential
        or not excepted <= sequential
        or timed | excepted != sequential
    ):
        raise SafetyPreflightError(
            "CONSTRAINT_ENDPOINT_COVERAGE_INCOMPLETE",
            "timed and reviewed-exception endpoints must disjointly cover every endpoint",
        )

    resolved_commands = tuple(
        ResolvedConstraintCommand(
            command_id=item.command_id,
            normalized_command_hash=canonical_sha256(
                {"normalized_command": item.normalized_command}
            ),
            resolved_object_ids=tuple(sorted(set(item.resolved_object_ids))),
            resolved_object_set_hash=canonical_sha256(
                {"resolved_object_ids": sorted(set(item.resolved_object_ids))}
            ),
        )
        for item in sorted(command_resolutions, key=lambda command: command.command_id)
    )
    coverage = ConstraintCoverage(
        sequential_endpoints_total=len(sequential),
        timed_endpoints=len(timed),
        reviewed_exception_endpoints=len(excepted),
        unresolved_selectors=0,
    )
    if baseline is not None:
        baseline_bindings = tuple(
            (
                item.command_id,
                item.normalized_command_hash,
                item.resolved_object_set_hash,
                item.resolved_object_ids,
            )
            for item in baseline.resolved_commands
        )
        candidate_bindings = tuple(
            (
                item.command_id,
                item.normalized_command_hash,
                item.resolved_object_set_hash,
                item.resolved_object_ids,
            )
            for item in resolved_commands
        )
        if (
            baseline.sdc_hash != sdc_hash
            or baseline.analysis_view_ids != tuple(sorted(analysis_view_hashes))
            or baseline_bindings != candidate_bindings
            or baseline.coverage != coverage
        ):
            raise SafetyPreflightError(
                "CONSTRAINT_BINDING_FORBIDDEN_DELTA",
                "candidate selector binding differs from the locked baseline intent",
            )

    payload = {
        "schema_version": 1,
        "binding_manifest_id": binding_manifest_id,
        "candidate_id": candidate_id,
        "sdc_hash": sdc_hash,
        "netlist_snapshot_hash": netlist_snapshot_hash,
        "analysis_view_ids": tuple(sorted(analysis_view_hashes)),
        "resolved_commands": tuple(
            item.model_dump(mode="json") for item in resolved_commands
        ),
        "coverage": coverage.model_dump(mode="json"),
        "comparison_to_baseline": "EQUIVALENT",
        "reviewed_mapping_refs": (),
    }
    return ConstraintBindingManifest(
        **payload,
        effective_binding_hash=canonical_sha256(payload),
    )


__all__ = [
    "ConstraintCommandResolution",
    "SafetyPreflightError",
    "audit_constraint_binding",
]
