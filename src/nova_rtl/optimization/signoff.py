"""Fail-closed M4 candidate and strict-equivalence milestone sign-off."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_digest, replay_run
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.flow import load_run_index
from nova_rtl.contracts.analysis import M3SignoffReport
from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.execution import StageResult
from nova_rtl.contracts.optimization import M4SignoffReport, M4ViewComparison
from nova_rtl.evidence.signoff import M3SignoffError, verify_m3_dependency_snapshot
from nova_rtl.optimization.flow import (
    M4CandidateBundle,
    OptimizationFlowError,
    verify_candidate_bundle,
)
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest


class M4SignoffError(OptimizationFlowError):
    """The M4 candidate packet cannot be reconstructed or trusted."""


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M4SignoffError("Git is required for M4 sign-off")
    completed = subprocess.run(
        (str(Path(executable).resolve()), "-C", str(repository_root), *arguments),
        env={
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": f"{Path(executable).resolve().parent}:/usr/bin:/bin",
        },
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise M4SignoffError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _require_git_ancestor(
    repository_root: Path, ancestor_commit: str, descendant_commit: str
) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise M4SignoffError("Git is required for M4 dependency verification")
    completed = subprocess.run(
        (
            str(Path(executable).resolve()),
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            ancestor_commit,
            descendant_commit,
        ),
        env={
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": f"{Path(executable).resolve().parent}:/usr/bin:/bin",
        },
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise M4SignoffError(
            f"recorded M4 commit {ancestor_commit} is not an ancestor of "
            f"{descendant_commit}"
        )


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    if Path(__file__).resolve(strict=True) != root / "src/nova_rtl/optimization/signoff.py":
        raise M4SignoffError("loaded NOVA package is outside the requested repository")
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise M4SignoffError("requested NOVA repository is not the Git repository root")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M4SignoffError("M4 sign-off requires a clean final commit")
    commit = _git_output(root, "rev-parse", "--verify", "HEAD")
    tree = _git_output(root, "rev-parse", "--verify", "HEAD^{tree}")
    return commit, tree


def _m3_dependency(
    path: Path, repository_root: Path, current_commit: str
) -> tuple[M3SignoffReport, str]:
    try:
        return verify_m3_dependency_snapshot(
            path,
            repository_root=repository_root,
            descendant_commit=current_commit,
        )
    except (M3SignoffError, OSError, ValueError) as error:
        raise M4SignoffError("M3 dependency reconstruction failed") from error


def _stage_result(
    run_directory: Path, stage_id: str, reference
) -> StageResult:  # type: ignore[no-untyped-def]
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    try:
        result = StageResult.model_validate_json(store.open_verified(reference).read())
        for raw in result.raw_artifacts:
            store.open_verified(raw).close()
    except (OSError, ValueError) as error:
        raise M4SignoffError(f"invalid M4 stage evidence: {stage_id}") from error
    if result.stage_result_id != stage_id or result.status != "PASS":
        raise M4SignoffError(f"M4 requires passing stage evidence: {stage_id}")
    return result


def _run_replay_identity(run_directory: Path, run_id: str) -> tuple[str, str]:
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    ledger_path = run_directory / "experiment-ledger.sqlite3"
    ledger = ExperimentLedger.open_existing(ledger_path, artifact_store=store)
    return replay_digest(replay_run(ledger, run_id)), _hash_bytes(ledger_path.read_bytes())


def _build_report(
    bundle_path: Path,
    bundle: M4CandidateBundle,
    m3_report: M3SignoffReport,
    m3_packet_hash: str,
    *,
    repository_root: Path,
    commit_sha: str,
    implementation_tree_hash: str,
    frozen_toolchain_receipt_hash: str | None = None,
) -> M4SignoffReport:
    parent_run = bundle_path.resolve().parents[3]
    parent_index = load_run_index(parent_run)
    candidate_run = parent_run / "m4" / "candidate-runs" / bundle.candidate_run_id
    candidate_index = load_run_index(candidate_run)
    if parent_index.candidate_id != "baseline":
        raise M4SignoffError("M4 primary candidate must descend from the baseline")
    if not {"stage_evidence_graph", "stage_opportunity_formation"}.issubset(
        parent_index.stage_result_artifacts
    ):
        raise M4SignoffError("M4 parent lacks the complete M3 evidence boundary")
    if candidate_index.candidate_id != bundle.candidate.candidate_id:
        raise M4SignoffError("M4 candidate run identity is inconsistent")
    if parent_index.platform_lock_hash != candidate_index.platform_lock_hash:
        raise M4SignoffError("candidate platform lock differs from its baseline")
    if tuple(parent_index.analysis_views) != tuple(candidate_index.analysis_views):
        raise M4SignoffError("candidate analysis views differ from its baseline")
    if (
        parent_index.constraint_contract_artifact.sha256
        != candidate_index.constraint_contract_artifact.sha256
    ):
        raise M4SignoffError("candidate constraint contract differs from its baseline")

    comparisons: dict[str, M4ViewComparison] = {}
    for view in sorted(candidate_index.analysis_views, key=lambda item: item.analysis_view_id):
        stage_id = f"stage_openroad_{view.analysis_view_id}"
        parent_ref = parent_index.stage_result_artifacts.get(stage_id)
        candidate_ref = candidate_index.stage_result_artifacts.get(stage_id)
        if parent_ref is None or candidate_ref is None:
            raise M4SignoffError(f"missing comparable physical view: {view.analysis_view_id}")
        baseline = _stage_result(parent_run, stage_id, parent_ref)
        candidate = _stage_result(candidate_run, stage_id, candidate_ref)
        comparisons[view.analysis_view_id] = M4ViewComparison(
            analysis_view_id=view.analysis_view_id,
            baseline_stage_result_hash=parent_ref.sha256,
            candidate_stage_result_hash=candidate_ref.sha256,
            baseline_metrics=baseline.metrics,
            candidate_metrics=candidate.metrics,
        )

    baseline_replay, baseline_ledger = _run_replay_identity(
        parent_run, parent_index.run_id
    )
    candidate_replay, candidate_ledger = _run_replay_identity(
        candidate_run, candidate_index.run_id
    )
    if frozen_toolchain_receipt_hash is None:
        manifest = load_toolchain_source_manifest(
            repository_root / "config/platform/toolchain-sources.json"
        )
        tool_root = repository_root / manifest.manifest.tool_root_name
        verify_toolchain(manifest.manifest, tool_root)
        receipt_hash = _hash_bytes((tool_root / "toolchain-receipt.json").read_bytes())
    else:
        receipt_hash = frozen_toolchain_receipt_hash
    gate_statuses = {
        gate.gate_id: gate.status for gate in bundle.evaluation.assessments
    }
    if any(status != "PASS" for status in gate_statuses.values()):
        raise M4SignoffError("M4 candidate did not pass the complete gate cascade")
    formal_stage_hashes = dict(
        sorted(
            (stage_id, reference.sha256)
            for stage_id, reference in bundle.formal_stage_result_artifacts.items()
        )
    )
    mapped_effect_hash = canonical_sha256(bundle.mapped_structural_effect)
    input_payload = {
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m3_commit_sha": m3_report.commit_sha,
        "m3_packet_hash": m3_packet_hash,
        "m3_report_hash": m3_report.report_hash,
        "parent_run_index_hash": parent_index.index_hash,
        "candidate_run_index_hash": candidate_index.index_hash,
        "candidate_bundle_hash": bundle.bundle_hash,
        "candidate_classification": bundle.candidate.classification,
        "evaluation_hash": canonical_sha256(bundle.evaluation),
        "gate_event_ids": bundle.gate_event_ids,
        "replay_event_sequence_range": bundle.replay_event_sequence_range,
        "replay_prefix_digest": bundle.replay_prefix_digest,
        "prephysical_proof_hash": canonical_sha256(bundle.prephysical_proof),
        "final_proof_hash": canonical_sha256(bundle.final_proof),
        "formal_stage_result_hashes": formal_stage_hashes,
        "mapped_structural_effect_hash": mapped_effect_hash,
        "objective_improvements": bundle.objective_improvements,
        "experiment_record_hash": bundle.experiment_record_artifact.sha256,
        "baseline_replay_digest": baseline_replay,
        "candidate_replay_digest": candidate_replay,
        "baseline_ledger_hash": baseline_ledger,
        "candidate_ledger_hash": candidate_ledger,
        "toolchain_receipt_hash": receipt_hash,
        "platform_lock_hash": candidate_index.platform_lock_hash,
    }
    payload = {
        "schema_version": 2,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m3_commit_sha": m3_report.commit_sha,
        "m3_packet_hash": m3_packet_hash,
        "m3_report_hash": m3_report.report_hash,
        "parent_run_id": parent_index.run_id,
        "parent_run_index_hash": parent_index.index_hash,
        "candidate_id": bundle.candidate.candidate_id,
        "candidate_run_id": candidate_index.run_id,
        "candidate_run_index_hash": candidate_index.index_hash,
        "candidate_bundle_hash": bundle.bundle_hash,
        "candidate_source_hash": bundle.candidate.source_hash,
        "patch_hash": bundle.candidate.patch_artifact.sha256,
        "transform_fingerprint": bundle.candidate.transform_fingerprint,
        "candidate_classification": bundle.candidate.classification,
        "evaluation_hash": canonical_sha256(bundle.evaluation),
        "gate_statuses": gate_statuses,
        "gate_event_ids": bundle.gate_event_ids,
        "replay_event_sequence_range": bundle.replay_event_sequence_range,
        "replay_prefix_digest": bundle.replay_prefix_digest,
        "prephysical_proof_hash": input_payload["prephysical_proof_hash"],
        "final_proof_hash": input_payload["final_proof_hash"],
        "formal_stage_result_hashes": formal_stage_hashes,
        "mapped_structural_effect_hash": mapped_effect_hash,
        "objective_improvements": bundle.objective_improvements,
        "experiment_record_hash": bundle.experiment_record_artifact.sha256,
        "required_view_comparisons": dict(sorted(comparisons.items())),
        "baseline_replay_digest": baseline_replay,
        "candidate_replay_digest": candidate_replay,
        "baseline_ledger_hash": baseline_ledger,
        "candidate_ledger_hash": candidate_ledger,
        "toolchain_receipt_hash": receipt_hash,
        "platform_lock_hash": candidate_index.platform_lock_hash,
        "input_set_hash": canonical_sha256(input_payload),
    }
    provisional = M4SignoffReport.model_construct(
        **payload,
        report_hash="sha256:" + "0" * 64,
    )
    return M4SignoffReport(
        **payload,
        report_hash=canonical_sha256(
            provisional,
            exclude=frozenset({"report_hash"}),
        ),
    )


def _reconstruct(
    bundle_path: Path,
    m3_packet: Path,
    repository_root: Path,
) -> M4SignoffReport:
    commit, tree = _clean_checkpoint(repository_root)
    bundle = verify_candidate_bundle(bundle_path, repository_root=repository_root)
    m3_report, m3_packet_hash = _m3_dependency(m3_packet, repository_root, commit)
    return _build_report(
        bundle_path,
        bundle,
        m3_report,
        m3_packet_hash,
        repository_root=repository_root,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )


def _publish_atomic(destination: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_m4_signoff(
    candidate_bundle: Path,
    m3_packet: Path,
    *,
    repository_root: Path = Path("."),
) -> tuple[Path, M4SignoffReport]:
    """Reconstruct, self-verify, and atomically publish the M4 packet."""

    root = repository_root.resolve(strict=True)
    bundle_path = candidate_bundle.resolve(strict=True)
    report = _reconstruct(bundle_path, m3_packet, root)
    destination = bundle_path.parent / "m4-signoff.json"
    _publish_atomic(destination, canonical_json_bytes(report))
    verify_m4_signoff(destination, m3_packet=m3_packet, repository_root=root)
    return destination, report


def verify_m4_signoff(
    report_path: Path,
    *,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> M4SignoffReport:
    """Independently reconstruct a stored M4 report from its immutable bundle."""

    try:
        resolved = report_path.resolve(strict=True)
        observed = M4SignoffReport.model_validate_json(resolved.read_bytes())
    except (OSError, ValueError) as error:
        raise M4SignoffError("M4 sign-off report is missing or invalid") from error
    expected = _reconstruct(
        resolved.parent / "candidate-bundle.json",
        m3_packet,
        repository_root.resolve(strict=True),
    )
    if observed != expected:
        raise M4SignoffError("M4 sign-off report differs from reconstructed evidence")
    return observed


def verify_m4_dependency_snapshot(
    report_path: Path,
    *,
    m3_packet: Path,
    repository_root: Path,
    descendant_commit: str,
) -> tuple[M4SignoffReport, str]:
    """Verify frozen M4 evidence as an ancestor of a newer milestone."""

    try:
        path = report_path.resolve(strict=True)
        content = path.read_bytes()
        observed = M4SignoffReport.model_validate_json(content)
    except (OSError, ValueError) as error:
        raise M4SignoffError("M4 sign-off packet is missing or invalid") from error
    root = repository_root.resolve(strict=True)
    _require_git_ancestor(root, observed.commit_sha, descendant_commit)
    tree = _git_output(root, "rev-parse", "--verify", f"{observed.commit_sha}^{{tree}}")
    if tree != observed.implementation_tree_hash:
        raise M4SignoffError("M4 packet does not match its recorded Git checkpoint")
    bundle_path = path.parent / "candidate-bundle.json"
    bundle = verify_candidate_bundle(
        bundle_path,
        repository_root=root,
        _verify_runtime_toolchain=False,
    )
    if not isinstance(bundle, M4CandidateBundle):
        raise M4SignoffError("M4 sign-off dependency cannot bind a rejected candidate")
    m3_report, m3_packet_hash = _m3_dependency(
        m3_packet,
        repository_root=root,
        current_commit=observed.commit_sha,
    )
    expected = _build_report(
        bundle_path,
        bundle,
        m3_report,
        m3_packet_hash,
        repository_root=root,
        commit_sha=observed.commit_sha,
        implementation_tree_hash=observed.implementation_tree_hash,
        frozen_toolchain_receipt_hash=observed.toolchain_receipt_hash,
    )
    if observed != expected:
        raise M4SignoffError("M4 dependency differs from reconstructed frozen evidence")
    return observed, _hash_bytes(content)


__all__ = [
    "M4SignoffError",
    "run_m4_signoff",
    "verify_m4_dependency_snapshot",
    "verify_m4_signoff",
]
