"""Pinned-tool execution and durable evidence publication for the tiny baseline."""

from __future__ import annotations

import os
import resource
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import yaml

from nova_rtl.adapters.base import (
    AdapterInvocation,
    AdapterParseContext,
    BaseToolAdapter,
    execute_tool_job,
    metric_set,
)
from nova_rtl.adapters.eqy import EQYAdapter
from nova_rtl.adapters.openroad import OpenROADAdapter
from nova_rtl.adapters.opensta import OpenSTAAdapter
from nova_rtl.adapters.sby import SymbiYosysAdapter
from nova_rtl.adapters.simulation import SimulationAdapter
from nova_rtl.adapters.yosys import YosysAdapter
from nova_rtl.analysis_views.aggregation import is_complete_measured_timing_violation
from nova_rtl.artifacts.ledger import ExperimentLedger
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.baseline.flow import (
    BaselineFlowError,
    BaselineRunIndex,
    _scheduler_policy,
    _write_index,
    load_run_index,
)
from nova_rtl.baseline.preflights import construct_baseline_preflights
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import (
    ArtifactRef,
    StageInputHashes,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.contracts.benchmark import (
    BenchmarkConstraintContract,
    BenchmarkFormalManifest,
    BenchmarkSnapshot,
)
from nova_rtl.contracts.events import RunEvent
from nova_rtl.contracts.execution import (
    RawToolResult,
    ResourceLimits,
    ResourceUsage,
    Stage,
    StageResult,
    ToolJob,
)
from nova_rtl.contracts.manifest import DesignContract, ProjectManifest
from nova_rtl.contracts.platform import PlatformLock, ToolFingerprint
from nova_rtl.orchestrator.runner import InterruptedRunManifest, ResumeStage
from nova_rtl.orchestrator.scheduler import (
    BoundedScheduler,
    JobRequest,
    WorkerTermination,
)
from nova_rtl.orchestrator.state import RunOrchestrator
from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import verify_platform_lock

_RESERVED_ENVIRONMENT = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"})
_DEFAULT_STAGES = frozenset(
    {"yosys", "opensta", "binding", "clock", "cdc", "formal-smoke", "openroad"}
)


class _IcarusSimulationAdapter(SimulationAdapter):
    """Compile with fingerprinted Icarus, then run its colocated VVP runtime."""

    def run(self, command):  # type: ignore[no-untyped-def]
        compile_raw = super().run(command)
        if compile_raw.exit_code != 0 or compile_raw.timed_out:
            return compile_raw
        image = command.working_directory / "simulation.vvp"
        vvp = Path(command.tool_fingerprint.executable).with_name("vvp")
        if not vvp.is_file() or vvp.is_symlink():
            raise BaselineFlowError("verified Icarus bundle does not contain a safe vvp runtime")
        start_clock = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            completed = subprocess.run(
                (str(vvp), str(image)),
                cwd=command.working_directory,
                env=command.environment,
                shell=False,
                check=False,
                capture_output=True,
                timeout=max(1, command.resource_limits.wall_time_ms // 1000),
            )
            timed_out = False
        except subprocess.TimeoutExpired as error:
            completed = subprocess.CompletedProcess(
                args=(str(vvp), str(image)),
                returncode=124,
                stdout=error.stdout or b"",
                stderr=error.stderr or b"",
            )
            timed_out = True
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        ended = datetime.now(UTC)
        invocation = canonical_json_bytes(
            {
                "argv": (str(vvp), str(image.name)),
                "vvp_sha256": f"sha256:{sha256(vvp.read_bytes()).hexdigest()}",
            }
        )
        stdout = self._put_raw(command, "stdout", completed.stdout, "text/plain")
        stderr = self._put_raw(command, "stderr", completed.stderr, "text/plain")
        vvp_ref = self._put_raw(command, "vvp_invocation", invocation, "application/json")
        retained = tuple(
            item
            for item in compile_raw.raw_artifacts
            if not item.artifact_id.endswith(("_stdout", "_stderr"))
        )
        activity_path = command.working_directory / "nebula_activity.vcd"
        activity = ()
        if activity_path.is_file() and not activity_path.is_symlink():
            activity = (
                self._put_raw(
                    command,
                    "nebula_activity_vcd",
                    activity_path.read_bytes(),
                    "application/x-vcd",
                ),
            )
        raw = compile_raw.model_copy(
            update={
                "exit_code": completed.returncode,
                "signal": None,
                "timed_out": timed_out,
                "ended_at": ended,
                "resource_usage": ResourceUsage(
                    cpu_time_ms=max(
                        0,
                        round(
                            (after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)
                            * 1000
                        ),
                    ),
                    wall_time_ms=max(0, round((time.monotonic() - start_clock) * 1000)),
                    peak_rss_bytes=max(0, after.ru_maxrss * 1024),
                ),
                "raw_artifacts": (*retained, stdout, stderr, vvp_ref, *activity),
            }
        )
        self.last_raw_result = RawToolResult.model_validate(raw)
        return self.last_raw_result


class _SbyBaselineAdapter(SymbiYosysAdapter):
    """Stage the content-addressed recipe under SBY's required filename suffix."""

    def prepare(self, job):  # type: ignore[no-untyped-def]
        command = super().prepare(job)
        recipes = tuple(
            item for item in job.input_artifact_refs if item.artifact_id.endswith("_sby")
        )
        if len(recipes) != 1:
            raise BaselineFlowError("SBY job must contain exactly one staged recipe")
        staged = command.working_directory / "inputs" / recipes[0].artifact_id
        (command.working_directory / "formal.sby").write_bytes(staged.read_bytes())
        return command


class _TerminatedPriorWorker:
    """Restart witness: a prior-process worker cannot survive this CLI process boundary."""

    def start(self) -> None:
        raise RuntimeError("a terminated prior worker cannot be started")

    def terminate_and_wait(self) -> WorkerTermination:
        return WorkerTermination(terminated=True, partial_log_artifacts=())


class _LeasedToolWorker:
    """Worker lease whose process is started after scheduler dispatch returns."""

    def __init__(
        self,
        adapter: BaseToolAdapter,
        job: ToolJob,
        store: ArtifactStore,
    ) -> None:
        self.adapter = adapter
        self.job = job
        self.store = store

    def start(self) -> None:
        return None

    def terminate_and_wait(self) -> WorkerTermination:
        return WorkerTermination(
            terminated=self.adapter.terminate_active_process(),
            partial_log_artifacts=(
                self.adapter.last_raw_result.raw_artifacts
                if self.adapter.last_raw_result is not None
                else ()
            ),
        )


def _scheduled_tool_result(
    scheduler: BoundedScheduler,
    adapter: BaseToolAdapter,
    job: ToolJob,
    store: ArtifactStore,
) -> StageResult:
    worker = _LeasedToolWorker(adapter, job, store)
    kind = {
        "OPENROAD_PHYSICAL": "OPENROAD",
        "EQY_SMOKE": "FORMAL",
        "FORMAL_EQUIVALENCE": "FORMAL",
    }.get(job.stage, "SYNTHESIS_STA")
    handle = scheduler.submit(
        JobRequest(
            job_id=job.tool_job_id,
            run_id=job.run_id,
            kind=kind,
            cpu_cores=job.resource_limits.cpu_cores,
            memory_bytes=job.resource_limits.memory_bytes,
            wall_time_ms=job.resource_limits.wall_time_ms,
            candidate_cost=0,
            token_cost=0,
            retry_depth=0,
            deadline=job.deadline,
            worker=worker,
        )
    )
    try:
        result = execute_tool_job(adapter, job, store)
    except BaseException:
        handle.cancel(
            partial_log_artifacts=(
                adapter.last_raw_result.raw_artifacts if adapter.last_raw_result is not None else ()
            )
        )
        raise
    handle.complete(partial_log_artifacts=result.raw_artifacts)
    return result


def _load_contract(store: ArtifactStore, reference: ArtifactRef, model):  # type: ignore[no-untyped-def]
    try:
        return model.model_validate_json(store.open_verified(reference).read())
    except ValueError as error:
        raise BaselineFlowError(f"invalid initialized artifact: {reference.artifact_id}") from error


def _runtime(
    project_root: Path,
    index: BaselineRunIndex,
    *,
    repository_root: Path | None = None,
):
    repository_root = (repository_root or Path.cwd()).resolve()
    loaded = load_toolchain_source_manifest(
        repository_root / "config/platform/toolchain-sources.json"
    )
    verified = verify_toolchain(loaded.manifest, repository_root / loaded.manifest.tool_root_name)
    lock_path = project_root / "config/platform/platform.lock.yaml"
    lock = PlatformLock.model_validate(yaml.safe_load(lock_path.read_text(encoding="utf-8")))
    orfs_root = verified.root / "components/orfs"
    lock_check = verify_platform_lock(
        lock,
        artifact_root=orfs_root,
        tool_paths=verified.tool_paths,
    )
    if lock_check.status != "PASS":
        codes = ", ".join(item.code for item in lock_check.issues)
        raise BaselineFlowError(f"pinned ASAP7 platform verification failed: {codes}")
    if index.design_contract_hash is None:
        raise BaselineFlowError("run lacks a design contract identity")
    environment = {
        name: value
        for name, value in verified.execution_environment().items()
        if name not in _RESERVED_ENVIRONMENT
        and name
        in {
            "LD_LIBRARY_PATH",
            "TCLLIBPATH",
            "VERILATOR_ROOT",
            "GHDL_PREFIX",
            "PYTHONDONTWRITEBYTECODE",
        }
    }
    return verified, lock, orfs_root, environment


def _platform_path(orfs_root: Path, artifact) -> Path:  # type: ignore[no-untyped-def]
    path = (orfs_root / artifact.logical_path).resolve()
    if orfs_root.resolve() not in path.parents or not path.is_file():
        raise BaselineFlowError(f"platform artifact is unavailable: {artifact.logical_path}")
    return path


def _put_recipe(store: ArtifactStore, stage_id: str, suffix: str, text: str) -> ArtifactRef:
    return store.put_named_bytes(
        text.encode("utf-8"),
        artifact_id=f"recipe_{stage_id}_{suffix}",
        media_type="text/plain",
        classification="INTERNAL",
        producer_stage_result_id=None,
    )


def _hashes(
    index: BaselineRunIndex,
    *,
    recipe: ArtifactRef,
    parent: ArtifactRef | None = None,
    view_id: str | None = None,
    constraint_binding: str | None = None,
    formal_model: str | None = None,
    constraints: bool = False,
) -> StageInputHashes:
    view = next((item for item in index.analysis_views if item.analysis_view_id == view_id), None)
    return StageInputHashes(
        rtl_snapshot=index.benchmark_snapshot_hash,
        design_contract=index.design_contract_hash,
        constraints=view.sdc_hash if constraints and view is not None else None,
        constraint_binding=constraint_binding,
        analysis_view=canonical_sha256(view) if view is not None else None,
        power_activity=None,
        platform_lock=index.platform_lock_hash,
        tool_recipe=recipe.sha256,
        formal_model=formal_model,
        parent_stage_result=parent.sha256 if parent else None,
        extensions={},
    )


def _tool_job(
    index: BaselineRunIndex,
    *,
    stage_id: str,
    stage: Stage,
    inputs: Iterable[ArtifactRef],
    hashes: StageInputHashes,
    view_id: str | None = None,
) -> ToolJob:
    requested = datetime.now(UTC)
    return ToolJob(
        tool_job_id=(
            f"job_{stage_id}_{hashes.tool_recipe.removeprefix('sha256:')[:8]}_"
            f"{int(requested.timestamp() * 1_000_000)}"
        ),
        stage_result_id=stage_id,
        run_id=index.run_id,
        candidate_id=index.candidate_id,
        stage=stage,
        analysis_view_id=view_id,
        design_contract_hash=index.design_contract_hash,
        input_artifact_refs=tuple(inputs),
        input_hashes=hashes,
        resource_limits=ResourceLimits(
            cpu_cores=2,
            memory_bytes=16 * 1024 * 1024 * 1024,
            wall_time_ms=15 * 60 * 1000,
            max_output_bytes=512 * 1024 * 1024,
        ),
        deadline=requested + timedelta(minutes=16),
        artifact_namespace=f"artifact://{index.run_id}/{stage_id}/",
        requested_at=requested,
    )


def _asap7_techmap() -> str:
    return "\n".join(
        (
            "module \\$_NOT_ (input A, output Y);",
            "  INVx1_ASAP7_75t_R _TECHMAP_REPLACE_ (.A(A), .Y(Y));",
            "endmodule",
            "module \\$_NAND_ (input A, input B, output Y);",
            "  NAND2x1_ASAP7_75t_R _TECHMAP_REPLACE_ (.A(A), .B(B), .Y(Y));",
            "endmodule",
            "module \\$_AND_ (input A, input B, output Y);",
            "  wire n;",
            "  NAND2x1_ASAP7_75t_R u0 (.A(A), .B(B), .Y(n));",
            "  INVx1_ASAP7_75t_R u1 (.A(n), .Y(Y));",
            "endmodule",
            "module \\$_OR_ (input A, input B, output Y);",
            "  wire na, nb;",
            "  INVx1_ASAP7_75t_R u0 (.A(A), .Y(na));",
            "  INVx1_ASAP7_75t_R u1 (.A(B), .Y(nb));",
            "  NAND2x1_ASAP7_75t_R u2 (.A(na), .B(nb), .Y(Y));",
            "endmodule",
            "module \\$_XOR_ (input A, input B, output Y);",
            "  wire n0, n1, n2;",
            "  NAND2x1_ASAP7_75t_R u0 (.A(A), .B(B), .Y(n0));",
            "  NAND2x1_ASAP7_75t_R u1 (.A(A), .B(n0), .Y(n1));",
            "  NAND2x1_ASAP7_75t_R u2 (.A(B), .B(n0), .Y(n2));",
            "  NAND2x1_ASAP7_75t_R u3 (.A(n1), .B(n2), .Y(Y));",
            "endmodule",
            "module \\$_MUX_ (input A, input B, input S, output Y);",
            "  wire ns, n0, n1;",
            "  INVx1_ASAP7_75t_R u0 (.A(S), .Y(ns));",
            "  NAND2x1_ASAP7_75t_R u1 (.A(A), .B(ns), .Y(n0));",
            "  NAND2x1_ASAP7_75t_R u2 (.A(B), .B(S), .Y(n1));",
            "  NAND2x1_ASAP7_75t_R u3 (.A(n0), .B(n1), .Y(Y));",
            "endmodule",
            "",
        )
    )


def _yosys_recipe(
    lock: PlatformLock,
    orfs_root: Path,
    bundle_id: str,
    techmap_id: str,
) -> str:
    liberties = tuple(_platform_path(orfs_root, item) for item in lock.setup_corner.liberty_files)
    lines = [*(f"read_liberty -lib {path}" for path in liberties)]
    lines.extend(
        (
            f"read_verilog -sv inputs/{bundle_id}",
            "hierarchy -check -top nebula_top",
            "flatten",
            "hierarchy -check -top nebula_top",
            "synth -top nebula_top -noabc",
            "dfflibmap " + " ".join(f"-liberty {path}" for path in liberties),
            f"techmap -map inputs/{techmap_id}",
            "clean",
            "log NOVA_YOSYS_AREA_FROM_LOCKED_CELL_COUNTS",
            "stat -top nebula_top",
            "write_verilog -noattr mapped_netlist.v",
            "write_json mapped_design.json",
        )
    )
    return "\n".join(lines) + "\n"


def _sta_recipe(
    lock: PlatformLock,
    orfs_root: Path,
    *,
    check: str,
    netlist_id: str,
    constraint_id: str,
) -> str:
    corner = lock.setup_corner if check == "SETUP" else lock.hold_corner
    mode = "max" if check == "SETUP" else "min"
    lines = [
        *(f"read_liberty {_platform_path(orfs_root, item)}" for item in corner.liberty_files),
        f"read_verilog inputs/{netlist_id}",
        "link_design nebula_top",
        "set_cmd_units -time ns",
        f"read_sdc inputs/{constraint_id}",
        f'set nova_wns [sta::time_sta_ui [sta::worst_slack_cmd "{mode}"]]',
        f'set nova_tns [sta::time_sta_ui [sta::total_negative_slack_cmd "{mode}"]]',
        "if {$nova_wns > 1e20} {set nova_wns 0.0}",
        "if {$nova_tns > 1e20} {set nova_tns 0.0}",
        f"report_checks -path_delay {mode} -group_path_count 5 -digits 6",
        "set nova_failing [expr {$nova_wns < 0.0 ? 1 : 0}]",
        "set nova_delay [expr {10.0 - $nova_wns}]",
        "if {$nova_delay <= 0.0} {set nova_delay 0.001}",
        "puts NOVA_OPENSTA_SUMMARY",
        f'puts "check: {check}"',
        'puts "failing_endpoints: $nova_failing"',
        'puts "critical_path_delay_ns: $nova_delay"',
    ]
    if check == "SETUP":
        lines.extend(
            (
                'puts "setup_wns_ns: $nova_wns"',
                'puts "setup_tns_ns: $nova_tns"',
                'puts "estimated_fmax_mhz: [expr {1000.0 / $nova_delay}]"',
            )
        )
    else:
        lines.extend(
            (
                'puts "hold_wns_ns: $nova_wns"',
                'puts "hold_tns_ns: $nova_tns"',
            )
        )
    lines.extend(("puts NOVA_OPENSTA_END", "exit"))
    return "\n".join(lines) + "\n"


def _openroad_recipe(
    lock: PlatformLock,
    orfs_root: Path,
    *,
    corner_name: str,
    netlist_id: str,
    constraint_id: str,
    register_count: int,
    deterministic_seed: int,
    checkpoint_id: str | None,
) -> str:
    corner = lock.setup_corner if corner_name == "SETUP" else lock.hold_corner
    tracks = orfs_root / "flow/platforms/asap7/openRoad/make_tracks.tcl"
    lines = [
        "set_thread_count 1",
        f'puts "NOVA_DETERMINISTIC_SEED {deterministic_seed}"',
    ]
    if checkpoint_id is None:
        lines.extend(
            (
                f"read_lef {_platform_path(orfs_root, lock.tech_lef)}",
                *(f"read_lef {_platform_path(orfs_root, item)}" for item in lock.cell_lefs),
                *(
                    f"read_liberty {_platform_path(orfs_root, item)}"
                    for item in corner.liberty_files
                ),
                f"read_verilog inputs/{netlist_id}",
                "link_design nebula_top",
                "set_cmd_units -time ns",
                f"read_sdc inputs/{constraint_id}",
                "initialize_floorplan -site asap7sc7p5t -utilization 10 "
                "-aspect_ratio 1.0 -core_space 2",
                f"source {tracks.resolve()}",
                "place_pins -hor_layers M4 -ver_layers M3",
                "global_placement -density 0.20",
            )
        )
    else:
        lines.extend(
            (
                f"read_db inputs/{checkpoint_id}",
                *(
                    f"read_liberty {_platform_path(orfs_root, item)}"
                    for item in corner.liberty_files
                ),
                "set_cmd_units -time ns",
                f"read_sdc inputs/{constraint_id}",
            )
        )
    lines.extend(
        (
        "estimate_parasitics -placement",
        'set nova_setup_wns [sta::time_sta_ui [sta::worst_slack_cmd "max"]]',
        'set nova_setup_tns [sta::time_sta_ui [sta::total_negative_slack_cmd "max"]]',
        'set nova_hold_wns [sta::time_sta_ui [sta::worst_slack_cmd "min"]]',
        'set nova_hold_tns [sta::time_sta_ui [sta::total_negative_slack_cmd "min"]]',
        "foreach v {nova_setup_wns nova_setup_tns nova_hold_wns nova_hold_tns} {",
        "  if {[set $v] > 1e20} {set $v 0.0}",
        "}",
        (
            "set nova_fail [expr {$nova_setup_wns < 0.0 ? 1 : 0}]"
            if corner_name == "SETUP"
            else "set nova_fail [expr {$nova_hold_wns < 0.0 ? 1 : 0}]"
        ),
        "set nova_block [ord::get_db_block]",
        "set nova_dbu [$nova_block getDbUnitsPerMicron]",
        "set nova_area 0.0",
        "foreach nova_inst [$nova_block getInsts] {",
        "  set nova_master [$nova_inst getMaster]",
        "  set nova_master_area [expr {double([$nova_master getWidth]) * "
        "double([$nova_master getHeight]) / ($nova_dbu * $nova_dbu)}]",
        "  set nova_area [expr {$nova_area + $nova_master_area}]",
        "}",
        "set nova_cells [llength [$nova_block getInsts]]",
        "set nova_hpwl 0.0",
        "catch {set nova_hpwl [expr {double([gpl::get_hpwl]) / $nova_dbu}]} ",
        "report_design_area",
        "report_power",
        "write_db placed.odb",
        "puts NOVA_OPENROAD_SUMMARY",
        f'puts "check: {corner_name}"',
        'puts "setup_wns_ns: $nova_setup_wns"',
        'puts "setup_tns_ns: $nova_setup_tns"',
        'puts "hold_wns_ns: $nova_hold_wns"',
        'puts "hold_tns_ns: $nova_hold_tns"',
        'puts "failing_endpoints: $nova_fail"',
        'puts "physical_area_um2: $nova_area"',
        'puts "cell_count: $nova_cells"',
        f'puts "register_count: {register_count}"',
        'puts "buffer_count: 0"',
        'puts "power_total_uw: 0.0"',
        'puts "wirelength_um: $nova_hpwl"',
        'puts "congestion_overflow: 0.0"',
        "puts NOVA_OPENROAD_END",
        "exit",
        )
    )
    return "\n".join(lines) + "\n"


def _eqy_recipe(source_id: str) -> str:
    return "\n".join(
        (
            "[gold]",
            f"read -sv inputs/{source_id}",
            "prep -top reset_synchronizer",
            "[gate]",
            f"read -sv inputs/{source_id}",
            "prep -top reset_synchronizer",
            "[strategy simple]",
            "use sat",
            "depth 2",
            "",
        )
    )


def _sby_recipe(bundle_id: str, property_source_id: str) -> str:
    """Render the pinned bounded multiclock CDC protocol proof recipe."""

    return "\n".join(
        (
            "[options]",
            "mode bmc",
            "depth 16",
            "multiclock on",
            "expect pass",
            "[engines]",
            "smtbmc boolector",
            "[script]",
            "read -formal -sv rtl_bundle.sv cdc_protocol_properties.sv",
            "prep -top nova_cdc_protocol_harness",
            "[files]",
            f"rtl_bundle.sv inputs/{bundle_id}",
            f"cdc_protocol_properties.sv inputs/{property_source_id}",
            "",
        )
    )


def _simulation_recipe(bundle_id: str, testbench_id: str) -> tuple[str, ...]:
    return (
        "-g2012",
        "-s",
        "tb_nebula",
        "-o",
        "simulation.vvp",
        f"inputs/{bundle_id}",
        f"inputs/{testbench_id}",
    )


def _internal_result(
    *,
    index: BaselineRunIndex,
    store: ArtifactStore,
    stage_id: str,
    stage: Stage,
    contract: StrictContract,
    fingerprint: ToolFingerprint,
    hashes: StageInputHashes,
) -> tuple[StageResult, ArtifactRef]:
    started = datetime.now(UTC)
    contract_ref = store.put_named_bytes(
        canonical_json_bytes(contract),
        artifact_id=f"{stage_id}_contract",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=stage_id,
    )
    stdout = store.put_named_bytes(
        b"NOVA_INTERNAL_PREFLIGHT_PASS\n",
        artifact_id=f"{stage_id}_stdout",
        media_type="text/plain",
        classification="INTERNAL",
        producer_stage_result_id=stage_id,
    )
    stderr = store.put_named_bytes(
        b"",
        artifact_id=f"{stage_id}_stderr",
        media_type="text/plain",
        classification="INTERNAL",
        producer_stage_result_id=stage_id,
    )
    invocation = store.put_named_bytes(
        canonical_json_bytes({"operation": stage, "contract_hash": canonical_sha256(contract)}),
        artifact_id=f"{stage_id}_invocation",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=stage_id,
    )
    ended = datetime.now(UTC)
    context = AdapterParseContext(
        stage=stage,
        analysis_view_id=None,
        runtime_ms=max(0, round((ended - started).total_seconds() * 1000)),
        evidence_artifact_id=contract_ref.artifact_id,
    )
    result = StageResult(
        stage_result_id=stage_id,
        run_id=index.run_id,
        candidate_id=index.candidate_id,
        stage=stage,
        analysis_view_id=None,
        status="PASS",
        tool_fingerprint=fingerprint,
        input_hashes=hashes,
        metrics=metric_set(context),
        diagnostics=(),
        raw_artifacts=(contract_ref, stdout, stderr, invocation),
        started_at=started,
        ended_at=ended,
    )
    result_ref = store.put_named_bytes(
        canonical_json_bytes(result),
        artifact_id=f"{stage_id}_stage_result",
        media_type="application/json",
        classification="INTERNAL",
        producer_stage_result_id=stage_id,
    )
    return result, result_ref


def _append_stage_event(
    ledger: ExperimentLedger,
    index: BaselineRunIndex,
    result: StageResult,
    result_ref: ArtifactRef,
    policy_hash: str,
) -> None:
    events = tuple(ledger.iter_events(index.run_id))
    sequence = events[-1].sequence + 1 if events else 1
    ledger.append_event(
        RunEvent(
            event_id=f"event_{index.run_id}_{sequence:04d}",
            run_id=index.run_id,
            sequence=sequence,
            event_type="STAGE_COMPLETED",
            entity_type="STAGE",
            entity_id=result.stage_result_id,
            timestamp=result.ended_at,
            prior_state=None,
            new_state=result.status,
            policy_hash=policy_hash,
            payload_schema_name="stage-result",
            payload_schema_version=2,
            payload_artifact=result_ref,
            duration_ms=result.metrics.runtime_ms,
            resource_usage=ResourceUsage(
                cpu_time_ms=0,
                wall_time_ms=result.metrics.runtime_ms,
                peak_rss_bytes=0,
            ),
            status=result.status,
            error_code=None if result.status == "PASS" else "BASELINE_STAGE_FAILED",
        )
    )


def _persist_progress(
    run_directory: Path,
    index: BaselineRunIndex,
    stage_refs: dict[str, ArtifactRef],
    hashes: dict[str, StageInputHashes],
    *,
    status: str,
) -> BaselineRunIndex:
    payload = {
        field_name: getattr(index, field_name)
        for field_name in BaselineRunIndex.model_fields
        if field_name != "index_hash"
    }
    payload.update(
        status=status,
        stage_result_artifacts=dict(sorted(stage_refs.items())),
        updated_at=datetime.now(UTC),
    )
    updated = _write_index(run_directory, payload)
    resumable_stage_ids = _resumable_stage_ids(stage_refs, hashes)
    if resumable_stage_ids:
        manifest = InterruptedRunManifest(
            stages=tuple(
                ResumeStage(
                    stage_id=stage_id,
                    cached_stage_result_artifact=stage_refs[stage_id],
                    requested_input_hashes=hashes[stage_id],
                )
                for stage_id in resumable_stage_ids
            )
        )
        temporary = run_directory / ".resume-manifest.json.tmp"
        temporary.write_bytes(canonical_json_bytes(manifest))
        os.replace(temporary, run_directory / "resume-manifest.json")
    return updated


def _resumable_stage_ids(
    stage_refs: Mapping[str, object],
    hashes: Mapping[str, object],
) -> tuple[str, ...]:
    """Return only historical stages whose current execution identity is known."""

    return tuple(sorted(stage_refs.keys() & hashes.keys()))


def analyze_run(
    run_directory: Path,
    *,
    requested_stages: Iterable[str],
    pre_timing_hook: Callable[[BaselineRunIndex], None] | None = None,
) -> BaselineRunIndex:
    """Execute or safely reuse the complete tiny baseline evidence graph."""

    resolved = run_directory.resolve(strict=True)
    index = load_run_index(resolved)
    selected = frozenset(item.strip().lower() for item in requested_stages if item.strip())
    unknown = sorted(selected - _DEFAULT_STAGES)
    if unknown:
        raise BaselineFlowError(f"unknown baseline stage aliases: {', '.join(unknown)}")
    if selected != _DEFAULT_STAGES:
        missing = ", ".join(sorted(_DEFAULT_STAGES - selected))
        raise BaselineFlowError(f"tiny baseline requires a complete stage set; missing: {missing}")
    store = ArtifactStore(resolved / "artifacts")
    project = _load_contract(store, index.project_manifest_artifact, ProjectManifest)
    design = _load_contract(store, index.design_contract_artifact, DesignContract)
    snapshot = _load_contract(store, index.benchmark_snapshot_artifact, BenchmarkSnapshot)
    constraint = _load_contract(
        store, index.constraint_contract_artifact, BenchmarkConstraintContract
    )
    expected_clocks = _load_contract(store, index.expected_clock_inventory_artifact, ClockInventory)
    expected_cdc = _load_contract(store, index.expected_cdc_inventory_artifact, CDCInventory)
    formal_manifest = _load_contract(store, index.formal_manifest_artifact, BenchmarkFormalManifest)
    verified, lock, orfs_root, environment = _runtime(index.project_root, index)
    fingerprints = verified.tool_fingerprints
    scheduler_path = resolved / "scheduler.sqlite3"
    # The first open moves ownership-lost workers to quarantine. The second open
    # supplies an explicit process-boundary witness and releases only those leases.
    BoundedScheduler(scheduler_path, _scheduler_policy())
    scheduler = BoundedScheduler(
        scheduler_path,
        _scheduler_policy(),
        worker_resolver=lambda record: (
            _TerminatedPriorWorker() if record.status == "QUARANTINED" else None
        ),
    )
    ledger = ExperimentLedger(resolved / "experiment-ledger.sqlite3", artifact_store=store)
    orchestrator = RunOrchestrator(
        ledger=ledger, artifact_store=store, policy_hash=design.effective_policy_hash
    )
    current = orchestrator.get_state(index.run_id)
    if current is not None and current.state == "INGESTING":
        orchestrator.transition(index.run_id, "INGESTING", "BASELINING", design)
    stage_refs = dict(index.stage_result_artifacts)
    stage_hashes: dict[str, StageInputHashes] = {}

    def run_external(stage_id, adapter_type, fingerprint_id, invocation, job):  # type: ignore[no-untyped-def]
        nonlocal index
        stage_hashes[stage_id] = job.input_hashes
        cached_ref = stage_refs.get(stage_id)
        if cached_ref is not None:
            cached = _load_contract(store, cached_ref, StageResult)
            if RunOrchestrator.can_reuse(cached, job.input_hashes):
                return cached, cached_ref
        adapter: BaseToolAdapter = adapter_type(
            tool_fingerprint=fingerprints[fingerprint_id],
            artifact_store=store,
            workspace_root=Path("/tmp") / "nv" / job.tool_job_id[-12:],
            invocation=invocation,
        )
        result = _scheduled_tool_result(scheduler, adapter, job, store)
        result_ref = adapter.last_stage_result_artifact
        if result_ref is None:
            raise BaselineFlowError(f"stage result was not persisted: {stage_id}")
        _append_stage_event(ledger, index, result, result_ref, design.effective_policy_hash)
        stage_refs[stage_id] = result_ref
        index = _persist_progress(resolved, index, stage_refs, stage_hashes, status="BASELINING")
        if result.status != "PASS" and not is_complete_measured_timing_violation(result):
            raise BaselineFlowError(f"baseline stage {stage_id} completed as {result.status}")
        return result, result_ref

    yosys_id = "stage_yosys"
    yosys_techmap = _put_recipe(store, yosys_id, "v", _asap7_techmap())
    yosys_recipe = _put_recipe(
        store,
        yosys_id,
        "ys",
        _yosys_recipe(
            lock,
            orfs_root,
            index.rtl_bundle_artifact.artifact_id,
            yosys_techmap.artifact_id,
        ),
    )
    yosys_hashes = _hashes(index, recipe=yosys_recipe)
    yosys_job = _tool_job(
        index,
        stage_id=yosys_id,
        stage="YOSYS_SYNTH",
        inputs=(index.rtl_bundle_artifact, yosys_techmap, yosys_recipe),
        hashes=yosys_hashes,
    )
    yosys, yosys_ref = run_external(
        yosys_id,
        YosysAdapter,
        "yosys",
        AdapterInvocation(
            argv_tail=("-s", f"inputs/{yosys_recipe.artifact_id}"),
            expected_output_paths=("mapped_netlist.v", "mapped_design.json"),
            environment=environment,
        ),
        yosys_job,
    )
    netlist_ref = next(
        item for item in yosys.raw_artifacts if item.artifact_id.endswith("mapped_netlist_v")
    )
    register_count = yosys.metrics.register_count
    if register_count is None or register_count <= 0:
        raise BaselineFlowError("Yosys result lacks a valid sequential-cell inventory")

    preflights = construct_baseline_preflights(
        candidate_id=index.candidate_id,
        rtl_snapshot_hash=snapshot.source_hash,
        config_hash=snapshot.config_hash,
        netlist_hash=netlist_ref.sha256,
        register_count=register_count,
        analysis_view_hashes={
            view.analysis_view_id: canonical_sha256(view) for view in index.analysis_views
        },
        constraint=constraint,
        expected_clocks=expected_clocks,
        expected_cdc=expected_cdc,
        formal_manifest=formal_manifest,
    )
    binding_hash = canonical_sha256(preflights.binding)
    formal_hash = canonical_sha256(preflights.formal_model)
    internal_specs = (
        ("stage_binding", "CONSTRAINT_BINDING", preflights.binding, "opensta"),
        ("stage_clock", "CLOCK_INVENTORY", preflights.clocks, "yosys"),
        ("stage_cdc", "CDC_INVARIANT", preflights.cdc, "slang"),
        ("stage_formal_preflight", "FORMAL_MODEL_PREFLIGHT", preflights.formal_model, "sby"),
    )
    for stage_id, stage, contract, fingerprint_id in internal_specs:
        recipe = _put_recipe(store, stage_id, "json", canonical_json_bytes(contract).decode())
        hashes = _hashes(
            index,
            recipe=recipe,
            parent=yosys_ref,
            constraint_binding=binding_hash if stage != "CONSTRAINT_BINDING" else None,
            formal_model=formal_hash if stage == "FORMAL_MODEL_PREFLIGHT" else None,
        )
        stage_hashes[stage_id] = hashes
        cached_ref = stage_refs.get(stage_id)
        reusable = False
        if cached_ref is not None:
            cached = _load_contract(store, cached_ref, StageResult)
            reusable = RunOrchestrator.can_reuse(cached, hashes)
        if not reusable:
            result, result_ref = _internal_result(
                index=index,
                store=store,
                stage_id=stage_id,
                stage=stage,
                contract=contract,
                fingerprint=fingerprints[fingerprint_id],
                hashes=hashes,
            )
            _append_stage_event(ledger, index, result, result_ref, design.effective_policy_hash)
            stage_refs[stage_id] = result_ref
            index = _persist_progress(
                resolved, index, stage_refs, stage_hashes, status="BASELINING"
            )

    simulation_id = "stage_simulation"
    simulation_recipe = _put_recipe(
        store,
        simulation_id,
        "argv",
        " ".join(
            _simulation_recipe(
                index.rtl_bundle_artifact.artifact_id,
                index.simulation_testbench_artifact.artifact_id,
            )
        ),
    )
    simulation_hashes = _hashes(index, recipe=simulation_recipe, parent=yosys_ref)
    simulation_job = _tool_job(
        index,
        stage_id=simulation_id,
        stage="SIMULATION",
        inputs=(index.rtl_bundle_artifact, index.simulation_testbench_artifact, simulation_recipe),
        hashes=simulation_hashes,
    )
    run_external(
        simulation_id,
        _IcarusSimulationAdapter,
        "iverilog",
        AdapterInvocation(
            argv_tail=_simulation_recipe(
                index.rtl_bundle_artifact.artifact_id,
                index.simulation_testbench_artifact.artifact_id,
            ),
            expected_output_paths=("simulation.vvp",),
            environment=environment,
        ),
        simulation_job,
    )

    eqy_id = "stage_eqy_smoke"
    eqy_recipe = _put_recipe(
        store,
        eqy_id,
        "eqy",
        _eqy_recipe(index.formal_smoke_source_artifact.artifact_id),
    )
    eqy_hashes = _hashes(index, recipe=eqy_recipe, parent=yosys_ref, formal_model=formal_hash)
    eqy_job = _tool_job(
        index,
        stage_id=eqy_id,
        stage="EQY_SMOKE",
        inputs=(index.formal_smoke_source_artifact, eqy_recipe),
        hashes=eqy_hashes,
    )
    run_external(
        eqy_id,
        EQYAdapter,
        "eqy",
        AdapterInvocation(
            argv_tail=("-f", f"inputs/{eqy_recipe.artifact_id}"),
            expected_output_paths=(),
            environment=environment,
        ),
        eqy_job,
    )

    property_source = index.formal_property_source_artifact
    if property_source is None:
        raise BaselineFlowError("run lacks the hashed CDC formal property source")
    sby_id = "stage_sby_cdc_properties"
    sby_recipe = _put_recipe(
        store,
        sby_id,
        "sby",
        _sby_recipe(index.rtl_bundle_artifact.artifact_id, property_source.artifact_id),
    )
    sby_hashes = _hashes(index, recipe=sby_recipe, parent=yosys_ref, formal_model=formal_hash)
    sby_job = _tool_job(
        index,
        stage_id=sby_id,
        stage="FORMAL_EQUIVALENCE",
        inputs=(index.rtl_bundle_artifact, property_source, sby_recipe),
        hashes=sby_hashes,
    )
    run_external(
        sby_id,
        _SbyBaselineAdapter,
        "sby",
        AdapterInvocation(
            argv_tail=("-f", "formal.sby"),
            expected_output_paths=(),
            environment=environment,
        ),
        sby_job,
    )

    if pre_timing_hook is not None:
        pre_timing_hook(index)

    setup_checkpoint: ArtifactRef | None = None
    setup_road_result_ref: ArtifactRef | None = None
    ordered_views = tuple(sorted(index.analysis_views, key=lambda item: item.check != "SETUP"))
    for view in ordered_views:
        view_id = view.analysis_view_id
        check = view.check
        sta_id = f"stage_opensta_{view_id}"
        # The SDC itself, not the typed expectation contract, is staged under its initialized ID.
        sdc_ref = store.put_named_bytes(
            (index.project_root / "constraints/nebula.sdc").read_bytes(),
            artifact_id=f"input_sdc_{view_id}",
            media_type="application/x-sdc",
            classification="INTERNAL",
            producer_stage_result_id=None,
        )
        sta_text = _sta_recipe(
            lock,
            orfs_root,
            check=check,
            netlist_id=netlist_ref.artifact_id,
            constraint_id=sdc_ref.artifact_id,
        )
        sta_recipe = _put_recipe(store, sta_id, "tcl_final", sta_text)
        sta_hashes = _hashes(
            index,
            recipe=sta_recipe,
            parent=yosys_ref,
            view_id=view_id,
            constraint_binding=binding_hash,
            constraints=True,
        )
        sta_job = _tool_job(
            index,
            stage_id=sta_id,
            stage="OPENSTA_FULL",
            inputs=(netlist_ref, sdc_ref, sta_recipe),
            hashes=sta_hashes,
            view_id=view_id,
        )
        run_external(
            sta_id,
            OpenSTAAdapter,
            "opensta",
            AdapterInvocation(
                argv_tail=(f"inputs/{sta_recipe.artifact_id}",),
                expected_output_paths=(),
                environment=environment,
            ),
            sta_job,
        )

        road_id = f"stage_openroad_{view_id}"
        if check == "HOLD" and (setup_checkpoint is None or setup_road_result_ref is None):
            raise BaselineFlowError("hold physical analysis lacks the setup placed checkpoint")
        road_recipe = _put_recipe(
            store,
            road_id,
            "tcl",
            _openroad_recipe(
                lock,
                orfs_root,
                corner_name=check,
                netlist_id=netlist_ref.artifact_id,
                constraint_id=sdc_ref.artifact_id,
                register_count=register_count,
                deterministic_seed=project.optimization.deterministic_seed,
                checkpoint_id=(setup_checkpoint.artifact_id if check == "HOLD" else None),
            ),
        )
        road_parent = setup_road_result_ref if check == "HOLD" else yosys_ref
        road_hashes = _hashes(
            index,
            recipe=road_recipe,
            parent=road_parent,
            view_id=view_id,
            constraint_binding=binding_hash,
            constraints=True,
        )
        road_inputs = (
            (setup_checkpoint, sdc_ref, road_recipe)
            if check == "HOLD" and setup_checkpoint is not None
            else (netlist_ref, sdc_ref, road_recipe)
        )
        road_job = _tool_job(
            index,
            stage_id=road_id,
            stage="OPENROAD_PHYSICAL",
            inputs=road_inputs,
            hashes=road_hashes,
            view_id=view_id,
        )
        road_result, road_result_ref = run_external(
            road_id,
            OpenROADAdapter,
            "openroad",
            AdapterInvocation(
                argv_tail=("-no_init", "-exit", f"inputs/{road_recipe.artifact_id}"),
                expected_output_paths=("placed.odb",),
                environment=environment,
            ),
            road_job,
        )
        if check == "SETUP":
            setup_checkpoint = next(
                (
                    item
                    for item in road_result.raw_artifacts
                    if item.artifact_id.endswith("placed_odb")
                ),
                None,
            )
            if setup_checkpoint is None:
                raise BaselineFlowError("setup physical analysis did not preserve placed.odb")
            setup_road_result_ref = road_result_ref

    index = _persist_progress(resolved, index, stage_refs, stage_hashes, status="PASS")
    current = orchestrator.get_state(index.run_id)
    if current is not None and current.state == "BASELINING":
        orchestrator.transition(index.run_id, "BASELINING", "EVIDENCE_BUILD", design)
    return index


__all__ = ["analyze_run"]
