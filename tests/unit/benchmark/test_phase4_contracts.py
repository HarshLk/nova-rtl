from __future__ import annotations

from pathlib import Path

from nova_rtl.benchmark.constraints import load_constraint_contract
from nova_rtl.benchmark.formal import load_formal_manifest
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.benchmark.power import load_power_workload
from nova_rtl.benchmark.validate import validate_benchmark
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"


def test_full_profile_emits_complete_clock_cdc_constraint_and_formal_inventories(
    tmp_path: Path,
) -> None:
    output = tmp_path / "full"
    snapshot = generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "full"), output)

    clock_inventory = ClockInventory.model_validate_json(
        (output / "expected/clock_inventory.json").read_text(encoding="utf-8")
    )
    cdc_inventory = CDCInventory.model_validate_json(
        (output / "expected/cdc_inventory.json").read_text(encoding="utf-8")
    )
    constraints = load_constraint_contract(output / "expected/constraint_contract.json")
    formal = load_formal_manifest(output / "formal/harnesses.json")
    workload = load_power_workload(output / "sim/power_workload.yaml")

    assert len(clock_inventory.master_clocks) == 5
    assert len(clock_inventory.generated_clocks) == 105
    assert len(clock_inventory.lineage_edges) == 105
    assert clock_inventory.cross_master_synchronous_relationships == ()
    assert all(clock.active_consumer_count > 0 for clock in clock_inventory.generated_clocks)
    assert len(cdc_inventory.crossings) == 12
    assert all(crossing.status == "APPROVED" for crossing in cdc_inventory.crossings)
    assert cdc_inventory.new_unapproved_count == 0
    assert cdc_inventory.ambiguous_count == 0
    assert len(constraints.master_clocks) == 5
    assert {clock.period_ns for clock in constraints.master_clocks} == {5.0}
    assert len(constraints.generated_clocks) == 105
    assert constraints.expected_unconstrained_endpoints == 0
    assert len(constraints.asynchronous_master_groups) == 5
    assert formal.property_ids == (
        "async_fifo_gray_pointer_safety",
        "req_ack_one_outstanding",
        "reset_async_assert_local_release",
        "stable_level_two_flop",
        "toggle_pulse_delivery",
    )
    assert tuple(phase.phase_id for phase in workload.phases) == (
        "reset",
        "idle",
        "bursts",
        "arbitration",
        "backpressure",
        "cdc_traffic",
    )
    assert workload.vcd_scope == "tb_nebula.dut"
    assert workload.measurement_window_ns == (300.0, 4300.0)
    assert snapshot.constraint_contract_hash == constraints.contract_hash
    assert snapshot.clock_inventory_hash == clock_inventory.clock_graph_hash
    assert snapshot.cdc_inventory_hash == cdc_inventory.inventory_hash
    assert snapshot.formal_manifest_hash == formal.manifest_hash
    assert snapshot.power_workload_hash == workload.workload_hash


def test_tiny_sdc_preserves_even_and_odd_generated_clock_semantics(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)

    contract = load_constraint_contract(output / "expected/constraint_contract.json")
    by_clock = {clock.clock_id: clock for clock in contract.generated_clocks}
    even = by_clock["gclk_ingress_div_002"]
    odd = by_clock["gclk_ingress_div_003"]

    assert len(contract.generated_clocks) == 10
    assert {clock.period_ns for clock in contract.master_clocks} == {
        10.0,
        14.0,
        18.0,
        22.0,
        26.0,
    }
    assert even.waveform_mode == "EVEN_DIVIDE_BY"
    assert even.edge_indices == ()
    assert even.divide_by == 2
    assert odd.waveform_mode == "ODD_EDGE_LIST"
    assert odd.edge_indices == (1, 5, 7)
    assert odd.divide_by == 3
    assert odd.source_selector == "[get_ports clk_ingress]"
    assert odd.target_selector == (
        "[get_nets ingress_generated_clocks[1]]"
    )
    assert contract.setup_clock_uncertainty_ns == 0.1
    assert contract.hold_clock_uncertainty_ns == 0.01
    assert contract.reviewed_exceptions[0].exception_id == "async_reset_assertion"
    assert len(contract.reviewed_exceptions) == 6
    assert contract.reviewed_exceptions[1].exception_id == "generated_to_master_ingress"
    sdc = (output / "constraints/nebula.sdc").read_text(encoding="utf-8")
    assert "set_clock_uncertainty -setup 0.10" in sdc
    assert "set_clock_uncertainty -hold 0.01" in sdc
    assert "set_false_path -from [get_clocks {gclk_ingress_div_002" in sdc
    assert "-to [get_clocks clk_ingress]" in sdc
    assert sdc.endswith("\n")


def test_phase4_identities_and_output_bytes_are_reproducible(tmp_path: Path) -> None:
    config = load_benchmark_config(PROFILE_MANIFEST, "tiny")

    first = generate_benchmark(config, tmp_path / "first")
    second = generate_benchmark(config, tmp_path / "second")

    assert first.constraint_contract_hash == second.constraint_contract_hash
    assert first.clock_inventory_hash == second.clock_inventory_hash
    assert first.cdc_inventory_hash == second.cdc_inventory_hash
    assert first.formal_manifest_hash == second.formal_manifest_hash
    assert first.power_workload_hash == second.power_workload_hash
    for relative_path in (
        "constraints/nebula.sdc",
        "expected/clock_inventory.json",
        "expected/cdc_inventory.json",
        "expected/constraint_contract.json",
        "formal/cdc_protocol_properties.sv",
        "formal/harnesses.json",
        "sim/power_workload.yaml",
    ):
        assert (tmp_path / "first" / relative_path).read_bytes() == (
            tmp_path / "second" / relative_path
        ).read_bytes()


def test_official_full_constraint_and_expectation_references_do_not_drift(
    tmp_path: Path,
) -> None:
    output = tmp_path / "full_reference"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "full"), output)

    for relative_path in (
        "constraints/nebula.sdc",
        "expected/clock_contract.json",
        "expected/cdc_contract.json",
    ):
        assert (PROJECT_ROOT / "benchmark" / relative_path).read_bytes() == (
            output / relative_path
        ).read_bytes()


def test_artifact_aware_validation_rehashes_the_complete_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    snapshot = generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)

    clean = validate_benchmark(snapshot, snapshot.expectations, artifact_root=output)

    assert clean.status == "PASS"
    assert clean.artifact_identity_mismatches == ()
    assert clean.clock_lineage_mismatches == ()
    assert clean.cdc_inventory_mismatches == ()
    assert clean.unreachable_challenge_logic == ()
    assert clean.unexpected_unconstrained_endpoints == ()

    sdc = output / "constraints/nebula.sdc"
    sdc.write_text(sdc.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    tampered = validate_benchmark(snapshot, snapshot.expectations, artifact_root=output)

    assert tampered.status == "FAIL"
    assert tampered.artifact_identity_mismatches == ("constraints/nebula.sdc",)
