"""Fail-closed M7 deterministic-recovery milestone sign-off."""

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
from nova_rtl.contracts.recovery import M7GateEvidence, M7SignoffReport
from nova_rtl.contracts.reporting import M5SignoffReport
from nova_rtl.optimization.planner_signoff import (
    M6SignoffError,
    verify_m6_dependency_snapshot,
)
from nova_rtl.optimization.search_flow import M5SearchBundle
from nova_rtl.recovery.showcase import (
    PathMigrationRecoveryError,
    verify_path_migration_showcase,
)


class M7SignoffError(RuntimeError):
    pass


_GATE_EVIDENCE_ID = "m7_recovery_safety_matrix"
_GATE_RELATIVE_PATH = "reports/m7-recovery-safety-matrix.txt"


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M7SignoffError("Git is required for M7 sign-off")
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
        raise M7SignoffError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _clean_checkpoint(repository_root: Path) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    if Path(__file__).resolve(strict=True) != root / "src/nova_rtl/recovery/signoff.py":
        raise M7SignoffError("loaded NOVA package is outside the requested repository")
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise M7SignoffError("requested NOVA repository is not the Git root")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M7SignoffError("M7 sign-off requires a clean final commit")
    return (
        _git_output(root, "rev-parse", "--verify", "HEAD"),
        _git_output(root, "rev-parse", "--verify", "HEAD^{tree}"),
    )


def _implementation_hash(repository_root: Path) -> str:
    paths = (
        "src/nova_rtl/recovery/classifier.py",
        "src/nova_rtl/recovery/compiler.py",
        "src/nova_rtl/recovery/execution.py",
        "src/nova_rtl/recovery/fingerprint.py",
        "src/nova_rtl/recovery/policy.py",
        "src/nova_rtl/recovery/router.py",
        "src/nova_rtl/recovery/safety.py",
        "src/nova_rtl/recovery/search.py",
        "src/nova_rtl/recovery/showcase.py",
        "src/nova_rtl/search/controller.py",
        "src/nova_rtl/optimization/search_flow.py",
        "config/policy/recovery_rules.yaml",
    )
    try:
        identities = {
            path: _hash_bytes((repository_root / path).read_bytes()) for path in paths
        }
    except OSError as error:
        raise M7SignoffError(f"cannot hash M7 implementation: {error}") from error
    return canonical_sha256(identities)


def _normalize(value: Any) -> Any:
    if isinstance(value, StrictContract):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_normalize(item) for item in value)
    return value


def _input_set_hash(payload: Mapping[str, Any]) -> str:
    return canonical_sha256({key: _normalize(value) for key, value in payload.items()})


def _recovery_matrix_argv() -> tuple[str, ...]:
    return (
        "python",
        "-P",
        "-m",
        "pytest",
        "tests/unit/recovery",
        "tests/integration/small/test_path_migration_recovery.py",
        "-q",
        "-p",
        "no:cacheprovider",
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


def _publish_immutable(destination: Path, content: bytes) -> None:
    if destination.exists():
        if destination.read_bytes() != content:
            raise M7SignoffError(f"immutable M7 evidence differs: {destination}")
        return
    _publish_atomic(destination, content)


def _normalize_gate_output(output: bytes) -> bytes:
    decoded = output.decode("utf-8", errors="strict")
    normalized = re.sub(
        r"(?m)(\d+ passed(?:, \d+ skipped)?)(?: in [0-9.]+s)?$",
        r"\1 in <elapsed>",
        decoded,
    )
    return normalized.encode("utf-8")


def _run_gate(repository_root: Path, destination: Path) -> M7GateEvidence:
    argv = _recovery_matrix_argv()
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
    raw_output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise M7SignoffError(
            "M7 recovery safety matrix failed: "
            + raw_output.decode("utf-8", errors="replace")[-2000:]
        )
    output = _normalize_gate_output(raw_output)
    decoded = output.decode("utf-8", errors="strict")
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s|$)", decoded)
    if len(matches) != 1 or int(matches[0]) < 1:
        raise M7SignoffError("M7 recovery safety matrix lacks one passing count")
    _publish_immutable(destination, output)
    return M7GateEvidence(
        evidence_id=_GATE_EVIDENCE_ID,
        argv=argv,
        output_relative_path=_GATE_RELATIVE_PATH,
        output_hash=_hash_bytes(output),
        output_size_bytes=len(output),
        passed_test_count=int(matches[0]),
    )


def _verify_gate_evidence(directory: Path, evidence: M7GateEvidence) -> None:
    if evidence.evidence_id != _GATE_EVIDENCE_ID:
        raise M7SignoffError("M7 gate evidence identity changed")
    if evidence.argv != _recovery_matrix_argv():
        raise M7SignoffError("M7 gate command specification changed")
    if evidence.output_relative_path != _GATE_RELATIVE_PATH:
        raise M7SignoffError("M7 gate output path is not canonical")
    relative = Path(evidence.output_relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise M7SignoffError("M7 gate output escapes its packet directory")
    try:
        content = (directory / relative).read_bytes()
    except OSError as error:
        raise M7SignoffError("M7 gate evidence is missing") from error
    if len(content) != evidence.output_size_bytes or _hash_bytes(content) != evidence.output_hash:
        raise M7SignoffError("M7 gate evidence identity changed")
    if f"{evidence.passed_test_count} passed" not in content.decode("utf-8"):
        raise M7SignoffError("M7 gate passing count is not preserved")


def _build_report(
    path_migration_report: Path,
    *,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
    gate: M7GateEvidence,
    commit_sha: str,
    implementation_tree_hash: str,
) -> M7SignoffReport:
    try:
        recovery = verify_path_migration_showcase(path_migration_report)
        m6, m6_hash = verify_m6_dependency_snapshot(
            m6_packet,
            m5_packet=m5_packet,
            m4_packet=m4_packet,
            m3_packet=m3_packet,
            repository_root=repository_root,
            descendant_commit=commit_sha,
        )
    except (M6SignoffError, PathMigrationRecoveryError, OSError, ValueError) as error:
        raise M7SignoffError("M7 dependency reconstruction failed") from error
    try:
        m5 = M5SignoffReport.model_validate_json(m5_packet.read_bytes())
        m5_bundle = M5SearchBundle.model_validate_json(
            (m5_packet.parent / "search-bundle.json").read_bytes()
        )
        candidate = next(
            item
            for item in m5_bundle.candidate_dag.candidates
            if item.candidate_id == recovery.failure.candidate_id
        )
    except (OSError, StopIteration, ValueError) as error:
        raise M7SignoffError("M7 path-migration lineage cannot resolve in M5") from error
    if (
        m5.search_bundle_hash != m5_bundle.bundle_hash
        or recovery.search_bundle_hash != m5_bundle.bundle_hash
        or recovery.run_id != m5_bundle.run_id
        or recovery.failure.candidate_id not in m5.valid_negative_candidate_ids
        or recovery.source_hash != candidate.source_hash
    ):
        raise M7SignoffError("M7 path-migration report differs from trusted M5 lineage")
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m6_commit_sha": m6.commit_sha,
        "m6_packet_hash": m6_hash,
        "m6_report_hash": m6.report_hash,
        "run_id": recovery.run_id,
        "search_bundle_hash": recovery.search_bundle_hash,
        "source_hash": recovery.source_hash,
        "recovery_policy_hash": recovery.policy_hash,
        "recovery_implementation_hash": _implementation_hash(repository_root),
        "path_migration_report_hash": recovery.report_hash,
        "failure_event_hash": canonical_sha256(recovery.failure),
        "repair_directive_hash": canonical_sha256(recovery.directive),
        "candidate_failure_fingerprint_hash": canonical_sha256(recovery.fingerprint),
        "recovery_decision_hash": canonical_sha256(recovery.decision),
        "failure_family": recovery.failure.failure_family,
        "recovery_action": recovery.decision.action,
        "next_target_cone_fingerprint": recovery.next_target_cone_fingerprint,
        "next_operation_family": recovery.next_operation_family,
        "deterministic_only": True,
        "safety_matrix_evidence": gate,
    }
    input_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "status"}
    }
    payload["input_set_hash"] = _input_set_hash(input_payload)
    provisional = M7SignoffReport.model_construct(
        **payload, report_hash="sha256:" + "0" * 64
    )
    return M7SignoffReport(
        **payload,
        report_hash=canonical_sha256(provisional, exclude=frozenset({"report_hash"})),
    )


def run_m7_signoff(
    path_migration_report: Path,
    *,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> tuple[Path, M7SignoffReport]:
    root = repository_root.resolve(strict=True)
    recovery_path = path_migration_report.resolve(strict=True)
    commit, tree = _clean_checkpoint(root)
    gate = _run_gate(root, recovery_path.parent / _GATE_RELATIVE_PATH)
    report = _build_report(
        recovery_path,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        gate=gate,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    destination = recovery_path.parent / "m7-signoff.json"
    _publish_immutable(destination, canonical_json_bytes(report) + b"\n")
    verify_m7_signoff(
        destination,
        path_migration_report=recovery_path,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
    )
    return destination, report


def verify_m7_signoff(
    report_path: Path,
    *,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path = Path("."),
) -> M7SignoffReport:
    try:
        resolved = report_path.resolve(strict=True)
        observed = M7SignoffReport.model_validate_json(resolved.read_bytes())
        _verify_gate_evidence(resolved.parent, observed.safety_matrix_evidence)
    except (OSError, UnicodeError, ValueError) as error:
        raise M7SignoffError("M7 sign-off report is missing or invalid") from error
    commit, tree = _clean_checkpoint(repository_root)
    expected = _build_report(
        path_migration_report.resolve(strict=True),
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=repository_root.resolve(strict=True),
        gate=observed.safety_matrix_evidence,
        commit_sha=commit,
        implementation_tree_hash=tree,
    )
    if observed != expected:
        raise M7SignoffError("M7 sign-off differs from reconstructed evidence")
    return observed


__all__ = ["M7SignoffError", "run_m7_signoff", "verify_m7_signoff"]
