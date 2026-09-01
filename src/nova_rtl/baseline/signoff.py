"""Fail-closed M2 trustworthy full-baseline sign-off."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

from nova_rtl.analysis_views.aggregation import (
    aggregate_required_views,
    is_complete_measured_timing_violation,
)
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.replay import replay_digest, replay_run
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.execution import _runtime
from nova_rtl.baseline.flow import (
    BaselineFlowError,
    _load_stage_results,
    inspect_baseline_run,
    load_run_index,
)
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.benchmark import (
    BenchmarkSnapshot,
    CalibrationReport,
    M2SignoffReport,
)
from nova_rtl.contracts.execution import PreparedCommand, StageResult
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.contracts.verification import ConstraintBindingManifest, FormalModelContract
from nova_rtl.signoff.m1 import M1SignoffReport, verify_m1_signoff_packet

_REQUIRED_STAGE_IDS = frozenset(
    {
        "stage_yosys",
        "stage_binding",
        "stage_clock",
        "stage_cdc",
        "stage_formal_preflight",
        "stage_simulation",
        "stage_eqy_smoke",
        "stage_sby_cdc_properties",
        "stage_opensta_asap7_setup",
        "stage_opensta_asap7_hold",
        "stage_openroad_asap7_setup",
        "stage_openroad_asap7_hold",
    }
)


class M2SignoffError(BaselineFlowError):
    """The full baseline cannot be signed off as trustworthy."""


def _hash_bytes(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


def _git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise M2SignoffError("Git is required for M2 sign-off")
    return Path(executable).resolve(strict=True)


def _git_environment(executable: Path) -> dict[str, str]:
    return {
        "PATH": f"{executable.parent}:/usr/local/bin:/usr/bin:/bin",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LANG": "C",
        "LC_ALL": "C",
    }


def _git_output(repository_root: Path, *arguments: str) -> str:
    executable = _git_executable()
    completed = subprocess.run(
        (str(executable), "-C", str(repository_root), *arguments),
        env=_git_environment(executable),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise M2SignoffError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _clean_commit(repository_root: Path) -> str:
    root = repository_root.resolve(strict=True)
    if Path(__file__).resolve(strict=True) != root / "src/nova_rtl/baseline/signoff.py":
        raise M2SignoffError("loaded NOVA package is outside the requested NOVA repository")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise M2SignoffError("M2 sign-off requires a clean final commit")
    commit = _git_output(root, "rev-parse", "--verify", "HEAD")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise M2SignoffError("M2 sign-off could not resolve a canonical Git commit")
    return commit


def _implementation_tree(repository_root: Path, commit: str) -> str:
    tree = _git_output(repository_root, "rev-parse", "--verify", f"{commit}^{{tree}}")
    if len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
        raise M2SignoffError("M2 sign-off could not resolve a canonical Git tree")
    return tree


def _verified_m1_dependency(
    repository_root: Path,
    m1_packet: Path,
    current_commit: str,
) -> tuple[M1SignoffReport, str]:
    report = verify_m1_signoff_packet(m1_packet, project_root=repository_root)
    executable = _git_executable()
    ancestry = subprocess.run(
        (
            str(executable),
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            report.implementation_commit,
            current_commit,
        ),
        env=_git_environment(executable),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if ancestry.returncode != 0:
        raise M2SignoffError("verified M1 checkpoint is not an ancestor of M2")
    report_bytes = (m1_packet.resolve(strict=True) / "m1-signoff.json").read_bytes()
    return report, _hash_bytes(report_bytes)


def _require_calibration_run_identity(
    calibration_directory: Path,
    project_root: Path,
) -> None:
    try:
        calibration = calibration_directory.resolve(strict=True)
        snapshot_root = project_root.resolve(strict=True)
    except OSError as error:
        raise M2SignoffError(
            "calibration directory is not the calibration that produced the analyzed snapshot"
        ) from error
    if snapshot_root.name != "repeat-selected" or snapshot_root.parent != calibration:
        raise M2SignoffError(
            "calibration directory is not the calibration that produced the analyzed snapshot"
        )


def _require_calibration_tool_identity(
    observed: ToolFingerprint,
    expected: ToolFingerprint,
) -> None:
    if observed != expected or observed.tool_id != "yosys":
        raise M2SignoffError("calibration Yosys identity differs from the verified toolchain")


def _require_benchmark_source_identity(
    repository_root: Path,
    snapshot: BenchmarkSnapshot,
    workload_scale: int,
) -> None:
    config = load_benchmark_config(
        repository_root / "benchmark/generator/benchmark.yaml", "full"
    ).model_copy(update={"workload_scale": workload_scale})
    with tempfile.TemporaryDirectory(prefix="nova-m2-source-identity-") as temporary:
        expected = generate_benchmark(config, Path(temporary) / "full")
    observed_identity = (
        snapshot.config_hash,
        snapshot.template_hash,
        snapshot.source_hash,
        snapshot.constraint_contract_hash,
        snapshot.clock_inventory_hash,
        snapshot.cdc_inventory_hash,
        snapshot.formal_manifest_hash,
        snapshot.power_workload_hash,
        snapshot.expected_master_clocks,
        snapshot.expected_generated_per_master,
        snapshot.expected_generated_total,
    )
    expected_identity = (
        expected.config_hash,
        expected.template_hash,
        expected.source_hash,
        expected.constraint_contract_hash,
        expected.clock_inventory_hash,
        expected.cdc_inventory_hash,
        expected.formal_manifest_hash,
        expected.power_workload_hash,
        expected.expected_master_clocks,
        expected.expected_generated_per_master,
        expected.expected_generated_total,
    )
    if observed_identity != expected_identity:
        raise M2SignoffError(
            "analyzed full snapshot differs from the current committed benchmark generator"
        )


def _publish_report_atomic(destination: Path, content: bytes) -> None:
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
    except OSError as error:
        raise M2SignoffError("failed to publish M2 sign-off report atomically") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _contract_from_stage(
    result: StageResult,
    store: ArtifactStore,
    model,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    references = tuple(
        item for item in result.raw_artifacts if item.artifact_id.endswith("_contract")
    )
    if len(references) != 1:
        raise M2SignoffError(
            f"stage {result.stage_result_id} lacks exactly one canonical contract artifact"
        )
    try:
        return model.model_validate_json(store.open_verified(references[0]).read())
    except ValueError as error:
        raise M2SignoffError(
            f"stage {result.stage_result_id} contains an invalid contract"
        ) from error


def _calibration_evidence(
    calibration_directory: Path,
    snapshot: BenchmarkSnapshot,
) -> tuple[CalibrationReport, str]:
    try:
        report_bytes = (calibration_directory / "calibration-report.json").read_bytes()
        validation_bytes = (calibration_directory / "calibration-validation.json").read_bytes()
        report = CalibrationReport.model_validate_json(report_bytes)
        validation = json.loads(validation_bytes)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise M2SignoffError("full calibration evidence is missing or invalid") from error
    if report.status != "PASS" or report.selected_workload_scale is None:
        raise M2SignoffError("full calibration did not select an in-range sample")
    selected = next(
        (item for item in report.samples if item.workload_scale == report.selected_workload_scale),
        None,
    )
    if selected is None or not (
        report.target.minimum <= selected.mapped_cell_count <= report.target.maximum
    ):
        raise M2SignoffError("selected calibration sample is outside 45K-55K cells")
    if selected.config_hash != snapshot.config_hash or selected.source_hash != snapshot.source_hash:
        raise M2SignoffError("calibration selection does not identify the analyzed full snapshot")
    if validation.get("status") != "PASS" or validation.get("findings"):
        raise M2SignoffError("selected calibration structural validation is not clean")
    if validation.get("selected_config_hash") != snapshot.config_hash:
        raise M2SignoffError("calibration validation configuration identity differs")
    if validation.get("selected_source_hash") != snapshot.source_hash:
        raise M2SignoffError("calibration validation source identity differs")
    if validation.get("generated_clock_count") != 105:
        raise M2SignoffError("calibration validation does not preserve all 105 clocks")
    if validation.get("active_generated_clock_consumer_count") != 105:
        raise M2SignoffError("calibration validation lacks 105 active clock consumers")
    if validation.get("cdc_instance_count") != 12:
        raise M2SignoffError("calibration validation CDC inventory differs")
    if len(validation.get("timing_violation_family_ids", ())) < 2:
        raise M2SignoffError("calibration lacks multiple editable timing-violation families")
    declared_hash = validation.get("evidence_hash")
    payload = {key: value for key, value in validation.items() if key != "evidence_hash"}
    if declared_hash != canonical_sha256(payload):
        raise M2SignoffError("calibration validation evidence hash differs")
    return report, str(declared_hash)


def _prepared_command(result: StageResult, store: ArtifactStore) -> PreparedCommand:
    references = tuple(
        item for item in result.raw_artifacts if item.artifact_id.endswith("_prepared_command")
    )
    if len(references) != 1:
        raise M2SignoffError(f"stage {result.stage_result_id} lacks prepared-command evidence")
    try:
        return PreparedCommand.model_validate_json(store.open_verified(references[0]).read())
    except ValueError as error:
        raise M2SignoffError(
            f"stage {result.stage_result_id} prepared command is invalid"
        ) from error


def _build_report(
    run_directory: Path,
    calibration_directory: Path,
    repository_root: Path,
    commit_sha: str,
    implementation_tree_hash: str,
    m1_report: M1SignoffReport,
    m1_packet_hash: str,
) -> M2SignoffReport:
    evidence = inspect_baseline_run(run_directory)
    index = load_run_index(run_directory)
    if evidence.status != "PASS" or index.status != "PASS":
        raise M2SignoffError("full baseline run is not complete")
    if index.profile != "full" or index.expected_master_clocks != 5:
        raise M2SignoffError("M2 requires the calibrated five-master full profile")
    if index.expected_generated_clocks != 105:
        raise M2SignoffError("M2 requires exactly 105 generated clocks")
    _require_calibration_run_identity(calibration_directory, index.project_root)

    store = ArtifactStore.open_existing(run_directory.resolve() / "artifacts")
    results = _load_stage_results(index, store)
    by_id = {item.stage_result_id: item for item in results}
    missing = sorted(_REQUIRED_STAGE_IDS - set(by_id))
    if missing:
        raise M2SignoffError(f"full baseline is missing stages: {', '.join(missing)}")
    for stage_id in _REQUIRED_STAGE_IDS:
        result = by_id[stage_id]
        if result.status != "PASS" and not is_complete_measured_timing_violation(result):
            raise M2SignoffError(f"required stage {stage_id} is {result.status}")

    snapshot = BenchmarkSnapshot.model_validate_json(
        store.open_verified(index.benchmark_snapshot_artifact).read()
    )
    calibration, calibration_validation_hash = _calibration_evidence(
        calibration_directory.resolve(), snapshot
    )
    _require_benchmark_source_identity(
        repository_root,
        snapshot,
        calibration.selected_workload_scale,
    )
    binding = _contract_from_stage(
        by_id["stage_binding"], store, ConstraintBindingManifest
    )
    clocks = _contract_from_stage(by_id["stage_clock"], store, ClockInventory)
    cdc = _contract_from_stage(by_id["stage_cdc"], store, CDCInventory)
    formal = _contract_from_stage(
        by_id["stage_formal_preflight"], store, FormalModelContract
    )
    if binding.coverage.unresolved_selectors != 0:
        raise M2SignoffError("constraint binding has unresolved selectors")
    if binding.coverage.timed_endpoints + binding.coverage.reviewed_exception_endpoints != (
        binding.coverage.sequential_endpoints_total
    ):
        raise M2SignoffError("constraint endpoint coverage is incomplete")
    if len(clocks.master_clocks) != 5 or len(clocks.generated_clocks) != 105:
        raise M2SignoffError("clock inventory is incomplete")
    if any(item.active_consumer_count <= 0 for item in clocks.generated_clocks):
        raise M2SignoffError("a generated clock lacks active consumers")
    if (
        cdc.new_unapproved_count
        or cdc.changed_approved_structure_count
        or cdc.removed_approved_structure_count
        or cdc.ambiguous_count
    ):
        raise M2SignoffError("CDC inventory differs from the approved registry")
    if formal.behavioral_elaboration_delta != "NONE":
        raise M2SignoffError("formal behavior identity is not NONE")
    if by_id["stage_sby_cdc_properties"].input_hashes.formal_model != canonical_sha256(formal):
        raise M2SignoffError("CDC property execution does not bind the formal preflight")

    grouped: dict[str, dict[str, StageResult]] = {}
    for result in results:
        if result.analysis_view_id is not None:
            grouped.setdefault(result.analysis_view_id, {})[result.stage] = result
    aggregate = aggregate_required_views(grouped, index.analysis_views)

    setup_result = by_id["stage_openroad_asap7_setup"]
    hold_result = by_id["stage_openroad_asap7_hold"]
    setup_result_ref = index.stage_result_artifacts[setup_result.stage_result_id]
    setup_checkpoint = next(
        (item for item in setup_result.raw_artifacts if item.artifact_id.endswith("placed_odb")),
        None,
    )
    if setup_checkpoint is None:
        raise M2SignoffError("setup OpenROAD result lacks the shared physical checkpoint")
    hold_command = _prepared_command(hold_result, store)
    hold_inputs = {item.sha256 for item in hold_command.staged_input_artifact_refs}
    if setup_checkpoint.sha256 not in hold_inputs:
        raise M2SignoffError("hold OpenROAD did not consume the setup physical checkpoint")
    if hold_result.input_hashes.parent_stage_result != setup_result_ref.sha256:
        raise M2SignoffError("hold OpenROAD parent identity is not the setup physical result")

    verified, _, _, _ = _runtime(
        index.project_root,
        index,
        repository_root=repository_root,
    )
    selected_calibration = next(
        item
        for item in calibration.samples
        if item.workload_scale == calibration.selected_workload_scale
    )
    _require_calibration_tool_identity(
        selected_calibration.tool_fingerprint,
        verified.tool_fingerprints["yosys"],
    )
    used_tools = sorted({result.tool_fingerprint.tool_id for result in results})
    tool_hashes: dict[str, str] = {}
    for tool_id in used_tools:
        expected = verified.tool_fingerprints.get(tool_id)
        observed = next(
            result.tool_fingerprint
            for result in results
            if result.tool_fingerprint.tool_id == tool_id
        )
        if expected is None or observed != expected:
            raise M2SignoffError(f"stage tool identity differs from the verified lock: {tool_id}")
        tool_hashes[tool_id] = observed.build_hash

    stage_hashes = {
        stage_id: index.stage_result_artifacts[stage_id].sha256
        for stage_id in sorted(index.stage_result_artifacts)
    }
    raw_hashes = {
        item.artifact_id: item.sha256
        for result in sorted(results, key=lambda value: value.stage_result_id)
        for item in sorted(result.raw_artifacts, key=lambda value: value.artifact_id)
    }
    views = {
        item.analysis_view_id: canonical_sha256(item)
        for item in sorted(index.analysis_views, key=lambda value: value.analysis_view_id)
    }
    ledger_path = run_directory.resolve() / "experiment-ledger.sqlite3"
    ledger = ExperimentLedger.open_existing(ledger_path, artifact_store=store)
    replay = replay_digest(replay_run(ledger, index.run_id))
    input_payload = {
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m1_implementation_commit": m1_report.implementation_commit,
        "m1_signoff_invocation_hash": m1_report.signoff_invocation_hash,
        "m1_packet_hash": m1_packet_hash,
        "run_index_hash": index.index_hash,
        "benchmark_snapshot_hash": index.benchmark_snapshot_hash,
        "design_contract_hash": index.design_contract_hash,
        "platform_lock_hash": index.platform_lock_hash,
        "calibration_report_hash": calibration.report_hash,
        "calibration_validation_hash": calibration_validation_hash,
        "analysis_view_hashes": views,
        "stage_result_hashes": stage_hashes,
        "raw_artifact_hashes": raw_hashes,
        "tool_build_hashes": dict(sorted(tool_hashes.items())),
    }
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "commit_sha": commit_sha,
        "implementation_tree_hash": implementation_tree_hash,
        "m1_implementation_commit": m1_report.implementation_commit,
        "m1_signoff_invocation_hash": m1_report.signoff_invocation_hash,
        "m1_packet_hash": m1_packet_hash,
        "run_id": index.run_id,
        "profile": "full",
        "expected_master_clocks": 5,
        "expected_generated_clocks": 105,
        **input_payload,
        "constraint_binding_hash": binding.effective_binding_hash,
        "clock_graph_hash": clocks.clock_graph_hash,
        "cdc_inventory_hash": cdc.inventory_hash,
        "formal_model_hash": canonical_sha256(formal),
        "shared_physical_checkpoint_hash": setup_checkpoint.sha256,
        "setup_wns_ns": aggregate.setup_wns_ns,
        "setup_tns_ns": aggregate.setup_tns_ns,
        "hold_wns_ns": aggregate.hold_wns_ns,
        "hold_tns_ns": aggregate.hold_tns_ns,
        "timing_constraint_status": (
            "FAIL" if aggregate.setup_wns_ns < 0.0 or aggregate.hold_wns_ns < 0.0 else "PASS"
        ),
        "replay_digest": replay,
        "ledger_hash": _hash_bytes(ledger_path.read_bytes()),
        "input_set_hash": canonical_sha256(input_payload),
    }
    return M2SignoffReport(
        **payload,
        report_hash=canonical_sha256(payload),
    )


def run_m2_signoff(
    run_directory: Path,
    calibration_directory: Path,
    m1_packet: Path,
    *,
    repository_root: Path = Path("."),
) -> tuple[Path, M2SignoffReport]:
    """Build and immediately re-verify the final commit-bound M2 packet."""

    repository_root = repository_root.resolve(strict=True)
    commit = _clean_commit(repository_root)
    tree = _implementation_tree(repository_root, commit)
    m1_report, m1_packet_hash = _verified_m1_dependency(
        repository_root, m1_packet, commit
    )
    report = _build_report(
        run_directory.resolve(strict=True),
        calibration_directory.resolve(strict=True),
        repository_root,
        commit,
        tree,
        m1_report,
        m1_packet_hash,
    )
    path = run_directory.resolve() / "m2-signoff.json"
    _publish_report_atomic(path, canonical_json_bytes(report))
    verified = verify_m2_signoff(
        path,
        calibration_directory=calibration_directory,
        m1_packet=m1_packet,
        repository_root=repository_root,
    )
    return path, verified


def verify_m2_signoff(
    report_path: Path,
    *,
    calibration_directory: Path,
    m1_packet: Path,
    repository_root: Path = Path("."),
) -> M2SignoffReport:
    """Recompute every M2 identity and reject a stale or incomplete packet."""

    try:
        observed = M2SignoffReport.model_validate_json(
            report_path.resolve(strict=True).read_bytes()
        )
    except (OSError, ValueError) as error:
        raise M2SignoffError("M2 sign-off report is missing or invalid") from error
    commit = _clean_commit(repository_root.resolve(strict=True))
    if observed.commit_sha != commit:
        raise M2SignoffError("M2 sign-off report is bound to a different commit")
    tree = _implementation_tree(repository_root.resolve(strict=True), commit)
    m1_report, m1_packet_hash = _verified_m1_dependency(
        repository_root.resolve(strict=True), m1_packet, commit
    )
    expected = _build_report(
        report_path.resolve().parent,
        calibration_directory.resolve(strict=True),
        repository_root.resolve(strict=True),
        commit,
        tree,
        m1_report,
        m1_packet_hash,
    )
    if observed != expected:
        raise M2SignoffError("M2 sign-off report differs from recomputed evidence")
    return observed


__all__ = [
    "M2SignoffError",
    "run_m2_signoff",
    "verify_m2_signoff",
]
