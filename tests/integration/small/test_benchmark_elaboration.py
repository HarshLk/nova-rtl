from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nova_rtl.benchmark.constraints import resolve_constraint_bindings
from nova_rtl.benchmark.formal import load_formal_manifest
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.benchmark.power import (
    load_power_workload,
    materialize_power_activity_contract,
)
from nova_rtl.benchmark.validate import validate_benchmark

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"
TOOL_ROOT = PROJECT_ROOT / ".nova-tools/components/oss_cad_suite/bin"


def _tool(name: str) -> Path:
    hydrated = TOOL_ROOT / name
    discovered = shutil.which(name)
    if hydrated.is_file():
        return hydrated
    if discovered is not None:
        return Path(discovered)
    pytest.skip(f"benchmark RTL integration requires {name}")


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _generated_sources(root: Path) -> list[Path]:
    return sorted((root / "rtl").rglob("*.sv"))


def _normalized_type(cell: dict[str, object]) -> str:
    return str(cell["type"]).replace("\\", "")


def _module_by_normalized_name(
    modules: dict[str, dict[str, object]], normalized_name: str
) -> dict[str, object]:
    return next(
        module for name, module in modules.items() if name.replace("\\", "") == normalized_name
    )


def test_full_profile_elaborates_five_domains_and_105_active_generated_clocks(
    tmp_path: Path,
) -> None:
    output = tmp_path / "full"
    snapshot = generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "full"), output)
    netlist = tmp_path / "elaborated.json"
    source_args = " ".join(str(path) for path in _generated_sources(output))
    script = (
        f"read_verilog -sv -I{output / 'rtl'} {source_args}; "
        "hierarchy -check -top nebula_top; proc; check; "
        f"write_json {netlist}"
    )

    result = _run([str(_tool("yosys")), "-q", "-p", script], cwd=PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "logic loop" not in (result.stdout + result.stderr).lower()
    design = json.loads(netlist.read_text(encoding="utf-8"))
    modules = design["modules"]
    top_cells = modules["nebula_top"]["cells"]
    expected_subsystems = {
        "u_ingress": "ingress_subsystem",
        "u_schedule": "scheduler_subsystem",
        "u_dma": "dma_subsystem",
        "u_compute": "compute_subsystem",
        "u_control": "control_subsystem",
    }
    assert {
        name: _normalized_type(top_cells[name]) for name in expected_subsystems
    } == expected_subsystems

    divider_banks = [
        cell for cell in top_cells.values() if "clock_divider_bank" in _normalized_type(cell)
    ]
    assert len(divider_banks) == 5
    bank_module_names = {_normalized_type(cell) for cell in divider_banks}
    assert len(bank_module_names) == 1
    bank_module = _module_by_normalized_name(modules, next(iter(bank_module_names)))
    divider_cells = [
        cell
        for cell in bank_module["cells"].values()
        if "protected_clock_divider" in _normalized_type(cell)
    ]
    assert len(divider_cells) == 21
    assert all("keep" in cell["attributes"] for cell in divider_cells)
    assert len(divider_cells) * len(divider_banks) == snapshot.expected_generated_total == 105

    consumer_counts = [
        sum(
            "generated_clock_consumer" in _normalized_type(cell)
            for cell in module["cells"].values()
        )
        for module in modules.values()
    ]
    assert consumer_counts.count(21) >= 5


def test_elaborated_crossings_match_the_approved_cdc_contract(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    netlist = tmp_path / "elaborated.json"
    source_args = " ".join(str(path) for path in _generated_sources(output))
    script = (
        f"read_verilog -sv -I{output / 'rtl'} {source_args}; "
        "hierarchy -check -top nebula_top; synth -top nebula_top; check; "
        f"write_json {netlist}"
    )

    result = _run([str(_tool("yosys")), "-q", "-p", script], cwd=PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "logic loop" not in (result.stdout + result.stderr).lower()
    design = json.loads(netlist.read_text(encoding="utf-8"))
    top_cells = design["modules"]["nebula_top"]["cells"]
    contract = json.loads((output / "expected/cdc_contract.json").read_text(encoding="utf-8"))
    assert set(contract["approved_patterns"]) == {
        "ASYNC_FIFO_GRAY_POINTERS",
        "REQUEST_ACKNOWLEDGE",
        "RESET_ASYNC_ASSERT_LOCAL_RELEASE",
        "STABLE_LEVEL_TWO_FLOP",
        "TOGGLE_PULSE",
    }
    for crossing in contract["crossings"]:
        cell = top_cells[crossing["instance"]]
        assert crossing["module"] in _normalized_type(cell)
        assert "keep" in cell["attributes"]

    fifo_modules = [
        module
        for name, module in design["modules"].items()
        if "cdc_async_fifo" in name.replace("\\", "")
    ]
    assert fifo_modules
    assert any(
        sum("cdc_gray_sync" in _normalized_type(cell) for cell in module["cells"].values()) == 2
        for module in fifo_modules
    )
    synthesized_consumer_counts = [
        sum(
            "generated_clock_consumer" in _normalized_type(cell)
            for cell in module["cells"].values()
        )
        for module in design["modules"].values()
    ]
    assert synthesized_consumer_counts.count(2) >= 5
    checksum_port = design["modules"]["nebula_top"]["ports"]["benchmark_checksum"]
    assert checksum_port["direction"] == "output"
    assert len(checksum_port["bits"]) == 32


def test_even_and_odd_dividers_have_declared_half_cycle_semantics(tmp_path: Path) -> None:
    testbench = tmp_path / "tb_divider.sv"
    testbench.write_text(
        """
module tb_divider;
  logic clk = 1'b0;
  logic rst_n = 1'b0;
  logic even_clock;
  logic odd_clock;
  logic even_previous = 1'b0;
  logic odd_previous = 1'b0;
  integer cycle = 0;
  integer even_last = 0;
  integer odd_last = 0;
  integer even_transitions = 0;
  integer odd_transitions = 0;
  integer odd_expected [0:5];

  protected_clock_divider #(.RATIO(2)) u_even (
      .master_clock(clk), .local_reset_n(rst_n), .generated_clock(even_clock));
  protected_clock_divider #(.RATIO(3)) u_odd (
      .master_clock(clk), .local_reset_n(rst_n), .generated_clock(odd_clock));

  always #5 clk = ~clk;

  initial begin
    odd_expected[0] = 1;
    odd_expected[1] = 2;
    odd_expected[2] = 1;
    odd_expected[3] = 2;
    odd_expected[4] = 1;
    odd_expected[5] = 2;
    repeat (2) @(posedge clk);
    @(negedge clk); rst_n = 1'b1;
    repeat (16) begin
      @(posedge clk); cycle = cycle + 1; #1;
      if (even_clock !== even_previous) begin
        if ((cycle - even_last) != 1) $fatal(1, "even divider span mismatch");
        even_last = cycle;
        even_transitions = even_transitions + 1;
      end
      if (odd_clock !== odd_previous) begin
        if (odd_transitions < 6 && (cycle - odd_last) != odd_expected[odd_transitions])
          $fatal(1, "odd divider span mismatch");
        odd_last = cycle;
        odd_transitions = odd_transitions + 1;
      end
      even_previous = even_clock;
      odd_previous = odd_clock;
    end
    if (even_transitions < 12 || odd_transitions < 6)
      $fatal(1, "divider transition count too small");
    $display("NOVA_DIVIDER_WAVEFORMS_PASS");
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    divider = PROJECT_ROOT / "benchmark/rtl/clocking/protected_clock_divider.sv"
    executable = tmp_path / "divider.vvp"

    compiled = _run(
        [
            str(_tool("iverilog")),
            "-g2012",
            "-s",
            "tb_divider",
            "-o",
            str(executable),
            str(divider),
            str(testbench),
        ],
        cwd=PROJECT_ROOT,
    )

    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    simulated = _run([str(_tool("vvp")), str(executable)], cwd=PROJECT_ROOT)
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr
    assert "NOVA_DIVIDER_WAVEFORMS_PASS" in simulated.stdout


def test_tiny_profile_workloads_change_observable_checksum(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    executable = tmp_path / "nebula.vvp"
    testbench = PROJECT_ROOT / "benchmark/sim/tb_nebula.sv"
    command = [
        str(_tool("iverilog")),
        "-g2012",
        "-I",
        str(output / "rtl"),
        "-s",
        "tb_nebula",
        "-o",
        str(executable),
        *(str(path) for path in _generated_sources(output)),
        str(testbench),
    ]

    compiled = _run(command, cwd=PROJECT_ROOT)

    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    first_run = tmp_path / "first_run"
    second_run = tmp_path / "second_run"
    first_run.mkdir()
    second_run.mkdir()
    simulated = _run([str(_tool("vvp")), str(executable)], cwd=first_run)
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr
    assert "NOVA_OBSERVABLE_ACTIVITY_PASS" in simulated.stdout
    phases = tuple(
        line.split()[1]
        for line in simulated.stdout.splitlines()
        if line.startswith("NOVA_PHASE ")
    )
    assert phases == (
        "reset",
        "idle",
        "bursts",
        "arbitration",
        "backpressure",
        "cdc_traffic",
        "measurement_complete",
    )
    repeated = _run([str(_tool("vvp")), str(executable)], cwd=second_run)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    workload = load_power_workload(output / "sim/power_workload.yaml")
    first_activity = materialize_power_activity_contract(
        workload, first_run / "nebula_activity.vcd"
    )
    second_activity = materialize_power_activity_contract(
        workload, second_run / "nebula_activity.vcd"
    )
    assert first_activity.contract_hash == second_activity.contract_hash
    assert first_activity.source_artifact == second_activity.source_artifact
    assert first_activity.scope == "tb_nebula.dut"
    assert first_activity.time_window_ns == (300.0, 4300.0)


def test_tiny_sdc_semantic_selectors_resolve_against_elaboration(tmp_path: Path) -> None:
    output = tmp_path / "tiny_constraints"
    snapshot = generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    netlist = tmp_path / "constraint_elaboration.json"
    source_args = " ".join(str(path) for path in _generated_sources(output))
    script = (
        f"read_verilog -sv -I{output / 'rtl'} {source_args}; "
        "hierarchy -check -top nebula_top; proc; check; "
        f"write_json {netlist}"
    )

    result = _run([str(_tool("yosys")), "-q", "-p", script], cwd=PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = resolve_constraint_bindings(
        output / "expected/constraint_contract.json", netlist
    )
    assert evidence.unresolved_selectors == ()
    assert evidence.generated_clocks_without_consumers == ()
    assert evidence.unexpected_unconstrained_endpoints == ()
    assert evidence.challenge_logic_without_observable_path == ()
    validation = validate_benchmark(
        snapshot,
        snapshot.expectations,
        artifact_root=output,
        evidence=evidence,
    )
    assert validation.status == "PASS"


def test_formal_protocol_property_source_parses_with_generated_rtl(tmp_path: Path) -> None:
    output = tmp_path / "tiny_formal"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    manifest = load_formal_manifest(output / "formal/harnesses.json")
    command = [
        str(_tool("slang")),
        "--lint-only",
        "-I",
        str(output / "rtl"),
        *(str(path) for path in _generated_sources(output)),
        *(str(output / path) for path in manifest.source_files),
    ]

    result = _run(command, cwd=PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "error" not in result.stderr.lower()


def test_tiny_rtl_and_widest_divider_are_warning_free_in_verilator(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    sources = [str(path) for path in _generated_sources(output)]
    common = [
        str(_tool("verilator")),
        "--lint-only",
        "--timing",
        "-Wall",
        "-Wno-DECLFILENAME",
        "-Wno-SYNCASYNCNET",
        "-Wno-UNUSEDSIGNAL",
        "-I" + str(output / "rtl"),
    ]

    design = _run([*common, "--top-module", "nebula_top", *sources], cwd=PROJECT_ROOT)

    assert design.returncode == 0, design.stdout + design.stderr
    assert "%Warning" not in design.stdout + design.stderr

    divider = output / "rtl/clocking/protected_clock_divider.sv"
    widest = _run(
        [*common, "--top-module", "protected_clock_divider", "-GRATIO=128", str(divider)],
        cwd=PROJECT_ROOT,
    )
    assert widest.returncode == 0, widest.stdout + widest.stderr
    assert "%Warning" not in widest.stdout + widest.stderr
