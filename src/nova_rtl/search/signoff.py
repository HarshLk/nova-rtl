"""Fail-closed M5 deterministic-search milestone sign-off."""

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

from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import StrictContract, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.reporting import M5GateEvidence, M5SignoffReport
from nova_rtl.optimization.search_flow import (
    M5SearchBundle,
    M5SearchFlowError,
    verify_deterministic_search,
)
from nova_rtl.optimization.signoff import (
    M4SignoffError,
    verify_m4_dependency_snapshot,
)
from nova_rtl.search.controller import SearchTrace
from nova_rtl.transforms.registry import competition_mvp_registry


class M5SignoffError(M5SearchFlowError):
    """The M5 search packet cannot be reconstructed or trusted."""


_EXPECTED_OPERATIONS = (
    "BALANCE_BOOLEAN_TREE",
    "FACTOR_COMMON_PREDICATE",
    "FSM_DECODE_RESTRUCTURE",
    "RESTRUCTURE_PRIORITY_MUX",
)
_GATE_EVIDENCE_ID = "m5_transform_formal_matrix"
_GATE_RELATIVE_PATH = "reports/m5-transform-formal-matrix.txt"


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M5SignoffError("Git is required for M5 sign-off")
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
        raise M5SignoffError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    expected = root / "src/nova_rtl/search/signoff.py"
    if Path(__file__).resolve(strict=True) != expected:
        raise M5SignoffError("loaded NOVA package is outside the requested repository")
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise M5SignoffError("requested NOVA repository is not the Git repository root")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M5SignoffError("M5 sign-off requires a clean final commit")
    return (
        _git_output(root, "rev-parse", "--verify", "HEAD"),
        _git_output(root, "rev-parse", "--verify", "HEAD^{tree}"),
    )


def _require_git_ancestor(
    repository_root: Path, ancestor_commit: str, descendant_commit: str
) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise M5SignoffError("Git is required for M5 dependency verification")
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
        raise M5SignoffError(
            f"recorded M5 commit {ancestor_commit} is not an ancestor of "
            f"{descendant_commit}"
        )


def _formal_matrix_argv() -> tuple[str, ...]:
    return (
        "python",
        "-P",
        "-m",
        "pytest",
        "tests/unit/transforms",
        "tests/integration/tiny/test_transform_formal_matrix.py",
        "-q",
        "-p",
        "no:cacheprovider",
    )


def _formal_environment(repository_root: Path) -> dict[str, str]:
    path_parts = [
        str(repository_root / ".nova-tools" / "bin"),
        str(Path(sys.executable).resolve().parent),
        "/usr/bin",
        "/bin",
    ]
    environment = {
        "HOME": os.environ.get("HOME", str(repository_root)),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": ":".join(path_parts),
        "PYTHONHASHSEED": "0",
    }
    for name in ("LD_LIBRARY_PATH", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_formal_matrix(repository_root: Path, destination: Path) -> M5GateEvidence:
    argv = _formal_matrix_argv()
    execution_argv = (str(Path(sys.executable).resolve(strict=True)), *argv[1:])
    try:
        completed = subprocess.run(
            execution_argv,
            cwd=repository_root,
            env=_formal_environment(repository_root),
            shell=False,
            check=False,
            capture_output=True,
            text=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise M5SignoffError(f"M5 transform/formal matrix could not execute: {error}") from error
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        detail = output.decode("utf-8", errors="replace")[-2000:]
        raise M5SignoffError(
            f"M5 transform/formal matrix failed with exit code {completed.returncode}: {detail}"
        )
    decoded = output.decode("utf-8", errors="strict")
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s|$)", decoded)
    if len(matches) != 1:
        raise M5SignoffError("M5 transform/formal matrix did not report one passing count")
    passed = int(matches[0])
    if passed < 1 or " failed" in decoded or " error" in decoded:
        raise M5SignoffError("M5 transform/formal matrix is not a clean passing result")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_atomic(destination, output)
    return M5GateEvidence(
        evidence_id=_GATE_EVIDENCE_ID,
        argv=argv,
        output_relative_path=_GATE_RELATIVE_PATH,
        output_hash=_hash_bytes(output),
        output_size_bytes=len(output),
        passed_test_count=passed,
    )


def _verify_formal_matrix_evidence(
    report_directory: Path, evidence: M5GateEvidence
) -> None:
    if evidence.evidence_id != _GATE_EVIDENCE_ID:
        raise M5SignoffError("M5 formal matrix evidence identity changed")
    if evidence.argv != _formal_matrix_argv():
        raise M5SignoffError("M5 formal matrix command specification changed")
    if evidence.output_relative_path != _GATE_RELATIVE_PATH:
        raise M5SignoffError("M5 formal matrix output path is not canonical")
    relative = Path(evidence.output_relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise M5SignoffError("M5 formal matrix output escapes the packet directory")
    try:
        content = (report_directory / relative).read_bytes()
    except OSError as error:
        raise M5SignoffError("M5 formal matrix output is missing") from error
    if len(content) != evidence.output_size_bytes or _hash_bytes(content) != evidence.output_hash:
        raise M5SignoffError("M5 formal matrix output identity changed")
    decoded = content.decode("utf-8", errors="strict")
    if f"{evidence.passed_test_count} passed" not in decoded:
        raise M5SignoffError("M5 formal matrix passing count is not preserved")


def _normalized_json_value(value: Any) -> Any:
    if isinstance(value, StrictContract):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _normalized_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_normalized_json_value(item) for item in value)
    return value


def _input_set_hash(payload: Mapping[str, Any]) -> str:
    excluded = {"schema_version", "status", "input_set_hash", "report_hash"}
    return canonical_sha256(
        {
            key: _normalized_json_value(value)
            for key, value in payload.items()
            if key not in excluded
        }
    )


def _search_replay_digest(records: tuple[StrictContract, ...]) -> str:
    return canonical_sha256(
        {"experiment_records": _normalized_json_value(records)}
    )


def _search_ledger_evidence(
    bundle_path: Path, bundle: M5SearchBundle
) -> tuple[str, str]:
    ledger_path = bundle_path.parent / "experiment-ledger.sqlite3"
    run_directory = bundle_path.resolve().parents[3]
    ledger = ExperimentLedger.open_existing(
        ledger_path,
        artifact_store=ArtifactStore.open_existing(run_directory / "artifacts"),
    )
    records = tuple(ledger.iter_records(bundle.run_id))
    if tuple(item.candidate_id for item in records) != bundle.search_result.ordered_candidate_ids:
        raise M5SignoffError("M5 search ledger does not cover evaluated candidates")
    try:
        ledger_hash = _hash_bytes(ledger_path.read_bytes())
    except OSError as error:
        raise M5SignoffError("M5 search ledger is missing") from error
    return ledger_hash, _search_replay_digest(records)


def _trace_hash(bundle_path: Path, bundle: M5SearchBundle) -> str:
    run_directory = bundle_path.resolve().parents[3]
    store = ArtifactStore.open_existing(run_directory / "artifacts")
    refs = tuple(
        item
        for item in bundle.search_result.artifact_refs
        if item.artifact_id.startswith("artifact_search_trace_")
    )
    if len(refs) != 1:
        raise M5SignoffError("M5 search lacks one canonical trace")
    try:
        trace = SearchTrace.model_validate_json(store.open_verified(refs[0]).read())
    except (OSError, ValueError) as error:
        raise M5SignoffError("M5 search trace is invalid") from error
    return trace.trace_hash


def _build_report(
    bundle_path: Path,
    bundle: M5SearchBundle,
    *,
    m4_report,  # type: ignore[no-untyped-def]
    m4_packet_hash: str,
    formal_evidence: M5GateEvidence,
    commit_sha: str,
    implementation_tree_hash: str,
    frozen_transform_operations: tuple[str, ...] | None = None,
) -> M5SignoffReport:
    operations = (
        frozen_transform_operations
        if frozen_transform_operations is not None
        else tuple(competition_mvp_registry().operations)
    )
    if operations != _EXPECTED_OPERATIONS:
        raise M5SignoffError("M5 competition transform registry is incomplete or reordered")
    selected = bundle.search_result.selected_candidate_id
    if selected is None:
        raise M5SignoffError("M5 search has no selected strict candidate")
    if selected != m4_report.candidate_id:
        raise M5SignoffError("M5 selected candidate differs from the signed M4 candidate")
    if bundle.candidate_bundle_hashes.get(selected) != m4_report.candidate_bundle_hash:
        raise M5SignoffError("M5 selected candidate bundle differs from signed M4 evidence")
    if not bundle.valid_negative_candidate_ids:
        raise M5SignoffError("M5 exit gate requires one valid negative result")
    consumed = bundle.search_result.consumed_budgets
    request = bundle.search_request
    budget_compliance = all(
        (
            consumed.candidates <= request.candidate_budget,
            consumed.formal_jobs <= request.formal_budget,
            consumed.physical_jobs <= request.physical_budget,
            consumed.tokens <= request.token_budget,
            consumed.latency_ms <= request.latency_budget_ms,
        )
    )
    if not budget_compliance:
        raise M5SignoffError("M5 search exceeded a declared budget")
    strict_frontier = bundle.pareto_archive.frontier_candidate_ids.get(
        "STRICT_SEQ_EQUIV", ()
    )
    selected_records = tuple(
        item for item in bundle.pareto_archive.records if item.candidate_id == selected
    )
    if len(selected_records) != 1:
        raise M5SignoffError("M5 selected candidate lacks one Pareto record")
    ledger_hash, replay_digest = _search_ledger_evidence(bundle_path, bundle)
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m4_commit_sha": m4_report.commit_sha,
        "m4_packet_hash": m4_packet_hash,
        "m4_report_hash": m4_report.report_hash,
        "m4_candidate_id": m4_report.candidate_id,
        "run_id": bundle.run_id,
        "search_bundle_hash": bundle.bundle_hash,
        "search_implementation_hash": bundle.implementation_hash,
        "transform_registry_hash": bundle.transform_registry_hash,
        "transform_operations": operations,
        "search_request_hash": canonical_sha256(bundle.search_request),
        "search_result_hash": canonical_sha256(bundle.search_result),
        "candidate_dag_hash": bundle.candidate_dag.dag_hash,
        "pareto_archive_hash": bundle.pareto_archive.archive_hash,
        "search_trace_hash": _trace_hash(bundle_path, bundle),
        "search_ledger_hash": ledger_hash,
        "search_replay_digest": replay_digest,
        "evaluated_candidate_ids": bundle.search_result.ordered_candidate_ids,
        "strict_frontier_candidate_ids": strict_frontier,
        "selected_candidate_id": selected,
        "valid_negative_candidate_ids": bundle.valid_negative_candidate_ids,
        "consumed_budgets": consumed,
        "budget_compliance": True,
        "platform_lock_hash": m4_report.platform_lock_hash,
        "toolchain_receipt_hash": m4_report.toolchain_receipt_hash,
        "comparison_identity_hashes": selected_records[0].comparison_identity_hashes,
        "formal_matrix_evidence": formal_evidence,
    }
    payload["input_set_hash"] = _input_set_hash(payload)
    provisional = M5SignoffReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    return M5SignoffReport(
        **payload,
        report_hash=canonical_sha256(provisional, exclude=frozenset({"report_hash"})),
    )


def _reconstruct(
    bundle_path: Path,
    *,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    formal_evidence: M5GateEvidence,
) -> M5SignoffReport:
    commit, tree = _clean_checkpoint(repository_root)
    try:
        bundle = verify_deterministic_search(
            bundle_path.resolve(strict=True), repository_root=repository_root
        )
        m4_report, m4_packet_hash = verify_m4_dependency_snapshot(
            m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
            descendant_commit=commit,
        )
    except (M4SignoffError, M5SearchFlowError, OSError, ValueError) as error:
        raise M5SignoffError("M5 dependency reconstruction failed") from error
    return _build_report(
        bundle_path,
        bundle,
        m4_report=m4_report,
        m4_packet_hash=m4_packet_hash,
        formal_evidence=formal_evidence,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )


def _publish_atomic(destination: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
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


def run_m5_signoff(
    search_bundle: Path,
    *,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> tuple[Path, M5SignoffReport]:
    """Execute the M5 exit gate and publish a commit-bound packet."""

    root = repository_root.resolve(strict=True)
    bundle_path = search_bundle.resolve(strict=True)
    report_directory = bundle_path.parent
    evidence = _run_formal_matrix(root, report_directory / _GATE_RELATIVE_PATH)
    report = _reconstruct(
        bundle_path,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        formal_evidence=evidence,
    )
    destination = report_directory / "m5-signoff.json"
    _publish_atomic(destination, canonical_json_bytes(report) + b"\n")
    verify_m5_signoff(
        destination,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
    )
    return destination, report


def verify_m5_signoff(
    report_path: Path,
    *,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> M5SignoffReport:
    """Reconstruct every M5 identity without rerunning EDA or formal tools."""

    try:
        resolved = report_path.resolve(strict=True)
        observed = M5SignoffReport.model_validate_json(resolved.read_bytes())
        _verify_formal_matrix_evidence(resolved.parent, observed.formal_matrix_evidence)
    except (OSError, UnicodeError, ValueError) as error:
        raise M5SignoffError("M5 sign-off report is missing or invalid") from error
    expected = _reconstruct(
        resolved.parent / "search-bundle.json",
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=repository_root.resolve(strict=True),
        formal_evidence=observed.formal_matrix_evidence,
    )
    if observed != expected:
        raise M5SignoffError("M5 sign-off report differs from reconstructed evidence")
    return observed


def verify_m5_dependency_snapshot(
    report_path: Path,
    *,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    descendant_commit: str,
) -> tuple[M5SignoffReport, str]:
    """Verify frozen M5 evidence as an ancestor of a newer milestone."""

    try:
        resolved = report_path.resolve(strict=True)
        content = resolved.read_bytes()
        observed = M5SignoffReport.model_validate_json(content)
    except (OSError, ValueError) as error:
        raise M5SignoffError("M5 sign-off packet is missing or invalid") from error
    root = repository_root.resolve(strict=True)
    _require_git_ancestor(root, observed.commit_sha, descendant_commit)
    tree = _git_output(root, "rev-parse", "--verify", f"{observed.commit_sha}^{{tree}}")
    if tree != observed.implementation_tree_hash:
        raise M5SignoffError("M5 packet does not match its recorded Git checkpoint")
    _verify_formal_matrix_evidence(resolved.parent, observed.formal_matrix_evidence)
    try:
        bundle_path = resolved.parent / "search-bundle.json"
        bundle = M5SearchBundle.model_validate_json(bundle_path.read_bytes())
        m4_report, m4_packet_hash = verify_m4_dependency_snapshot(
            m4_packet,
            m3_packet=m3_packet,
            repository_root=root,
            descendant_commit=observed.commit_sha,
        )
    except (M4SignoffError, OSError, ValueError) as error:
        raise M5SignoffError("M5 frozen dependency reconstruction failed") from error
    expected = _build_report(
        bundle_path,
        bundle,
        m4_report=m4_report,
        m4_packet_hash=m4_packet_hash,
        formal_evidence=observed.formal_matrix_evidence,
        commit_sha=observed.commit_sha,
        implementation_tree_hash=observed.implementation_tree_hash,
        frozen_transform_operations=tuple(observed.transform_operations),
    )
    if observed != expected:
        raise M5SignoffError("M5 dependency differs from reconstructed frozen evidence")
    return observed, _hash_bytes(content)


__all__ = [
    "M5SignoffError",
    "run_m5_signoff",
    "verify_m5_dependency_snapshot",
    "verify_m5_signoff",
]
