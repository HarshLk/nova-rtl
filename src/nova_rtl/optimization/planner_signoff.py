"""Fail-closed M6 constrained-planner milestone sign-off."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from nova_rtl.contracts.base import StrictContract, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.planning import M6GateEvidence, M6SignoffReport
from nova_rtl.optimization.planner_flow import (
    M6PlannerFlowError,
    M6PlannerRun,
    verify_planner_run,
)
from nova_rtl.search.signoff import M5SignoffError, verify_m5_dependency_snapshot
from nova_rtl.transforms.registry import competition_mvp_registry


class M6SignoffError(M6PlannerFlowError):
    """The M6 planner packet cannot be reconstructed or trusted."""


_GATE_EVIDENCE_ID = "m6_planner_safety_matrix"
_GATE_RELATIVE_PATH = "reports/m6-planner-safety-matrix.txt"


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M6SignoffError("Git is required for M6 sign-off")
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
        raise M6SignoffError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    expected = root / "src/nova_rtl/optimization/planner_signoff.py"
    if Path(__file__).resolve(strict=True) != expected:
        raise M6SignoffError("loaded NOVA package is outside the requested repository")
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise M6SignoffError("requested NOVA repository is not the Git root")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M6SignoffError("M6 sign-off requires a clean final commit")
    return (
        _git_output(root, "rev-parse", "--verify", "HEAD"),
        _git_output(root, "rev-parse", "--verify", "HEAD^{tree}"),
    )


def _planner_matrix_argv() -> tuple[str, ...]:
    return (
        "python",
        "-P",
        "-m",
        "pytest",
        "tests/unit/planner",
        "tests/integration/small/test_planner_equivalence.py",
        "-q",
        "-p",
        "no:cacheprovider",
    )


def _run_gate(repository_root: Path, destination: Path) -> M6GateEvidence:
    argv = _planner_matrix_argv()
    completed = subprocess.run(
        (str(Path(sys.executable).resolve(strict=True)), *argv[1:]),
        cwd=repository_root,
        env={
            "HOME": os.environ.get("HOME", str(repository_root)),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": f"{Path(sys.executable).resolve().parent}:/usr/bin:/bin",
            "PYTHONHASHSEED": "0",
        },
        shell=False,
        check=False,
        capture_output=True,
        text=False,
        timeout=300,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise M6SignoffError(
            "M6 planner safety matrix failed: "
            + output.decode("utf-8", errors="replace")[-2000:]
        )
    decoded = output.decode("utf-8", errors="strict")
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s|$)", decoded)
    if len(matches) != 1 or int(matches[0]) < 1:
        raise M6SignoffError("M6 planner safety matrix lacks one passing count")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_atomic(destination, output)
    return M6GateEvidence(
        evidence_id=_GATE_EVIDENCE_ID,
        argv=argv,
        output_relative_path=_GATE_RELATIVE_PATH,
        output_hash=_hash_bytes(output),
        output_size_bytes=len(output),
        passed_test_count=int(matches[0]),
    )


def _verify_gate_evidence(directory: Path, evidence: M6GateEvidence) -> None:
    if evidence.evidence_id != _GATE_EVIDENCE_ID:
        raise M6SignoffError("M6 gate evidence identity changed")
    if evidence.argv != _planner_matrix_argv():
        raise M6SignoffError("M6 gate command specification changed")
    if evidence.output_relative_path != _GATE_RELATIVE_PATH:
        raise M6SignoffError("M6 gate output path is not canonical")
    relative = Path(evidence.output_relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise M6SignoffError("M6 gate output escapes its packet directory")
    try:
        content = (directory / relative).read_bytes()
    except OSError as error:
        raise M6SignoffError("M6 gate evidence is missing") from error
    if len(content) != evidence.output_size_bytes or _hash_bytes(content) != evidence.output_hash:
        raise M6SignoffError("M6 gate evidence identity changed")
    if f"{evidence.passed_test_count} passed" not in content.decode("utf-8"):
        raise M6SignoffError("M6 gate passing count is not preserved")


def _normalize(value: Any) -> Any:
    if isinstance(value, StrictContract):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_normalize(item) for item in value)
    return value


def _input_set_hash(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            key: _normalize(value)
            for key, value in payload.items()
            if key not in {"schema_version", "status", "input_set_hash", "report_hash"}
        }
    )


def _build_report(
    run: M6PlannerRun,
    *,
    m5_report: Any,
    m5_packet_hash: str,
    gate: M6GateEvidence,
    commit_sha: str,
    implementation_tree_hash: str,
    frozen_registered_operations: tuple[str, ...] | None = None,
) -> M6SignoffReport:
    if run.m5_search_bundle_hash != m5_report.search_bundle_hash:
        raise M6SignoffError("M6 planner run differs from signed M5 search")
    operations = tuple(sorted({item.transformation.operation for item in run.proposals}))
    allowed_operations = (
        frozenset(frozen_registered_operations)
        if frozen_registered_operations is not None
        else competition_mvp_registry().operations
    )
    if not operations or not set(operations).issubset(allowed_operations):
        raise M6SignoffError("M6 planner emitted an unregistered operation")
    requests = {item.opportunity_id: item for item in run.planner_requests}
    budget_compliance = all(
        result.input_tokens + result.output_tokens
        <= requests[result.opportunity_id].token_budget
        and result.latency_ms <= requests[result.opportunity_id].latency_budget_ms
        for result in run.planner_results
    )
    if not budget_compliance:
        raise M6SignoffError("M6 planner exceeded a declared budget")
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m5_commit_sha": m5_report.commit_sha,
        "m5_packet_hash": m5_packet_hash,
        "m5_report_hash": m5_report.report_hash,
        "run_id": run.run_id,
        "m5_search_bundle_hash": run.m5_search_bundle_hash,
        "planner_run_hash": run.bundle_hash,
        "planner_implementation_hash": run.implementation_hash,
        "planner_policy_hash": run.planner_policy_hash,
        "provider_response_hash": run.provider_response_hash,
        "planner_request_hashes": tuple(
            canonical_sha256(item) for item in run.planner_requests
        ),
        "context_pack_hashes": tuple(
            item.rendered_message_hash for item in run.context_packs
        ),
        "provider_result_hashes": tuple(
            canonical_sha256(item) for item in run.provider_results
        ),
        "planner_result_hashes": tuple(
            canonical_sha256(item) for item in run.planner_results
        ),
        "proposal_hashes": tuple(canonical_sha256(item) for item in run.proposals),
        "normalized_plan_hashes": tuple(
            item.proposal_hash for item in run.normalized_plans
        ),
        "registered_operations": operations,
        "validated_planner_modes": ("HEURISTIC", "SINGLE_AGENT"),
        "provider_attempt_count": len(run.provider_results),
        "fallback_count": run.fallback_count,
        "retrieval_grant_count": sum(
            item.decision == "GRANTED" for item in run.retrieval_audit
        ),
        "retrieval_denial_count": sum(
            item.decision == "DENIED" for item in run.retrieval_audit
        ),
        "budget_compliance": True,
        "planner_matrix_evidence": gate,
    }
    payload["input_set_hash"] = _input_set_hash(payload)
    provisional = M6SignoffReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    return M6SignoffReport(
        **payload,
        report_hash=canonical_sha256(provisional, exclude=frozenset({"report_hash"})),
    )


def _reconstruct(
    planner_run: Path,
    *,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    gate: M6GateEvidence,
) -> M6SignoffReport:
    commit, tree = _clean_checkpoint(repository_root)
    try:
        run = verify_planner_run(planner_run, repository_root=repository_root)
        m5_report, m5_hash = verify_m5_dependency_snapshot(
            m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
            descendant_commit=commit,
        )
    except (M5SignoffError, M6PlannerFlowError, OSError, ValueError) as error:
        raise M6SignoffError("M6 dependency reconstruction failed") from error
    return _build_report(
        run,
        m5_report=m5_report,
        m5_packet_hash=m5_hash,
        gate=gate,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )


def _publish_atomic(destination: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_m6_signoff(
    planner_run: Path,
    *,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> tuple[Path, M6SignoffReport]:
    """Run the M6 gate and atomically publish its commit-bound packet."""

    root = repository_root.resolve(strict=True)
    run_path = planner_run.resolve(strict=True)
    destination = run_path.parent / "m6-signoff.json"
    if destination.is_file():
        return destination, verify_m6_signoff(
            destination,
            planner_run=run_path,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=root,
        )
    gate = _run_gate(root, run_path.parent / _GATE_RELATIVE_PATH)
    report = _reconstruct(
        run_path,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        gate=gate,
    )
    _publish_atomic(destination, canonical_json_bytes(report) + b"\n")
    verify_m6_signoff(
        destination,
        planner_run=run_path,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
    )
    return destination, report


def verify_m6_signoff(
    report_path: Path,
    *,
    planner_run: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> M6SignoffReport:
    """Reconstruct M6 without invoking a provider, EDA tool, or test process."""

    try:
        resolved = report_path.resolve(strict=True)
        observed = M6SignoffReport.model_validate_json(resolved.read_bytes())
        _verify_gate_evidence(resolved.parent, observed.planner_matrix_evidence)
    except (OSError, UnicodeError, ValueError) as error:
        raise M6SignoffError("M6 sign-off report is missing or invalid") from error
    root = repository_root.resolve(strict=True)
    current_commit, _ = _clean_checkpoint(root)
    if observed.commit_sha != current_commit:
        frozen, _ = verify_m6_dependency_snapshot(
            resolved,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=root,
            descendant_commit=current_commit,
        )
        return frozen
    expected = _reconstruct(
        planner_run.resolve(strict=True),
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        gate=observed.planner_matrix_evidence,
    )
    if observed != expected:
        raise M6SignoffError("M6 sign-off differs from reconstructed evidence")
    return observed


def verify_m6_dependency_snapshot(
    report_path: Path,
    *,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    descendant_commit: str,
) -> tuple[M6SignoffReport, str]:
    """Verify frozen M6 evidence as an immutable ancestor of a newer milestone."""

    try:
        resolved = report_path.resolve(strict=True)
        content = resolved.read_bytes()
        observed = M6SignoffReport.model_validate_json(content)
        planner_run_path = resolved.parent / "planner-run.json"
        run = M6PlannerRun.model_validate_json(planner_run_path.read_bytes())
    except (OSError, ValueError) as error:
        raise M6SignoffError("M6 dependency packet is missing or invalid") from error
    root = repository_root.resolve(strict=True)
    try:
        _git_output(root, "merge-base", "--is-ancestor", observed.commit_sha, descendant_commit)
    except M6SignoffError as error:
        raise M6SignoffError("M6 commit is not an ancestor of the descendant") from error
    tree = _git_output(root, "rev-parse", "--verify", f"{observed.commit_sha}^{{tree}}")
    if tree != observed.implementation_tree_hash:
        raise M6SignoffError("M6 packet does not match its recorded Git checkpoint")
    _verify_gate_evidence(resolved.parent, observed.planner_matrix_evidence)
    try:
        m5_report, m5_hash = verify_m5_dependency_snapshot(
            m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=root,
            descendant_commit=observed.commit_sha,
        )
    except (M5SignoffError, OSError, ValueError) as error:
        raise M6SignoffError("M6 frozen dependency reconstruction failed") from error
    expected = _build_report(
        run,
        m5_report=m5_report,
        m5_packet_hash=m5_hash,
        gate=observed.planner_matrix_evidence,
        commit_sha=observed.commit_sha,
        implementation_tree_hash=observed.implementation_tree_hash,
        frozen_registered_operations=observed.registered_operations,
    )
    if observed != expected:
        raise M6SignoffError("M6 dependency differs from reconstructed frozen evidence")
    return observed, _hash_bytes(content)


__all__ = [
    "M6SignoffError",
    "run_m6_signoff",
    "verify_m6_dependency_snapshot",
    "verify_m6_signoff",
]
