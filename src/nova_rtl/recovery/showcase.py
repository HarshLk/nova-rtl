"""Deterministic M7 path-migration recovery showcase and inspection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Self

from pydantic import model_validator

from nova_rtl.contracts.base import (
    EvidenceRef,
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
from nova_rtl.recovery.compiler import compile_directive
from nova_rtl.recovery.fingerprint import FingerprintEvidence, fingerprint
from nova_rtl.recovery.policy import load_recovery_policy
from nova_rtl.recovery.router import RecoveryBudgets, RecoveryHistory, route_recovery


class PathMigrationRecoveryReport(StrictContract):
    schema_version: Literal[1] = 1
    run_id: str
    search_bundle_hash: HashRef
    source_hash: HashRef
    policy_hash: HashRef
    failure: FailureEvent
    directive: RepairDirective
    fingerprint: CandidateFailureFingerprint
    decision: RecoveryDecision
    next_target_cone_fingerprint: str
    next_operation_family: str
    status: Literal["PASS"] = "PASS"
    report_hash: HashRef

    @model_validator(mode="after")
    def recovery_is_distinct_and_self_hashed(self) -> Self:
        if self.failure.failure_family != "CRITICAL_PATH_MIGRATION":
            raise ValueError("showcase must classify critical-path migration")
        if self.decision.action not in {"OPPORTUNITY_REANALYSIS", "TARGET_NEW_PATH_CLUSTER"}:
            raise ValueError("showcase must route opportunity reanalysis")
        if self.next_target_cone_fingerprint == self.fingerprint.target_cone_fingerprint:
            raise ValueError("path migration must select a distinct target cone")
        if self.next_operation_family == self.fingerprint.operation_family:
            raise ValueError("path migration must avoid identical mechanism repetition")
        if self.report_hash != canonical_sha256(self, exclude=frozenset({"report_hash"})):
            raise ValueError("path-migration report hash is not canonical")
        return self


class PathMigrationRecoveryError(RuntimeError):
    pass


def _policy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config/policy/recovery_rules.yaml"


def create_path_migration_showcase(
    *,
    output_directory: Path,
    run_id: str,
    candidate_id: str,
    parent_candidate_id: str,
    source_hash: str,
    search_bundle_hash: str,
) -> tuple[Path, PathMigrationRecoveryReport]:
    """Create a reproducible sibling-path recovery decision without model reasoning."""

    policy = load_recovery_policy(_policy_path())
    path_evidence = EvidenceRef(
        evidence_id="evidence_path_migration",
        kind="PATH",
        artifact_id="artifact_search_path_evidence",
        json_pointer="/critical_paths/1",
        snapshot_hash=source_hash,
    )
    metric_evidence = EvidenceRef(
        evidence_id="evidence_path_migration_metrics",
        kind="METRIC",
        artifact_id="artifact_search_metric_evidence",
        json_pointer="/metric_deltas/setup_wns_ns",
        snapshot_hash=source_hash,
    )
    rule = policy.rules["CRITICAL_PATH_MIGRATION"]
    failure_id = "failure_path_migration_" + canonical_sha256(
        {"run_id": run_id, "candidate_id": candidate_id, "source_hash": source_hash}
    )[-16:]
    failure = FailureEvent(
        failure_event_id=failure_id,
        run_id=run_id,
        subject_type="CANDIDATE",
        subject_id=candidate_id,
        candidate_id=candidate_id,
        proposal_id="proposal_local_improvement",
        parent_candidate_id=parent_candidate_id,
        opportunity_id="opportunity_original_path",
        failed_stage="OPENSTA_FULL",
        analysis_view_id="asap7_setup",
        failure_family="CRITICAL_PATH_MIGRATION",
        failure_scope="CANDIDATE",
        repairability=rule.repairability,
        severity=rule.severity,
        retryable=rule.retryable,
        constraint_hash_verified=True,
        analysis_view_hash_verified=True,
        constraint_binding_status="EQUIVALENT",
        protected_structure_status="UNCHANGED",
        metric_delta={"target_wns_improvement_ns": 0.02, "sibling_wns_delta_ns": -0.01},
        primary_evidence_refs=(path_evidence, metric_evidence),
        raw_stage_result_ref="stage_opensta_path_migration",
        classifier_version="m7-classifier-v1",
    )
    directive = compile_directive(
        failure, (path_evidence, metric_evidence), policy
    )
    semantic = fingerprint(
        SimpleNamespace(candidate_id=candidate_id),
        failure,
        FingerprintEvidence(
            target_cone_fingerprint="cone:v1:original_critical_path",
            operation_family="LOGIC_RESTRUCTURE",
            operation="RESTRUCTURE_PRIORITY_MUX",
            parameters={"fan_in": 8},
            ast_delta_tokens=("priority_chain", "balanced_mux"),
            mapped_delta_tokens=("mux_depth_reduced",),
            ancestor_lineage=(parent_candidate_id,),
            formal_counterexample_fingerprint=None,
            metric_response_class="PATH_MIGRATED",
        ),
    )
    decision = route_recovery(
        failure,
        directive,
        RecoveryHistory(current_transform_family="LOGIC_RESTRUCTURE"),
        RecoveryBudgets(
            remaining_family_budget=1,
            remaining_lineage_budget=4,
            remaining_token_budget=0,
            remaining_latency_budget_ms=1000,
            deadline=datetime(2100, 1, 1, tzinfo=UTC),
        ),
        policy=policy,
    )
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "search_bundle_hash": search_bundle_hash,
        "source_hash": source_hash,
        "policy_hash": policy.policy_hash,
        "failure": failure,
        "directive": directive,
        "fingerprint": semantic,
        "decision": decision,
        "next_target_cone_fingerprint": "cone:v1:sibling_critical_path",
        "next_operation_family": "FANOUT_LOCALIZATION",
        "status": "PASS",
    }
    unhashed = PathMigrationRecoveryReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    report = PathMigrationRecoveryReport(
        **payload,
        report_hash=canonical_sha256(unhashed, exclude=frozenset({"report_hash"})),
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / "path-migration-recovery.json"
    encoded = canonical_json_bytes(report) + b"\n"
    if path.exists() and path.read_bytes() != encoded:
        raise PathMigrationRecoveryError(f"immutable recovery report differs: {path}")
    if not path.exists():
        path.write_bytes(encoded)
    return path, report


def verify_path_migration_showcase(path: Path) -> PathMigrationRecoveryReport:
    try:
        report = PathMigrationRecoveryReport.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise PathMigrationRecoveryError(f"invalid path-migration report: {error}") from error
    policy = load_recovery_policy(_policy_path())
    if report.policy_hash != policy.policy_hash:
        raise PathMigrationRecoveryError("path-migration report policy identity differs")
    return report


def inspect_failure(failure_id: str, runs_root: Path) -> tuple[Path, PathMigrationRecoveryReport]:
    matches: list[tuple[Path, PathMigrationRecoveryReport]] = []
    for path in sorted(runs_root.resolve().glob("**/path-migration-recovery.json")):
        report = verify_path_migration_showcase(path)
        if report.failure.failure_event_id == failure_id:
            matches.append((path, report))
    if len(matches) != 1:
        raise PathMigrationRecoveryError(
            f"failure ID must resolve to exactly one verified report: {failure_id}"
        )
    return matches[0]


__all__ = [
    "PathMigrationRecoveryError",
    "PathMigrationRecoveryReport",
    "create_path_migration_showcase",
    "inspect_failure",
    "verify_path_migration_showcase",
]
