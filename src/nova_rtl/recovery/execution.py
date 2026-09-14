"""Immutable persistence boundary for deterministic recovery outcomes."""

from __future__ import annotations

from pydantic import model_validator

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import (
    ArtifactRef,
    HashRef,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.recovery import (
    CandidateFailureFingerprint,
    FailureEvent,
    RecoveryDecision,
    RepairDirective,
)
from nova_rtl.contracts.reporting import ExperimentRecord


class RecoveryChain(StrictContract):
    schema_version: int = 1
    failure: FailureEvent
    directive: RepairDirective
    fingerprint: CandidateFailureFingerprint
    decision: RecoveryDecision
    chain_hash: HashRef

    @model_validator(mode="after")
    def links_and_hash_resolve(self):  # type: ignore[no-untyped-def]
        if self.directive.failure_event_id != self.failure.failure_event_id:
            raise ValueError("repair directive does not resolve its failure event")
        if self.fingerprint.candidate_id != self.failure.candidate_id:
            raise ValueError("failure fingerprint does not resolve its candidate")
        if self.decision.failure_event_id != self.failure.failure_event_id:
            raise ValueError("recovery decision does not resolve its failure event")
        if self.chain_hash != canonical_sha256(self, exclude=frozenset({"chain_hash"})):
            raise ValueError("recovery chain hash is not canonical")
        return self


class RecoveryApplication(StrictContract):
    experiment_record: ExperimentRecord
    recovery_decision_id: str
    artifact_refs: tuple[ArtifactRef, ...]


def apply_recovery(
    experiment: ExperimentRecord,
    *,
    failure: FailureEvent,
    directive: RepairDirective,
    fingerprint: CandidateFailureFingerprint,
    decision: RecoveryDecision,
    artifact_store: ArtifactStore,
) -> RecoveryApplication:
    """Bind one recovery authority chain into an experiment before ledger append."""

    payload = {
        "schema_version": 1,
        "failure": failure,
        "directive": directive,
        "fingerprint": fingerprint,
        "decision": decision,
    }
    if directive.failure_event_id != failure.failure_event_id:
        raise ValueError("repair directive does not resolve its failure event")
    if fingerprint.candidate_id != failure.candidate_id:
        raise ValueError("failure fingerprint does not resolve its candidate")
    if decision.failure_event_id != failure.failure_event_id:
        raise ValueError("recovery decision does not resolve its failure event")
    unhashed = RecoveryChain.model_construct(**payload, chain_hash="sha256:" + "0" * 64)
    chain = RecoveryChain.model_construct(
        **payload,
        chain_hash=canonical_sha256(unhashed, exclude=frozenset({"chain_hash"})),
    )
    ref = artifact_store.put_named_bytes(
        canonical_json_bytes(chain),
        artifact_id=f"artifact_recovery_chain_{chain.chain_hash[-16:]}",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    updated = experiment.model_copy(
        update={
            "failure_event_id": failure.failure_event_id,
            "repair_directive_id": directive.repair_directive_id,
            "candidate_failure_fingerprint_id": fingerprint.candidate_failure_fingerprint_id,
            "recovery_decision_id": decision.recovery_decision_id,
            "descendant_outcome": f"RECOVERY_{decision.action}",
        }
    )
    ExperimentRecord.model_validate(updated.model_dump(mode="python"))
    return RecoveryApplication(
        experiment_record=updated,
        recovery_decision_id=decision.recovery_decision_id,
        artifact_refs=(ref,),
    )


__all__ = ["RecoveryApplication", "RecoveryChain", "apply_recovery"]
