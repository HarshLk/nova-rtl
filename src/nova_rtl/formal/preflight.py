"""Functional/formal compilation identity audit before proof execution."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import StringConstraints, model_validator

from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.base import HashRef, NonEmptyString, StrictContract
from nova_rtl.contracts.verification import FormalModelContract
from nova_rtl.formal.harness import MulticlockHarnessContract

DefineName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]{2,95}$")]


class CompilationDefine(StrictContract):
    name: DefineName
    value: NonEmptyString


class FormalCompilationIdentity(StrictContract):
    """Functional portion and protected fingerprints from one elaboration."""

    functional_rtl_hash: HashRef
    parameter_hash: HashRef
    functional_elaboration_hash: HashRef
    behavior_affecting_defines: tuple[CompilationDefine, ...]
    property_only_defines: tuple[CompilationDefine, ...]
    protected_clock_fingerprint: HashRef
    protected_reset_fingerprint: HashRef
    protected_cdc_fingerprint: HashRef

    @model_validator(mode="after")
    def define_sets_are_canonical(self) -> Self:
        for label, values in (
            ("behavior-affecting", self.behavior_affecting_defines),
            ("property-only", self.property_only_defines),
        ):
            identities = tuple((item.name, item.value) for item in values)
            if identities != tuple(sorted(identities)) or len(identities) != len(
                set(identities)
            ):
                raise ValueError(f"{label} defines must be sorted and unique")
        behavior_names = {item.name for item in self.behavior_affecting_defines}
        property_names = {item.name for item in self.property_only_defines}
        if behavior_names & property_names:
            raise ValueError("behavior and property-only define names must be disjoint")
        return self


def _reject(code: str, message: str) -> None:
    raise SafetyPreflightError(code, message)


def preflight_formal_model(
    *,
    formal_model_contract_id: str,
    candidate_id: str,
    synthesis: FormalCompilationIdentity,
    formal: FormalCompilationIdentity,
    allowed_property_only_defines: tuple[CompilationDefine, ...],
    harness: MulticlockHarnessContract,
    property_manifest_hash: HashRef,
    gold_snapshot_hash: HashRef,
    gate_snapshot_hash: HashRef | None,
) -> FormalModelContract:
    """Reject any functional delta, then emit a proof-ready identity contract."""

    if synthesis.property_only_defines:
        _reject(
            "FORMAL_COMPILATION_PROFILE_INVALID",
            "synthesis compilation cannot contain formal property-only defines",
        )
    if formal.property_only_defines != allowed_property_only_defines:
        _reject(
            "FORMAL_PROPERTY_DEFINE_MISMATCH",
            "formal property-only defines differ from the reviewed allowlist",
        )
    functional_identity = (
        synthesis.functional_rtl_hash,
        synthesis.parameter_hash,
        synthesis.functional_elaboration_hash,
        synthesis.behavior_affecting_defines,
    )
    formal_identity = (
        formal.functional_rtl_hash,
        formal.parameter_hash,
        formal.functional_elaboration_hash,
        formal.behavior_affecting_defines,
    )
    if formal_identity != functional_identity:
        _reject(
            "FORMAL_BEHAVIOR_IDENTITY_CHANGED",
            "formal compilation changes functional sources, parameters, defines, or elaboration",
        )
    protected_identity = (
        synthesis.protected_clock_fingerprint,
        synthesis.protected_reset_fingerprint,
        synthesis.protected_cdc_fingerprint,
    )
    if protected_identity != (
        formal.protected_clock_fingerprint,
        formal.protected_reset_fingerprint,
        formal.protected_cdc_fingerprint,
    ):
        _reject(
            "FORMAL_PROTECTED_STRUCTURE_CHANGED",
            "formal compilation changes protected clock, reset, or CDC structure",
        )
    if synthesis.protected_clock_fingerprint != harness.generated_clock_graph_hash:
        _reject(
            "FORMAL_CLOCK_MODEL_MISMATCH",
            "multiclock harness is not bound to the protected clock graph",
        )

    return FormalModelContract(
        formal_model_contract_id=formal_model_contract_id,
        candidate_id=candidate_id,
        functional_rtl_hash=synthesis.functional_rtl_hash,
        parameter_hash=synthesis.parameter_hash,
        gold_snapshot_hash=gold_snapshot_hash,
        gate_snapshot_hash=gate_snapshot_hash,
        property_manifest_hash=property_manifest_hash,
        master_clock_model="INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
        generated_clock_model="DERIVED_FROM_PROTECTED_DIVIDER_STATE",
        multiclock_enabled=True,
        reset_assumption_hash=harness.reset_assumption_hash,
        environment_assumption_hash=harness.environment_assumption_hash,
        proof_scope_policy="WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
        behavioral_elaboration_delta="NONE",
    )


__all__ = [
    "CompilationDefine",
    "FormalCompilationIdentity",
    "preflight_formal_model",
]
