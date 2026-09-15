"""Fail-closed M9 release packet construction and independent verification."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.release import (
    AblationComparison,
    FinalCandidateSeal,
    FrequencySweepResult,
    M9SignoffReport,
)
from nova_rtl.evaluation.acceptance import AcceptanceResult
from nova_rtl.optimization.council_signoff import verify_m8_dependency_snapshot
from nova_rtl.reports.replay import verify_offline_replay


class M9SignoffError(RuntimeError):
    """The final release inputs cannot support a trustworthy M9 PASS."""


def _hash_file(path: Path) -> str:
    return "sha256:" + sha256(path.resolve(strict=True).read_bytes()).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise M9SignoffError("Git is required for M9 sign-off")
    result = subprocess.run(
        (executable, "-C", str(root), *arguments),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    if result.returncode:
        raise M9SignoffError(f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def assemble_m9_report(**values: object) -> M9SignoffReport:
    """Build the self-hashed canonical packet after all inputs have passed."""

    payload = {"schema_version": 1, "status": "PASS", **values}
    return M9SignoffReport(**payload, report_hash=canonical_sha256(payload))


def _construct(
    release_directory: Path,
    *,
    m8_packet: Path,
    council_directory: Path,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
) -> M9SignoffReport:
    root = repository_root.resolve(strict=True)
    release = release_directory.resolve(strict=True)
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise M9SignoffError("M9 sign-off requires a clean final commit")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    m8, m8_packet_hash = verify_m8_dependency_snapshot(
        m8_packet,
        council_directory=council_directory,
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=root,
        descendant_commit=commit,
    )
    replay = verify_offline_replay(release / "replay")
    seal = FinalCandidateSeal.model_validate_json(
        (release / "final-candidate-seal.json").read_bytes()
    )
    ablation = AblationComparison.model_validate_json(
        (release / "ablation-comparison.json").read_bytes()
    )
    frequency = FrequencySweepResult.model_validate_json(
        (release / "frequency-sweep.json").read_bytes()
    )
    acceptance = AcceptanceResult.model_validate_json(
        (release / "acceptance.json").read_bytes()
    )
    if acceptance.status != "PASS":
        raise M9SignoffError("M9 acceptance gate is not PASS")
    if frequency.fmax_mhz is None or len(frequency.trials) < 2:
        raise M9SignoffError("M9 requires a multi-point setup-and-hold frequency sweep")
    archive = release / "nova_rtl_evidence_bundle.tar.zst"
    gate_evidence = release / "gate-evidence.txt"
    created_at = datetime.fromisoformat(_git(root, "show", "-s", "--format=%cI", commit))
    return assemble_m9_report(
        commit_sha=commit,
        implementation_tree_hash=tree,
        m8_commit_sha=m8.commit_sha,
        m8_packet_hash=m8_packet_hash,
        report_bundle_hash=replay.terminal_state_hash,
        replay_manifest_hash=replay.manifest_hash,
        final_candidate_seal_hash=seal.seal_hash,
        ablation_hash=canonical_sha256(ablation),
        frequency_sweep_hash=frequency.result_hash,
        acceptance_hash=acceptance.result_hash,
        submission_bundle_hash=_hash_file(archive),
        gate_evidence_hash=_hash_file(gate_evidence),
        created_at=created_at,
    )


def run_m9_signoff(
    release_directory: Path,
    *,
    m8_packet: Path,
    council_directory: Path,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
) -> tuple[Path, M9SignoffReport]:
    report = _construct(
        release_directory,
        m8_packet=m8_packet,
        council_directory=council_directory,
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=repository_root,
    )
    path = release_directory.resolve() / "m9-signoff.json"
    path.write_bytes(canonical_json_bytes(report) + b"\n")
    return path, report


def verify_m9_signoff(
    report_path: Path,
    *,
    release_directory: Path,
    m8_packet: Path,
    council_directory: Path,
    m7_packet: Path,
    path_migration_report: Path,
    m6_packet: Path,
    m5_packet: Path,
    m4_packet: Path,
    m3_packet: Path,
    repository_root: Path,
) -> M9SignoffReport:
    recorded = M9SignoffReport.model_validate_json(report_path.read_bytes())
    rebuilt = _construct(
        release_directory,
        m8_packet=m8_packet,
        council_directory=council_directory,
        m7_packet=m7_packet,
        path_migration_report=path_migration_report,
        m6_packet=m6_packet,
        m5_packet=m5_packet,
        m4_packet=m4_packet,
        m3_packet=m3_packet,
        repository_root=repository_root,
    )
    if recorded != rebuilt:
        raise M9SignoffError("M9 packet differs from independently reconstructed evidence")
    return rebuilt


__all__ = [
    "M9SignoffError",
    "assemble_m9_report",
    "run_m9_signoff",
    "verify_m9_signoff",
]
