from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nova_rtl.platform.activation import verify_toolchain
from nova_rtl.platform.hydration import load_toolchain_source_manifest
from nova_rtl.platform.lock import load_platform_lock, verify_platform_lock

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = PROJECT_ROOT / "config/platform/toolchain-sources.json"
SMOKE_RTL = Path(__file__).resolve().parent / "rtl/two_flop_smoke.sv"
SMOKE_SDC = Path(__file__).resolve().parent / "constraints/two_flop_smoke.sdc"
DESIGN_NAME = "nova_m0_two_flop_smoke"


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    assert completed.returncode == 0, (
        f"command failed with exit {completed.returncode}: {' '.join(command)}\n"
        f"full log: {log_path}\n{completed.stdout[-12000:]}"
    )
    return completed


def _orfs_config(work_home: Path) -> str:
    return f"""\
export PLATFORM = asap7
export DESIGN_NAME = {DESIGN_NAME}
export DESIGN_NICKNAME = {DESIGN_NAME}
export VERILOG_FILES = {SMOKE_RTL}
export SDC_FILE = {SMOKE_SDC}
export WORK_HOME = {work_home}
export FLOW_VARIANT = m0_smoke
export ASAP7_USE_VT = RVT
export CORNER = BC
export CORE_UTILIZATION = 10
export PLACE_DENSITY = 0.20
export SYNTH_MINIMUM_KEEP_SIZE = 0
export REMOVE_ABC_BUFFERS = 1
export SKIP_INCREMENTAL_REPAIR = 1
export GPL_TIMING_DRIVEN = 0
export GPL_ROUTING_DRIVEN = 0
export SKIP_CTS_REPAIR_TIMING = 1
"""


def _sta_script(lock, netlist: Path, check: str) -> str:
    corner = lock.setup_corner if check == "SETUP" else lock.hold_corner
    path_delay = "max" if check == "SETUP" else "min"
    liberty_commands = "\n".join(
        f"read_liberty {{{artifact.resolved_path}}}" for artifact in corner.liberty_files
    )
    return f"""\
{liberty_commands}
read_verilog {{{netlist}}}
link_design {DESIGN_NAME}
read_sdc {{{SMOKE_SDC}}}
set registers [all_registers]
set register_count [llength $registers]
set paths [find_timing_paths \
  -from $registers -to $registers \
  -path_delay {path_delay} -group_count 1]
set path_count [llength $paths]
puts "NOVA_SMOKE_VIEW {corner.corner_id}"
puts "NOVA_SMOKE_CHECK {check}"
puts "NOVA_SMOKE_REGISTER_COUNT $register_count"
puts "NOVA_SMOKE_PATH_COUNT $path_count"
if {{ $register_count != 2 || $path_count < 1 }} {{
  puts stderr "required {check} view has no timed register path"
  exit 3
}}
report_checks \
  -from $registers -to $registers \
  -path_delay {path_delay} -group_count 1 \
  -fields {{slew cap input_pin net fanout}}
exit
"""


@pytest.mark.integration
def test_locked_asap7_two_view_smoke_reaches_openroad_cts(
    platform_lock_path: Path,
    tmp_path: Path,
) -> None:
    assert SMOKE_RTL.is_file(), f"missing committed smoke RTL: {SMOKE_RTL}"
    assert SMOKE_SDC.is_file(), f"missing committed smoke SDC: {SMOKE_SDC}"

    loaded_manifest = load_toolchain_source_manifest(MANIFEST_PATH)
    verified = verify_toolchain(loaded_manifest.manifest, PROJECT_ROOT / ".nova-tools")
    orfs_root = verified.root / "components/orfs"
    lock_verification = verify_platform_lock(
        platform_lock_path,
        artifact_root=orfs_root,
        tool_paths=verified.tool_paths,
    )
    assert lock_verification.status == "PASS", lock_verification.issues
    lock = load_platform_lock(platform_lock_path)
    assert lock.platform_id == "asap7"

    flow_home = orfs_root / "flow"
    work_home = tmp_path / "orfs-work"
    config_path = tmp_path / "config.mk"
    config_path.write_text(_orfs_config(work_home), encoding="utf-8")
    environment = verified.execution_environment()
    environment.update(
        {
            "OPENROAD_EXE": str(verified.tool_paths["openroad"]),
            "OPENSTA_EXE": str(verified.tool_paths["opensta"]),
            "YOSYS_EXE": str(verified.tool_paths["yosys"]),
            "PYTHON_EXE": sys.executable,
            "QT_QPA_PLATFORM": "offscreen",
        }
    )
    make_log = tmp_path / "openroad-cts.log"
    _run_checked(
        [
            "make",
            "--directory",
            str(flow_home),
            f"DESIGN_CONFIG={config_path}",
            f"WORK_HOME={work_home}",
            "FLOW_VARIANT=m0_smoke",
            f"OPENROAD_EXE={verified.tool_paths['openroad']}",
            f"OPENSTA_EXE={verified.tool_paths['opensta']}",
            f"YOSYS_EXE={verified.tool_paths['yosys']}",
            "NUM_CORES=2",
            "cts",
        ],
        cwd=PROJECT_ROOT,
        environment=environment,
        log_path=make_log,
        timeout_seconds=900,
    )

    result_directory = work_home / "results/asap7" / DESIGN_NAME / "m0_smoke"
    netlist = result_directory / "1_2_yosys.v"
    synth_database = result_directory / "1_synth.odb"
    cts_database = result_directory / "4_cts.odb"
    assert netlist.stat().st_size > 0
    assert synth_database.stat().st_size > 0
    assert cts_database.stat().st_size > 0

    view_results: list[dict[str, object]] = []
    for check, corner in (("SETUP", lock.setup_corner), ("HOLD", lock.hold_corner)):
        script_path = tmp_path / f"{check.lower()}.tcl"
        report_path = tmp_path / f"{check.lower()}.rpt"
        script_path.write_text(_sta_script(lock, netlist, check), encoding="utf-8")
        completed = _run_checked(
            [str(verified.tool_paths["opensta"]), str(script_path)],
            cwd=PROJECT_ROOT,
            environment=environment,
            log_path=report_path,
            timeout_seconds=180,
        )
        assert f"NOVA_SMOKE_VIEW {corner.corner_id}" in completed.stdout
        assert f"NOVA_SMOKE_CHECK {check}" in completed.stdout
        assert "NOVA_SMOKE_REGISTER_COUNT 2" in completed.stdout
        assert "NOVA_SMOKE_PATH_COUNT 1" in completed.stdout
        assert "Startpoint: first_stage$_SDFF_PN0_" in completed.stdout
        assert "Endpoint: data_out$_SDFF_PN0_" in completed.stdout
        view_results.append(
            {
                "check": check,
                "corner_id": corner.corner_id,
                "report": str(report_path),
            }
        )

    summary = {
        "status": "PASS",
        "platform_lock": str(platform_lock_path),
        "synth_netlist": str(netlist),
        "cts_database": str(cts_database),
        "views": view_results,
    }
    (tmp_path / "smoke-report.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
