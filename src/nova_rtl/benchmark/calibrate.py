"""Deterministic bounded search for the full benchmark mapped-cell target."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp

import yaml

from nova_rtl.adapters.base import AdapterInvocation, execute_tool_job
from nova_rtl.adapters.opensta import OpenSTAAdapter
from nova_rtl.adapters.yosys import YosysAdapter
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.execution import _asap7_techmap
from nova_rtl.baseline.flow import _rtl_bundle
from nova_rtl.benchmark.calibration_validation import inspect_full_mapped_design
from nova_rtl.benchmark.generator import generate_benchmark
from nova_rtl.contracts.base import (
    ArtifactRef,
    StageInputHashes,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import (
    BenchmarkConfig,
    CalibrationReport,
    CalibrationSample,
    MappedCellTarget,
)
from nova_rtl.contracts.execution import ResourceLimits, StageResult, ToolJob
from nova_rtl.contracts.manifest import ProjectManifest
from nova_rtl.contracts.platform import PlatformLock
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import verify_platform_lock

MAX_CALIBRATION_SAMPLES = 12
Measurement = Callable[[BenchmarkConfig], CalibrationSample]


class CalibrationError(RuntimeError):
    """Calibration could not produce trustworthy in-range evidence."""

    def __init__(self, message: str, *, report: CalibrationReport | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class CalibrationRunResult:
    """Published calibration evidence returned to the CLI."""

    report: CalibrationReport
    report_artifact: ArtifactRef
    output: Path
    validation: SelectedCalibrationValidation | None = None


@dataclass(frozen=True)
class SelectedCalibrationValidation:
    """Published selected-sample structural, timing, and repeatability evidence."""

    status: str
    timing_violation_family_ids: tuple[str, ...]
    evidence_artifact: ArtifactRef
    evidence_hash: str


def build_calibration_report(
    target: MappedCellTarget,
    samples: Iterable[CalibrationSample],
    *,
    budget_exhausted: bool = False,
) -> CalibrationReport:
    """Select the first in-range sample and bind the bounded evidence to a hash."""

    ordered = tuple(samples)
    selected = next(
        (
            sample.workload_scale
            for sample in ordered
            if target.minimum <= sample.mapped_cell_count <= target.maximum
        ),
        None,
    )
    status = (
        "PASS"
        if selected is not None
        else "BUDGET_EXHAUSTED"
        if budget_exhausted
        else "TARGET_NOT_REACHED"
    )
    payload = {
        "schema_version": 1,
        "target": target,
        "samples": ordered,
        "selected_workload_scale": selected,
        "status": status,
    }
    hash_payload = {
        **payload,
        "target": target.model_dump(mode="json"),
        "samples": tuple(sample.model_dump(mode="json") for sample in ordered),
    }
    return CalibrationReport(**payload, report_hash=canonical_sha256(hash_payload))


def _next_scale(
    samples: tuple[CalibrationSample, ...],
    target: MappedCellTarget,
) -> int | None:
    """Choose the next unique integer scale using monotonic bracketing."""

    tried = {sample.workload_scale for sample in samples}
    below = tuple(sample for sample in samples if sample.mapped_cell_count < target.minimum)
    above = tuple(sample for sample in samples if sample.mapped_cell_count > target.maximum)
    if below and above:
        low = max(below, key=lambda item: item.workload_scale).workload_scale
        high = min(above, key=lambda item: item.workload_scale).workload_scale
        if low > high:
            raise CalibrationError("mapped cell observations are not monotonic with workload scale")
        candidates = tuple(
            scale
            for scale in range(low + 1, high)
            if scale not in tried
        )
        if not candidates:
            return None
        midpoint = (low + high) // 2
        return min(candidates, key=lambda scale: (abs(scale - midpoint), scale))
    latest = samples[-1].workload_scale
    if below:
        candidate = latest * 2
    elif above:
        candidate = max(1, latest // 2)
    else:
        return None
    return candidate if candidate not in tried else None


def calibrate_with_measurement(
    base_config: BenchmarkConfig,
    measure: Measurement,
    *,
    max_samples: int = MAX_CALIBRATION_SAMPLES,
) -> CalibrationReport:
    """Calibrate by changing only ``workload_scale`` and measuring each snapshot once."""

    target = base_config.mapped_cell_target
    if target is None:
        raise CalibrationError("calibration requires a mapped-cell target")
    if not 1 <= max_samples <= MAX_CALIBRATION_SAMPLES:
        raise CalibrationError("calibration sample budget must be between one and twelve")

    samples: list[CalibrationSample] = []
    next_scale: int | None = base_config.workload_scale
    while next_scale is not None and len(samples) < max_samples:
        sample_config = base_config.model_copy(update={"workload_scale": next_scale})
        sample = measure(sample_config)
        if sample.workload_scale != next_scale:
            raise CalibrationError("measurement returned the wrong workload scale identity")
        samples.append(sample)
        report = build_calibration_report(target, samples)
        if report.status == "PASS":
            return report
        next_scale = _next_scale(tuple(samples), target)

    report = build_calibration_report(target, samples, budget_exhausted=True)
    raise CalibrationError(
        "calibration exhausted the twelve-sample budget without reaching 45,000-55,000 cells",
        report=report,
    )


def publish_calibration_report(
    store: ArtifactStore,
    report: CalibrationReport,
) -> ArtifactRef:
    """Publish the canonical calibration curve into immutable content-addressed storage."""

    return store.put_named_bytes(
        canonical_json_bytes(report),
        artifact_id="calibration_report",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _verified_runtime(repository_root: Path):  # type: ignore[no-untyped-def]
    loaded = load_toolchain_source_manifest(
        repository_root / "config/platform/toolchain-sources.json"
    )
    verified = verify_toolchain(loaded.manifest, repository_root / loaded.manifest.tool_root_name)
    lock = PlatformLock.model_validate(
        yaml.safe_load(
            (repository_root / "config/platform/platform.lock.yaml").read_text(encoding="utf-8")
        )
    )
    orfs_root = verified.root / "components/orfs"
    lock_check = verify_platform_lock(
        lock,
        artifact_root=orfs_root,
        tool_paths=verified.tool_paths,
    )
    if lock_check.status != "PASS":
        codes = ", ".join(item.code for item in lock_check.issues)
        raise CalibrationError(f"pinned ASAP7 platform verification failed: {codes}")
    environment = {
        name: value
        for name, value in verified.execution_environment().items()
        if name
        in {
            "LD_LIBRARY_PATH",
            "TCLLIBPATH",
            "VERILATOR_ROOT",
            "GHDL_PREFIX",
            "PYTHONDONTWRITEBYTECODE",
        }
    }
    return verified, lock, orfs_root, environment


def _locked_liberty_inputs(
    store: ArtifactStore,
    lock: PlatformLock,
    orfs_root: Path,
) -> tuple[ArtifactRef, ...]:
    inputs: list[ArtifactRef] = []
    for index, locked in enumerate(lock.setup_corner.liberty_files):
        path = (orfs_root / locked.logical_path).resolve()
        if orfs_root.resolve() not in path.parents or not path.is_file() or path.is_symlink():
            raise CalibrationError(f"locked Liberty is unavailable: {locked.logical_path}")
        content = path.read_bytes()
        if locked.sha256 != f"sha256:{sha256(content).hexdigest()}":
            raise CalibrationError(f"locked Liberty hash changed: {locked.artifact_id}")
        if path.suffix == ".gz":
            content = gzip.decompress(content)
        inputs.append(
            store.put_named_bytes(
                content,
                artifact_id=f"calibration_setup_liberty_{index:02d}",
                media_type="application/x-liberty",
                classification="INTERNAL",
                producer_stage_result_id=None,
            )
        )
    return tuple(inputs)


def _calibration_recipe(
    liberty_inputs: tuple[ArtifactRef, ...],
    *,
    bundle_id: str,
    techmap_id: str,
) -> str:
    lines = [
        *(f"read_liberty -lib inputs/{item.artifact_id}" for item in liberty_inputs),
        f"read_verilog -sv inputs/{bundle_id}",
        "hierarchy -check -top nebula_top",
        "flatten",
        "hierarchy -check -top nebula_top",
        "synth -top nebula_top -noabc",
        "dfflibmap "
        + " ".join(f"-liberty inputs/{item.artifact_id}" for item in liberty_inputs),
        f"techmap -map inputs/{techmap_id}",
        "clean",
        "log NOVA_YOSYS_AREA_FROM_LOCKED_CELL_COUNTS",
        "stat -top nebula_top",
        "write_verilog -noattr mapped_netlist.v",
        "write_json mapped_design.json",
    ]
    return "\n".join(lines) + "\n"


def _timing_recipe(
    liberty_inputs: tuple[ArtifactRef, ...],
    *,
    netlist_id: str,
    constraint_id: str,
) -> str:
    lines = [
        *(f"read_liberty inputs/{item.artifact_id}" for item in liberty_inputs),
        f"read_verilog inputs/{netlist_id}",
        "link_design nebula_top",
        "set_cmd_units -time ns",
        f"read_sdc inputs/{constraint_id}",
        "foreach {nova_family nova_clock} {",
        "  family_0 clk_ingress",
        "  family_1 clk_schedule",
        "  family_2 clk_dma",
        "  family_3 clk_compute",
        "  family_4 clk_control",
        "} {",
        '  puts "NOVA_TIMING_FAMILY_BEGIN $nova_family"',
        "  report_checks -from [get_clocks $nova_clock] -path_delay max "
        "-group_path_count 3 -digits 6",
        '  puts "NOVA_TIMING_FAMILY_END $nova_family"',
        "}",
        'set nova_wns [sta::time_sta_ui [sta::worst_slack_cmd "max"]]',
        'set nova_tns [sta::time_sta_ui [sta::total_negative_slack_cmd "max"]]',
        "if {$nova_wns > 1e20} {set nova_wns 0.0}",
        "if {$nova_tns > 1e20} {set nova_tns 0.0}",
        "set nova_failing [expr {$nova_wns < 0.0 ? 1 : 0}]",
        "set nova_delay [expr {10.0 - $nova_wns}]",
        "if {$nova_delay <= 0.0} {set nova_delay 0.001}",
        "puts NOVA_OPENSTA_SUMMARY",
        'puts "check: SETUP"',
        'puts "failing_endpoints: $nova_failing"',
        'puts "critical_path_delay_ns: $nova_delay"',
        'puts "setup_wns_ns: $nova_wns"',
        'puts "setup_tns_ns: $nova_tns"',
        'puts "estimated_fmax_mhz: [expr {1000.0 / $nova_delay}]"',
        "puts NOVA_OPENSTA_END",
        "exit",
    ]
    return "\n".join(lines) + "\n"


def _timing_violation_families(stdout: str) -> tuple[str, ...]:
    families = []
    for family in (f"family_{index}" for index in range(5)):
        match = re.search(
            rf"NOVA_TIMING_FAMILY_BEGIN {family}(.*?)NOVA_TIMING_FAMILY_END {family}",
            stdout,
            re.DOTALL,
        )
        if match is not None and "(VIOLATED)" in match.group(1):
            families.append(family)
    return tuple(families)


def _measure_with_locked_yosys(
    config: BenchmarkConfig,
    *,
    work_root: Path,
    store: ArtifactStore,
    lock: PlatformLock,
    liberty_inputs: tuple[ArtifactRef, ...],
    environment: dict[str, str],
    yosys_fingerprint,  # type: ignore[no-untyped-def]
) -> CalibrationSample:
    snapshot_root = work_root / "snapshots" / f"scale-{config.workload_scale:06d}"
    snapshot = generate_benchmark(config, snapshot_root)
    project = ProjectManifest.model_validate(
        yaml.safe_load((snapshot_root / "project.yaml").read_text(encoding="utf-8"))
    )
    bundle = store.put_named_bytes(
        _rtl_bundle(snapshot_root, project),
        artifact_id="calibration_rtl_bundle",
        media_type="text/x-systemverilog",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    techmap = store.put_named_bytes(
        _asap7_techmap().encode("utf-8"),
        artifact_id="calibration_asap7_techmap",
        media_type="text/x-verilog",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    recipe_text = _calibration_recipe(
        liberty_inputs,
        bundle_id=bundle.artifact_id,
        techmap_id=techmap.artifact_id,
    )
    recipe = store.put_named_bytes(
        recipe_text.encode("utf-8"),
        artifact_id="calibration_yosys_recipe",
        media_type="text/plain",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    stage_id = f"stage_calibration_scale_{config.workload_scale:06d}"
    requested = datetime.now(UTC)
    design_identity = canonical_sha256(
        {"purpose": "full_benchmark_calibration", "config_hash": snapshot.config_hash}
    )
    input_hashes = StageInputHashes(
        rtl_snapshot=snapshot.source_hash,
        design_contract=design_identity,
        constraints=None,
        constraint_binding=None,
        analysis_view=None,
        power_activity=None,
        platform_lock=canonical_sha256(lock),
        tool_recipe=recipe.sha256,
        formal_model=None,
        parent_stage_result=None,
        extensions={},
    )
    job = ToolJob(
        tool_job_id=f"job_calibration_{config.workload_scale:06d}_{snapshot.source_hash[7:15]}",
        stage_result_id=stage_id,
        run_id="run_full_benchmark_calibration",
        candidate_id=f"candidate_scale_{config.workload_scale:06d}",
        stage="YOSYS_SYNTH",
        analysis_view_id=None,
        design_contract_hash=design_identity,
        input_artifact_refs=(*liberty_inputs, bundle, techmap, recipe),
        input_hashes=input_hashes,
        resource_limits=ResourceLimits(
            cpu_cores=2,
            memory_bytes=16 * 1024 * 1024 * 1024,
            wall_time_ms=15 * 60 * 1000,
            max_output_bytes=512 * 1024 * 1024,
        ),
        deadline=requested + timedelta(minutes=16),
        artifact_namespace=f"artifact://calibration/scale-{config.workload_scale:06d}/",
        requested_at=requested,
    )
    adapter = YosysAdapter(
        tool_fingerprint=yosys_fingerprint,
        artifact_store=store,
        workspace_root=work_root / "workspaces",
        invocation=AdapterInvocation(
            argv_tail=("-s", f"inputs/{recipe.artifact_id}"),
            expected_output_paths=("mapped_netlist.v", "mapped_design.json"),
            environment=environment,
        ),
    )
    result = execute_tool_job(adapter, job, store)
    result_ref = adapter.last_stage_result_artifact
    if result.status != "PASS" or result_ref is None:
        raise CalibrationError(
            f"locked Yosys calibration sample {config.workload_scale} failed as {result.status}"
        )
    if result.metrics.cell_count is None or result.metrics.cell_count <= 0:
        raise CalibrationError("locked Yosys result lacks a positive mapped-cell count")
    return CalibrationSample(
        workload_scale=config.workload_scale,
        mapped_cell_count=result.metrics.cell_count,
        config_hash=snapshot.config_hash,
        source_hash=snapshot.source_hash,
        snapshot_hash=snapshot.snapshot_hash,
        recipe_hash=recipe.sha256,
        tool_fingerprint=yosys_fingerprint,
        stage_result_artifact=result_ref,
    )


def _publish_run_files(
    root: Path,
    store: ArtifactStore,
    report: CalibrationReport,
) -> ArtifactRef:
    reference = publish_calibration_report(store, report)
    report_path = root / "calibration-report.json"
    report_path.write_bytes(canonical_json_bytes(report))
    os.chmod(report_path, 0o444)
    receipt_path = root / "calibration-artifact.json"
    receipt_path.write_bytes(canonical_json_bytes(reference))
    os.chmod(receipt_path, 0o444)
    return reference


def validate_selected_calibration(
    config: BenchmarkConfig,
    calibration_root: Path,
) -> SelectedCalibrationValidation:
    """Validate selected mapped structure, repeat identities, and per-family setup violations."""

    root = calibration_root.resolve(strict=True)
    store = ArtifactStore(root / "artifacts")
    report = CalibrationReport.model_validate_json((root / "calibration-report.json").read_bytes())
    selected = next(
        (
            sample
            for sample in report.samples
            if sample.workload_scale == report.selected_workload_scale
        ),
        None,
    )
    if report.status != "PASS" or selected is None:
        raise CalibrationError("selected calibration sample is unavailable")
    if config.workload_scale != selected.workload_scale:
        raise CalibrationError(
            "calibration validation configuration scale "
            f"{config.workload_scale} does not match selected scale "
            f"{selected.workload_scale}"
        )
    stage = StageResult.model_validate_json(
        store.open_verified(selected.stage_result_artifact).read()
    )
    mapped_design_ref = next(
        (
            item
            for item in stage.raw_artifacts
            if item.artifact_id.endswith("mapped_design_json")
        ),
        None,
    )
    mapped_netlist_ref = next(
        (
            item
            for item in stage.raw_artifacts
            if item.artifact_id.endswith("mapped_netlist_v")
        ),
        None,
    )
    if mapped_design_ref is None or mapped_netlist_ref is None:
        raise CalibrationError("selected Yosys evidence lacks mapped design artifacts")
    mapped_design = json.loads(store.open_verified(mapped_design_ref).read())
    structure = inspect_full_mapped_design(mapped_design, config)

    repeat_root = root / "repeat-selected"
    repeat = generate_benchmark(config, repeat_root)
    identity_findings = []
    if repeat.config_hash != selected.config_hash:
        identity_findings.append("repeated selected configuration hash changed")
    if repeat.source_hash != selected.source_hash:
        identity_findings.append("repeated selected RTL source hash changed")

    verified, lock, orfs_root, environment = _verified_runtime(_repository_root())
    liberty_inputs = _locked_liberty_inputs(store, lock, orfs_root)
    constraint = store.put_named_bytes(
        (repeat_root / "constraints/nebula.sdc").read_bytes(),
        artifact_id="calibration_selected_sdc",
        media_type="application/x-sdc",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    recipe_text = _timing_recipe(
        liberty_inputs,
        netlist_id=mapped_netlist_ref.artifact_id,
        constraint_id=constraint.artifact_id,
    )
    recipe = store.put_named_bytes(
        recipe_text.encode("utf-8"),
        artifact_id="calibration_opensta_recipe",
        media_type="text/plain",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    requested = datetime.now(UTC)
    design_identity = canonical_sha256(
        {"purpose": "selected_full_calibration_validation", "config_hash": selected.config_hash}
    )
    binding_identity = canonical_sha256(
        {"constraint_hash": constraint.sha256, "mapped_design_hash": mapped_design_ref.sha256}
    )
    view_identity = canonical_sha256(
        {
            "corner_id": lock.setup_corner.corner_id,
            "liberty_hashes": tuple(item.sha256 for item in lock.setup_corner.liberty_files),
        }
    )
    timing_job = ToolJob(
        tool_job_id=f"job_selected_timing_{selected.source_hash[7:15]}",
        stage_result_id="stage_selected_full_opensta",
        run_id="run_full_benchmark_calibration",
        candidate_id=f"candidate_scale_{selected.workload_scale:06d}",
        stage="OPENSTA_FULL",
        analysis_view_id="asap7_wc_setup",
        design_contract_hash=design_identity,
        input_artifact_refs=(*liberty_inputs, mapped_netlist_ref, constraint, recipe),
        input_hashes=StageInputHashes(
            rtl_snapshot=selected.source_hash,
            design_contract=design_identity,
            constraints=constraint.sha256,
            constraint_binding=binding_identity,
            analysis_view=view_identity,
            power_activity=None,
            platform_lock=canonical_sha256(lock),
            tool_recipe=recipe.sha256,
            formal_model=None,
            parent_stage_result=selected.stage_result_artifact.sha256,
            extensions={},
        ),
        resource_limits=ResourceLimits(
            cpu_cores=2,
            memory_bytes=16 * 1024 * 1024 * 1024,
            wall_time_ms=15 * 60 * 1000,
            max_output_bytes=512 * 1024 * 1024,
        ),
        deadline=requested + timedelta(minutes=16),
        artifact_namespace="artifact://calibration/selected-timing/",
        requested_at=requested,
    )
    timing_adapter = OpenSTAAdapter(
        tool_fingerprint=verified.tool_fingerprints["opensta"],
        artifact_store=store,
        workspace_root=root / "workspaces",
        invocation=AdapterInvocation(
            argv_tail=(f"inputs/{recipe.artifact_id}",),
            expected_output_paths=(),
            environment=environment,
        ),
    )
    timing = execute_tool_job(timing_adapter, timing_job, store)
    timing_ref = timing_adapter.last_stage_result_artifact
    if timing_ref is None or timing.status in {"INFRASTRUCTURE_ERROR", "INCONCLUSIVE"}:
        raise CalibrationError(
            f"selected full OpenSTA validation failed as {timing.status}"
        )
    timing_stdout_ref = next(
        (item for item in timing.raw_artifacts if item.artifact_id.endswith("_stdout")),
        None,
    )
    if timing_stdout_ref is None:
        raise CalibrationError("selected full OpenSTA validation lacks raw stdout")
    timing_stdout = store.open_verified(timing_stdout_ref).read().decode("utf-8")
    violating_families = _timing_violation_families(timing_stdout)

    findings = [*structure.findings, *identity_findings]
    if len(violating_families) < 2:
        findings.append(
            "setup violations were not observed in at least two editable timing families"
        )
    payload = {
        "schema_version": 1,
        "selected_workload_scale": selected.workload_scale,
        "mapped_cell_count": selected.mapped_cell_count,
        "mapped_cell_target": report.target.model_dump(mode="json"),
        "master_clock_count": structure.master_clock_count,
        "generated_clock_count": structure.generated_clock_count,
        "active_generated_clock_consumer_count": structure.active_consumer_count,
        "cdc_instance_count": structure.cdc_instance_count,
        "challenge_family_ids": structure.challenge_family_ids,
        "challenge_lane_count": structure.challenge_lane_count,
        "timing_violation_family_ids": violating_families,
        "selected_config_hash": selected.config_hash,
        "repeated_config_hash": repeat.config_hash,
        "selected_source_hash": selected.source_hash,
        "repeated_source_hash": repeat.source_hash,
        "mapped_design_artifact": mapped_design_ref.model_dump(mode="json"),
        "timing_stage_result_artifact": timing_ref.model_dump(mode="json"),
        "findings": tuple(sorted(findings)),
        "status": "PASS" if not findings else "FAIL",
    }
    evidence_hash = canonical_sha256(payload)
    evidence = {**payload, "evidence_hash": evidence_hash}
    evidence_ref = store.put_named_bytes(
        canonical_json_bytes(evidence),
        artifact_id="calibration_selected_validation",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )
    evidence_path = root / "calibration-validation.json"
    evidence_path.write_bytes(canonical_json_bytes(evidence))
    os.chmod(evidence_path, 0o444)
    if findings:
        raise CalibrationError(
            "selected full calibration validation failed: " + "; ".join(findings)
        )
    return SelectedCalibrationValidation(
        status="PASS",
        timing_violation_family_ids=violating_families,
        evidence_artifact=evidence_ref,
        evidence_hash=evidence_hash,
    )


def run_full_calibration(
    config: BenchmarkConfig,
    output: Path,
    *,
    max_samples: int = MAX_CALIBRATION_SAMPLES,
) -> CalibrationRunResult:
    """Execute the bounded full-profile calibration with the verified portable toolchain."""

    if config.profile != "full" or config.generated_clocks_per_master != 21:
        raise CalibrationError("full calibration requires the 21-generated-clock full profile")
    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"calibration output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        store = ArtifactStore(temporary / "artifacts")
        repository_root = _repository_root()
        verified, lock, orfs_root, environment = _verified_runtime(repository_root)
        liberty_inputs = _locked_liberty_inputs(store, lock, orfs_root)

        def measure(sample_config: BenchmarkConfig) -> CalibrationSample:
            return _measure_with_locked_yosys(
                sample_config,
                work_root=temporary,
                store=store,
                lock=lock,
                liberty_inputs=liberty_inputs,
                environment=environment,
                yosys_fingerprint=verified.tool_fingerprints["yosys"],
            )

        try:
            report = calibrate_with_measurement(
                config,
                measure,
                max_samples=max_samples,
            )
        except CalibrationError as error:
            if error.report is not None:
                _publish_run_files(temporary, store, error.report)
                os.replace(temporary, output)
                published = True
            raise
        reference = _publish_run_files(temporary, store, report)
        os.replace(temporary, output)
        published = True
        selected_scale = report.selected_workload_scale
        if selected_scale is None:
            raise CalibrationError("passing calibration report lacks a selected workload scale")
        selected_config = config.model_copy(update={"workload_scale": selected_scale})
        validation = validate_selected_calibration(selected_config, output)
        return CalibrationRunResult(
            report=report,
            report_artifact=reference,
            output=output,
            validation=validation,
        )
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


__all__ = [
    "CalibrationError",
    "MAX_CALIBRATION_SAMPLES",
    "CalibrationRunResult",
    "build_calibration_report",
    "calibrate_with_measurement",
    "publish_calibration_report",
    "run_full_calibration",
]
