"""Evidence-bound deterministic recovery for completed M5 search outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from nova_rtl.artifacts.store import ArtifactStore, ArtifactStoreError
from nova_rtl.contracts.base import (
    ArtifactRef,
    EvidenceRef,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.recovery import FailureEvent, SearchRecoveryReport
from nova_rtl.optimization.search_flow import (
    M5SearchBundle,
    M5SearchFlowError,
    verify_deterministic_search,
)
from nova_rtl.recovery.compiler import compile_directive
from nova_rtl.recovery.fingerprint import FingerprintEvidence, fingerprint
from nova_rtl.recovery.policy import load_recovery_policy
from nova_rtl.recovery.router import (
    RecoveryBudgets,
    RecoveryHistory,
    route_recovery_envelope,
)
from nova_rtl.transforms.registry import competition_mvp_registry


class SearchRecoveryError(RuntimeError):
    """A completed M5 search cannot produce trusted recovery evidence."""


def _policy_path(repository_root: Path) -> Path:
    return repository_root / "config/policy/recovery_rules.yaml"


def _select_candidate(bundle: M5SearchBundle):  # type: ignore[no-untyped-def]
    if not bundle.valid_negative_candidate_ids:
        raise SearchRecoveryError("search has no valid negative candidate to recover")
    candidate_id = bundle.valid_negative_candidate_ids[0]
    return next(
        item for item in bundle.candidate_dag.candidates if item.candidate_id == candidate_id
    )


def _classify_metric_failure(delta) -> str:  # type: ignore[no-untyped-def]
    metrics = delta.metric_deltas
    if metrics.get("setup_wns_ns", 0.0) < 0.0:
        return "TIMING_REGRESSION"
    if metrics.get("hold_wns_ns", 0.0) < 0.0:
        return "HOLD_REGRESSION"
    baseline_area = delta.baseline_vector.get("physical_area_um2", 0.0)
    area_delta = metrics.get("physical_area_um2", 0.0)
    if baseline_area > 0.0 and 100.0 * area_delta / baseline_area > 0.10:
        return "AREA_POLICY_VIOLATION"
    return "TIMING_NO_GAIN"


def _evidence_bytes(bundle: M5SearchBundle, candidate_id: str) -> bytes:
    delta = bundle.metric_deltas[candidate_id]
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "run_id": bundle.run_id,
            "search_bundle_hash": bundle.bundle_hash,
            "candidate_id": candidate_id,
            "candidate_bundle_hash": bundle.candidate_bundle_hashes[candidate_id],
            "metric_comparison": delta.model_dump(mode="json"),
        }
    )


def _evidence_reference(content: bytes, candidate, comparison_hash: str) -> ArtifactRef:  # type: ignore[no-untyped-def]
    digest = sha256(content).hexdigest()
    return ArtifactRef(
        artifact_id="artifact_recovery_metrics_"
        + comparison_hash.removeprefix("sha256:")[:20],
        uri=f"artifact://sha256/{digest}",
        sha256=f"sha256:{digest}",
        media_type="application/json",
        size_bytes=len(content),
        created_at=candidate.created_at,
        producer_stage_result_id=None,
        classification="INTERNAL",
    )


def _build_report(
    bundle_path: Path,
    bundle: M5SearchBundle,
    *,
    repository_root: Path,
    store: ArtifactStore,
    persist_evidence: bool,
) -> SearchRecoveryReport:
    candidate = _select_candidate(bundle)
    delta = bundle.metric_deltas[candidate.candidate_id]
    family = _classify_metric_failure(delta)
    policy = load_recovery_policy(_policy_path(repository_root))
    rule = policy.rules[family]
    evidence_content = _evidence_bytes(bundle, candidate.candidate_id)
    expected_artifact = _evidence_reference(
        evidence_content, candidate, delta.comparison_hash
    )
    if persist_evidence:
        artifact = store.put_named_bytes(
            evidence_content,
            artifact_id="artifact_recovery_metrics_"
            + delta.comparison_hash.removeprefix("sha256:")[:20],
            media_type="application/json",
            classification="INTERNAL",
            producer_stage_result_id=None,
        ).model_copy(update={"created_at": candidate.created_at})
        if artifact != expected_artifact:
            raise SearchRecoveryError("artifact store returned a changed evidence identity")
    else:
        artifact = expected_artifact
        if store.open_verified(artifact).read() != evidence_content:
            raise SearchRecoveryError("recovery metric evidence differs from M5 comparison")

    evidence = tuple(
        EvidenceRef(
            evidence_id=f"evidence_{kind.lower()}_"
            + delta.comparison_hash.removeprefix("sha256:")[:16],
            kind=kind,
            artifact_id=artifact.artifact_id,
            json_pointer=f"/metric_comparison/{kind.lower()}",
            snapshot_hash=candidate.source_hash,
        )
        for kind in rule.required_evidence_kinds
    )
    failure_id = "failure_search_" + canonical_sha256(
        {
            "bundle": bundle.bundle_hash,
            "candidate": candidate.candidate_id,
            "comparison": delta.comparison_hash,
            "family": family,
        }
    ).removeprefix("sha256:")[:20]
    failure = FailureEvent(
        failure_event_id=failure_id,
        run_id=bundle.run_id,
        subject_type="CANDIDATE",
        subject_id=candidate.candidate_id,
        candidate_id=candidate.candidate_id,
        proposal_id=candidate.proposal_id,
        parent_candidate_id=candidate.parent_candidate_id,
        opportunity_id=candidate.opportunity_id,
        failed_stage="OPENROAD_PHYSICAL",
        analysis_view_id="asap7_setup",
        failure_family=family,
        failure_scope="CANDIDATE",
        repairability=rule.repairability,
        severity=rule.severity,
        retryable=rule.retryable,
        constraint_hash_verified=True,
        analysis_view_hash_verified=True,
        constraint_binding_status="EQUIVALENT",
        protected_structure_status="UNCHANGED",
        metric_delta=dict(sorted(delta.metric_deltas.items())),
        primary_evidence_refs=evidence,
        raw_stage_result_ref="stage_search_metric_comparison",
        classifier_version="m7-search-outcome-v1",
    )
    directive = compile_directive(failure, evidence, policy)
    plan = next(
        item for item in bundle.planned_candidates if item.proposal_id == candidate.proposal_id
    )
    descriptor = competition_mvp_registry().get_descriptor(plan.operation)
    semantic = fingerprint(
        SimpleNamespace(candidate_id=candidate.candidate_id),
        failure,
        FingerprintEvidence(
            target_cone_fingerprint=f"cone:v1:{candidate.opportunity_id}",
            operation_family=descriptor.family,
            operation=plan.operation,
            parameters={},
            ast_delta_tokens=tuple(candidate.changed_spans),
            mapped_delta_tokens=(family.lower(),),
            ancestor_lineage=(candidate.parent_candidate_id,),
            formal_counterexample_fingerprint=None,
            metric_response_class=family,
        ),
    )
    envelope = route_recovery_envelope(
        failure,
        directive,
        RecoveryHistory(current_transform_family=descriptor.family),
        RecoveryBudgets(
            remaining_family_budget=1,
            remaining_lineage_budget=4,
            remaining_token_budget=0,
            remaining_latency_budget_ms=1000,
            deadline=datetime(2100, 1, 1, tzinfo=UTC),
        ),
        policy=policy,
        candidate_failure_fingerprint_ids=(
            semantic.candidate_failure_fingerprint_id,
        ),
    )
    payload = {
        "schema_version": 1,
        "run_id": bundle.run_id,
        "search_bundle_hash": bundle.bundle_hash,
        "candidate_bundle_hash": bundle.candidate_bundle_hashes[candidate.candidate_id],
        "metric_comparison_hash": delta.comparison_hash,
        "source_hash": candidate.source_hash,
        "evidence_artifacts": (artifact,),
        "failure": failure,
        "directive": directive,
        "fingerprint": semantic,
        "route_plan": envelope.route_plan,
        "request": envelope.request,
        "decision": envelope.decision,
        "recovery_outcome": "ROUTED",
        "status": "PASS",
    }
    provisional = SearchRecoveryReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    return SearchRecoveryReport(
        **payload,
        report_hash=canonical_sha256(provisional, exclude=frozenset({"report_hash"})),
    )


def recover_search_bundle(
    bundle_path: Path, *, repository_root: Path
) -> tuple[Path, SearchRecoveryReport]:
    """Classify and route one real valid-negative M5 result without claiming success."""

    root = repository_root.resolve(strict=True)
    path = bundle_path.resolve(strict=True)
    try:
        bundle = verify_deterministic_search(path, repository_root=root)
        run_directory = path.parents[3]
        store = ArtifactStore(run_directory / "artifacts")
        report = _build_report(
            path, bundle, repository_root=root, store=store, persist_evidence=True
        )
        output = path.parent / "recovery" / "search-recovery.json"
        encoded = canonical_json_bytes(report) + b"\n"
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and output.read_bytes() != encoded:
            raise SearchRecoveryError(f"immutable search recovery differs: {output}")
        if not output.exists():
            output.write_bytes(encoded)
        return output, verify_search_recovery(output, repository_root=root)
    except SearchRecoveryError:
        raise
    except (ArtifactStoreError, M5SearchFlowError, OSError, StopIteration, ValueError) as error:
        raise SearchRecoveryError(f"cannot recover M5 search: {error}") from error


def verify_search_recovery(
    report_path: Path, *, repository_root: Path
) -> SearchRecoveryReport:
    """Reconstruct a recovery report from its exact M5 search and artifact bytes."""

    try:
        path = report_path.resolve(strict=True)
        observed = SearchRecoveryReport.model_validate_json(path.read_bytes())
        bundle_path = path.parents[1] / "search-bundle.json"
        bundle = verify_deterministic_search(
            bundle_path, repository_root=repository_root.resolve(strict=True)
        )
        store = ArtifactStore.open_existing(bundle_path.parents[3] / "artifacts")
        for reference in observed.evidence_artifacts:
            store.open_verified(reference).close()
        expected = _build_report(
            bundle_path,
            bundle,
            repository_root=repository_root.resolve(strict=True),
            store=store,
            persist_evidence=False,
        )
    except (ArtifactStoreError, M5SearchFlowError, OSError, StopIteration, ValueError) as error:
        raise SearchRecoveryError(f"invalid search recovery report: {error}") from error
    if observed != expected:
        raise SearchRecoveryError("search recovery differs from reconstructed M5 evidence")
    return observed


__all__ = [
    "SearchRecoveryError",
    "recover_search_bundle",
    "verify_search_recovery",
]
