"""Formal compilation identity and multiclock harness safety behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.analysis import ClockInventory
from nova_rtl.formal.harness import render_multiclock_harness
from nova_rtl.formal.preflight import (
    CompilationDefine,
    FormalCompilationIdentity,
    preflight_formal_model,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_MANIFEST = PROJECT_ROOT / "benchmark/generator/benchmark.yaml"


def hash_ref(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _clock_inventory(tmp_path: Path) -> ClockInventory:
    output = tmp_path / "tiny"
    generate_benchmark(load_benchmark_config(PROFILE_MANIFEST, "tiny"), output)
    return ClockInventory.model_validate_json(
        (output / "expected/clock_inventory.json").read_text(encoding="utf-8")
    )


def _compilation(
    clock_inventory: ClockInventory,
    *,
    property_only: tuple[CompilationDefine, ...] = (),
    behavior_defines: tuple[CompilationDefine, ...] = (),
) -> FormalCompilationIdentity:
    return FormalCompilationIdentity(
        functional_rtl_hash=hash_ref("1"),
        parameter_hash=hash_ref("2"),
        functional_elaboration_hash=hash_ref("3"),
        behavior_affecting_defines=behavior_defines,
        property_only_defines=property_only,
        protected_clock_fingerprint=clock_inventory.clock_graph_hash,
        protected_reset_fingerprint=hash_ref("4"),
        protected_cdc_fingerprint=hash_ref("5"),
    )


def test_multiclock_harness_shares_independent_master_events_and_enables_multiclock(
    tmp_path: Path,
) -> None:
    inventory = _clock_inventory(tmp_path)

    first = render_multiclock_harness(
        inventory,
        reset_assumptions=("reset assertion may occur asynchronously",),
        fairness_assumptions=("each master event eventually advances",),
        environment_assumptions=("workload inputs are identical for gold and gate",),
    )
    second = render_multiclock_harness(
        inventory,
        reset_assumptions=("reset assertion may occur asynchronously",),
        fairness_assumptions=("each master event eventually advances",),
        environment_assumptions=("workload inputs are identical for gold and gate",),
    )

    assert first.contract.harness_hash == second.contract.harness_hash
    assert len(first.contract.shared_master_events) == 5
    assert all(
        item.gold_event_signal.replace("gold_", "")
        == item.gate_event_signal.replace("gate_", "")
        for item in first.contract.shared_master_events
    )
    assert first.contract.generated_clock_graph_hash == inventory.clock_graph_hash
    assert b"multiclock on" in first.sby_options
    assert b"gold_clk_ingress" in first.systemverilog_source
    assert b"gate_clk_ingress" in first.systemverilog_source


def test_formal_preflight_emits_none_behavior_delta_before_proof(tmp_path: Path) -> None:
    inventory = _clock_inventory(tmp_path)
    formal_define = CompilationDefine(name="NOVA_FORMAL_PROPERTIES", value="1")
    synthesis = _compilation(inventory)
    formal = _compilation(inventory, property_only=(formal_define,))
    harness = render_multiclock_harness(
        inventory,
        reset_assumptions=("reset assertion may occur asynchronously",),
        fairness_assumptions=("each master event eventually advances",),
        environment_assumptions=("workload inputs are identical for gold and gate",),
    )

    contract = preflight_formal_model(
        formal_model_contract_id="formal_model_baseline",
        candidate_id="baseline",
        synthesis=synthesis,
        formal=formal,
        allowed_property_only_defines=(formal_define,),
        harness=harness.contract,
        property_manifest_hash=hash_ref("6"),
        gold_snapshot_hash=hash_ref("7"),
        gate_snapshot_hash=None,
    )

    assert contract.behavioral_elaboration_delta == "NONE"
    assert contract.multiclock_enabled is True
    assert contract.master_clock_model == "INDEPENDENT_SHARED_GOLD_GATE_EVENTS"
    assert contract.generated_clock_model == "DERIVED_FROM_PROTECTED_DIVIDER_STATE"


def test_behavior_changing_formal_only_define_is_rejected_before_proof(
    tmp_path: Path,
) -> None:
    inventory = _clock_inventory(tmp_path)
    synthesis = _compilation(inventory)
    formal = _compilation(
        inventory,
        behavior_defines=(CompilationDefine(name="NOVA_DIVIDER_BYPASS", value="1"),),
    )
    harness = render_multiclock_harness(
        inventory,
        reset_assumptions=("reset assertion may occur asynchronously",),
        fairness_assumptions=("each master event eventually advances",),
        environment_assumptions=("workload inputs are identical for gold and gate",),
    )

    with pytest.raises(SafetyPreflightError) as failure:
        preflight_formal_model(
            formal_model_contract_id="formal_model_baseline",
            candidate_id="baseline",
            synthesis=synthesis,
            formal=formal,
            allowed_property_only_defines=(),
            harness=harness.contract,
            property_manifest_hash=hash_ref("6"),
            gold_snapshot_hash=hash_ref("7"),
            gate_snapshot_hash=None,
        )

    assert failure.value.code == "FORMAL_BEHAVIOR_IDENTITY_CHANGED"
