"""Fail-closed M8 bounded-council milestone sign-off."""

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
from nova_rtl.contracts.planning import (
    CouncilRequest,
    CouncilTrace,
    M8GateEvidence,
    M8SignoffReport,
    PlannerResult,
)
from nova_rtl.optimization.council_evidence import verify_council_evidence
from nova_rtl.recovery.signoff import (
    M7SignoffError,
    verify_m7_dependency_snapshot,
)


class M8SignoffError(RuntimeError):
    pass


_GATE_EVIDENCE_ID = "m8_council_safety_matrix"
_GATE_RELATIVE_PATH = "reports/m8-council-safety-matrix.txt"


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M8SignoffError("Git is required for M8 sign-off")
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
        raise M8SignoffError(f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    if Path(__file__).resolve(strict=True) != root / "src/nova_rtl/optimization/council_signoff.py":
        raise M8SignoffError("loaded NOVA package is outside the requested repository")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M8SignoffError("M8 sign-off requires a clean final commit")
    return (
        _git_output(root, "rev-parse", "--verify", "HEAD"),
        _git_output(root, "rev-parse", "--verify", "HEAD^{tree}"),
    )


def _implementation_hash(repository_root: Path) -> str:
    paths = (
        "src/nova_rtl/contracts/planning.py",
        "src/nova_rtl/optimization/council_evidence.py",
        "src/nova_rtl/optimization/council_showcase.py",
        "src/nova_rtl/optimization/council_signoff.py",
        "src/nova_rtl/planner/council.py",
        "src/nova_rtl/planner/context.py",
        "src/nova_rtl/planner/evidence.py",
        "src/nova_rtl/planner/search.py",
        "config/policy/council.yaml",
        "config/prompts/chair.md",
        "config/prompts/formal_critic.md",
        "config/prompts/logic_or_domain_specialist.md",
        "config/prompts/ppa_critic.md",
        "config/prompts/timing_forensics.md",
    )
    try:
        return canonical_sha256(
            {path: _hash_bytes((repository_root / path).read_bytes()) for path in paths}
        )
    except OSError as error:
        raise M8SignoffError(f"cannot hash M8 implementation: {error}") from error


def _council_matrix_argv() -> tuple[str, ...]:
    return (
        "python",
        "-P",
        "-m",
        "pytest",
        "tests/unit/planner",
        "tests/integration/small/test_council_flow.py",
        "-q",
        "-p",
        "no:cacheprovider",
    )


def _normalize_gate_output(output: bytes) -> bytes:
    decoded = output.decode("utf-8", errors="strict")
    return re.sub(
        r"(?m)(\d+ passed(?:, \d+ skipped)?)(?: in [0-9.]+s)?$",
        r"\1 in <elapsed>",
        decoded,
    ).encode()


def _publish_immutable(destination: Path, content: bytes) -> None:
    if destination.exists():
        if destination.read_bytes() != content:
            raise M8SignoffError(f"immutable M8 evidence differs: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _run_gate(repository_root: Path, destination: Path) -> M8GateEvidence:
    argv = _council_matrix_argv()
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
    raw = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise M8SignoffError(
            "M8 council safety matrix failed: "
            + raw.decode(errors="replace")[-2000:]
        )
    content = _normalize_gate_output(raw)
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s|$)", content.decode())
    if len(matches) != 1 or int(matches[0]) < 1:
        raise M8SignoffError("M8 council matrix lacks one passing count")
    _publish_immutable(destination, content)
    return M8GateEvidence(
        evidence_id=_GATE_EVIDENCE_ID,
        argv=argv,
        output_relative_path=_GATE_RELATIVE_PATH,
        output_hash=_hash_bytes(content),
        output_size_bytes=len(content),
        passed_test_count=int(matches[0]),
    )


def _verify_gate_evidence(directory: Path, evidence: M8GateEvidence) -> None:
    if (
        evidence.evidence_id != _GATE_EVIDENCE_ID
        or evidence.argv != _council_matrix_argv()
        or evidence.output_relative_path != _GATE_RELATIVE_PATH
    ):
        raise M8SignoffError("M8 gate evidence specification changed")
    path = directory / evidence.output_relative_path
    try:
        content = path.read_bytes()
    except OSError as error:
        raise M8SignoffError("M8 gate evidence is missing") from error
    if len(content) != evidence.output_size_bytes or _hash_bytes(content) != evidence.output_hash:
        raise M8SignoffError("M8 gate evidence identity changed")


def _normalize(value: Any) -> Any:
    if isinstance(value, StrictContract):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_normalize(item) for item in value)
    return value


def _build_report(
    council_directory: Path,
    *,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    gate: M8GateEvidence,
    commit_sha: str,
    implementation_tree_hash: str,
    frozen_council_implementation_hash: str | None = None,
) -> M8SignoffReport:
    try:
        council = verify_council_evidence(council_directory)
        request = CouncilRequest.model_validate_json(
            (council_directory / "council-request.json").read_bytes()
        )
        trace = CouncilTrace.model_validate_json(
            (council_directory / "council-trace.json").read_bytes()
        )
        planner = PlannerResult.model_validate_json(
            (council_directory / "planner-result.json").read_bytes()
        )
        m7, m7_hash = verify_m7_dependency_snapshot(
            m7_packet,
            path_migration_report=path_migration_report,
            m6_packet=m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
            descendant_commit=commit_sha,
        )
    except (M7SignoffError, OSError, ValueError) as error:
        raise M8SignoffError("M8 dependency reconstruction failed") from error
    objections = tuple(
        objection
        for report in council.critique_reports
        for objection in report.objections
    )
    if (
        council.status != "PASS"
        or planner.status != "PASS"
        or trace.trace_completeness_percent != 100.0
        or trace.deadline_outcome != "MET"
        or len(council.selected_role_ids) > 5
        or set(item.critic_class for item in council.critique_reports)
        != {"FORMAL", "PPA"}
        or not objections
        or len(council.critique_dispositions) != len(objections)
    ):
        raise M8SignoffError("M8 council evidence does not satisfy the exit gate")
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m7_commit_sha": m7.commit_sha,
        "m7_packet_hash": m7_hash,
        "m7_report_hash": m7.report_hash,
        "run_id": council.run_id,
        "opportunity_id": council.opportunity_id,
        "council_request_hash": canonical_sha256(request),
        "council_result_hash": canonical_sha256(council),
        "council_trace_hash": canonical_sha256(trace),
        "planner_result_hash": canonical_sha256(planner),
        "council_policy_hash": council.policy_hash,
        "selected_role_ids": council.selected_role_ids,
        "proposer_role_count": sum(
            item.role_kind == "PROPOSER" for item in council.private_role_records
        ),
        "critic_classes": tuple(
            sorted({item.critic_class for item in council.critique_reports})
        ),
        "final_proposal_count": len(council.final_ordered_proposal_ids),
        "critique_objection_count": len(objections),
        "critique_disposition_count": len(council.critique_dispositions),
        "revision_count": len(council.revision_records),
        "aggregate_token_budget": request.aggregate_token_budget,
        "total_tokens": council.total_tokens,
        "council_deadline_seconds": min(120, request.aggregate_latency_budget_ms // 1000),
        "trace_completeness_percent": 100.0,
        "deterministic_replay": True,
        "bounded_fallback_validated": True,
        "council_implementation_hash": (
            frozen_council_implementation_hash
            if frozen_council_implementation_hash is not None
            else _implementation_hash(repository_root)
        ),
        "council_matrix_evidence": gate,
    }
    input_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "status"}
    }
    payload["input_set_hash"] = canonical_sha256(_normalize(input_payload))
    provisional = M8SignoffReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    return M8SignoffReport(
        **payload,
        report_hash=canonical_sha256(provisional, exclude=frozenset({"report_hash"})),
    )


def run_m8_signoff(
    council_directory: Path,
    *,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> tuple[Path, M8SignoffReport]:
    root = repository_root.resolve(strict=True)
    directory = council_directory.resolve(strict=True)
    commit, tree = _clean_checkpoint(root)
    gate = _run_gate(root, directory / _GATE_RELATIVE_PATH)
    report = _build_report(
        directory,
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        gate=gate,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    destination = directory / "m8-signoff.json"
    _publish_immutable(destination, canonical_json_bytes(report) + b"\n")
    verify_m8_signoff(
        destination,
        council_directory=directory,
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
    )
    return destination, report


def verify_m8_signoff(
    report_path: Path,
    *,
    council_directory: Path,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> M8SignoffReport:
    try:
        resolved = report_path.resolve(strict=True)
        observed = M8SignoffReport.model_validate_json(resolved.read_bytes())
        _verify_gate_evidence(resolved.parent, observed.council_matrix_evidence)
    except (OSError, ValueError) as error:
        raise M8SignoffError("M8 sign-off report is missing or invalid") from error
    commit, tree = _clean_checkpoint(repository_root)
    expected = _build_report(
        council_directory.resolve(strict=True),
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=repository_root.resolve(strict=True),
        gate=observed.council_matrix_evidence,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    if observed != expected:
        raise M8SignoffError("M8 sign-off differs from reconstructed evidence")
    return observed


def verify_m8_dependency_snapshot(
    report_path: Path,
    *,
    council_directory: Path,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    descendant_commit: str,
) -> tuple[M8SignoffReport, str]:
    """Reconstruct an immutable M8 packet at its signed ancestor commit."""

    try:
        resolved = report_path.resolve(strict=True)
        content = resolved.read_bytes()
        observed = M8SignoffReport.model_validate_json(content)
        _verify_gate_evidence(resolved.parent, observed.council_matrix_evidence)
    except (OSError, ValueError) as error:
        raise M8SignoffError("M8 dependency report is missing or invalid") from error
    root = repository_root.resolve(strict=True)
    try:
        _git_output(
            root,
            "merge-base",
            "--is-ancestor",
            observed.commit_sha,
            descendant_commit,
        )
    except M8SignoffError as error:
        raise M8SignoffError("M8 commit is not an ancestor of the descendant") from error
    tree = _git_output(root, "rev-parse", "--verify", f"{observed.commit_sha}^{{tree}}")
    if tree != observed.implementation_tree_hash:
        raise M8SignoffError("M8 dependency implementation tree changed")
    expected = _build_report(
        council_directory.resolve(strict=True),
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        gate=observed.council_matrix_evidence,
        commit_sha=observed.commit_sha,
        implementation_tree_hash=observed.implementation_tree_hash,
        frozen_council_implementation_hash=observed.council_implementation_hash,
    )
    if observed != expected:
        raise M8SignoffError("M8 dependency differs from reconstructed evidence")
    return observed, _hash_bytes(content)


__all__ = [
    "M8SignoffError",
    "run_m8_signoff",
    "verify_m8_dependency_snapshot",
    "verify_m8_signoff",
]
