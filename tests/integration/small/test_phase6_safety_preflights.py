"""Generated tiny benchmark through every Phase 6 safety contract."""

from __future__ import annotations

from pathlib import Path

from nova_rtl.benchmark.constraints import load_constraint_contract
from nova_rtl.benchmark.formal import load_formal_manifest
from nova_rtl.benchmark.generator import generate_benchmark, load_benchmark_config
from nova_rtl.cdc.inventory import (
    CdcCrossingObservation,
    StructuralCDCObservation,
    construct_cdc_inventory,
)
from nova_rtl.constraints.binding import (
    ConstraintCommandResolution,
    audit_constraint_binding,
)
from nova_rtl.constraints.clocks import (
    ClockStageObservation,
    ObservedGeneratedClock,
    construct_clock_inventory,
)
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import canonical_sha256
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


def _constraint_resolutions(contract) -> tuple[ConstraintCommandResolution, ...]:
    commands: list[ConstraintCommandResolution] = []
    for clock in contract.master_clocks:
        commands.append(
            ConstraintCommandResolution(
                command_id=f"create_clock_{clock.clock_id}",
                normalized_command=f"create_clock {clock.clock_id}",
                selectors=(clock.source_selector,),
                resolved_object_ids=(f"port:{clock.clock_id}",),
            )
        )
    for clock in contract.generated_clocks:
        commands.append(
            ConstraintCommandResolution(
                command_id=f"create_generated_{clock.clock_id}",
                normalized_command=f"create_generated_clock {clock.clock_id}",
                selectors=(clock.source_selector, clock.target_selector),
                resolved_object_ids=(
                    f"pin:{clock.source_selector}",
                    f"pin:{clock.target_selector}",
                ),
            )
        )
    commands.append(
        ConstraintCommandResolution(
            command_id="asynchronous_master_groups",
            normalized_command="set_clock_groups -asynchronous",
            selectors=tuple(item.clock_id for item in contract.master_clocks),
            resolved_object_ids=tuple(
                f"clock:{item.clock_id}" for item in contract.master_clocks
            ),
        )
    )
    commands.append(
        ConstraintCommandResolution(
            command_id="clock_uncertainty_all",
            normalized_command="set_clock_uncertainty all_clocks",
            selectors=("all_clocks",),
            resolved_object_ids=tuple(
                f"clock:{item.clock_id}"
                for item in (*contract.master_clocks, *contract.generated_clocks)
            ),
        )
    )
    for index, selector in enumerate(contract.input_delay_selectors):
        commands.append(
            ConstraintCommandResolution(
                command_id=f"input_delay_{index:02d}",
                normalized_command=f"set_input_delay {selector}",
                selectors=(selector,),
                resolved_object_ids=(f"port:{selector}",),
            )
        )
    for index, selector in enumerate(contract.output_delay_selectors):
        commands.append(
            ConstraintCommandResolution(
                command_id=f"output_delay_{index:02d}",
                normalized_command=f"set_output_delay {selector}",
                selectors=(selector,),
                resolved_object_ids=(f"port:{selector}",),
            )
        )
    for exception in contract.reviewed_exceptions:
        commands.append(
            ConstraintCommandResolution(
                command_id=f"exception_{exception.exception_id}",
                normalized_command=f"set_false_path {exception.exception_id}",
                selectors=(exception.from_selector, exception.to_selector),
                resolved_object_ids=("port:rst_n", "pin:all_registers/D"),
            )
        )
    return tuple(sorted(commands, key=lambda item: item.command_id))


def _clock_observation(stage: str, inventory: ClockInventory) -> ClockStageObservation:
    return ClockStageObservation(
        stage=stage,
        master_clocks=inventory.master_clocks,
        generated_clocks=tuple(
            ObservedGeneratedClock(
                clock_id=item.clock_id,
                domain_id=item.domain_id,
                master_ancestor_ids=(item.master_clock_id,),
                source_object=item.source_object,
                multiply_by=item.multiply_by,
                divide_by=item.divide_by,
                waveform_ns=item.waveform_ns,
                active_consumer_count=item.active_consumer_count,
            )
            for item in inventory.generated_clocks
        ),
    )


def _cdc_observation(inventory: CDCInventory) -> StructuralCDCObservation:
    return StructuralCDCObservation(
        stage="POST_SYNTHESIS",
        approved_pattern_registry_hash=inventory.approved_pattern_registry_hash,
        crossings=tuple(
            CdcCrossingObservation(
                crossing_id=item.crossing_id,
                source_domain_id=item.source_domain_id,
                destination_domain_id=item.destination_domain_id,
                source_object=item.source_object,
                destination_object=item.destination_object,
                signal_class=item.signal_class,
                recognized_pattern=item.recognized_pattern,
                structural_fingerprint=item.structural_fingerprint,
                protocol_property_refs=item.protocol_property_refs,
                is_combinational=False,
            )
            for item in inventory.crossings
        ),
    )


def test_tiny_benchmark_produces_all_canonical_phase6_contracts(tmp_path: Path) -> None:
    output = tmp_path / "tiny"
    snapshot = generate_benchmark(
        load_benchmark_config(PROFILE_MANIFEST, "tiny"),
        output,
    )
    constraint = load_constraint_contract(output / "expected/constraint_contract.json")
    expected_clocks = ClockInventory.model_validate_json(
        (output / "expected/clock_inventory.json").read_text(encoding="utf-8")
    )
    expected_cdc = CDCInventory.model_validate_json(
        (output / "expected/cdc_inventory.json").read_text(encoding="utf-8")
    )
    formal_manifest = load_formal_manifest(output / "formal/harnesses.json")
    commands = _constraint_resolutions(constraint)
    view_hashes = {
        "func_hold_fast": hash_ref("1"),
        "func_setup_slow": hash_ref("2"),
    }

    binding = audit_constraint_binding(
        binding_manifest_id="binding_baseline",
        candidate_id="baseline",
        sdc_hash=constraint.sdc_hash,
        netlist_snapshot_hash=snapshot.source_hash,
        analysis_view_hashes=view_hashes,
        expected_analysis_view_hashes=view_hashes,
        expected_command_ids=tuple(item.command_id for item in commands),
        command_resolutions=commands,
        sequential_endpoint_ids=("pin:u_a/D", "pin:u_b/D", "pin:u_c/D"),
        timed_endpoint_ids=("pin:u_a/D", "pin:u_b/D"),
        reviewed_exception_endpoint_ids=("pin:u_c/D",),
        baseline=None,
    )
    clocks = construct_clock_inventory(
        candidate_id="baseline",
        expected=expected_clocks,
        post_elaboration=_clock_observation("POST_ELABORATION", expected_clocks),
        post_synthesis=_clock_observation("POST_SYNTHESIS", expected_clocks),
    )
    cdc = construct_cdc_inventory(
        candidate_id="baseline",
        approved_registry=expected_cdc,
        observation=_cdc_observation(expected_cdc),
    )
    harness = render_multiclock_harness(
        clocks,
        reset_assumptions=("reset assertion may occur asynchronously",),
        fairness_assumptions=("each master event eventually advances",),
        environment_assumptions=("workload inputs are shared by gold and gate",),
    )
    property_define = CompilationDefine(name="NOVA_FORMAL_PROPERTIES", value="1")
    synthesis = FormalCompilationIdentity(
        functional_rtl_hash=snapshot.source_hash,
        parameter_hash=snapshot.config_hash,
        functional_elaboration_hash=hash_ref("3"),
        behavior_affecting_defines=(),
        property_only_defines=(),
        protected_clock_fingerprint=clocks.clock_graph_hash,
        protected_reset_fingerprint=hash_ref("4"),
        protected_cdc_fingerprint=cdc.inventory_hash,
    )
    formal = synthesis.model_copy(update={"property_only_defines": (property_define,)})
    formal_model = preflight_formal_model(
        formal_model_contract_id="formal_model_baseline",
        candidate_id="baseline",
        synthesis=synthesis,
        formal=formal,
        allowed_property_only_defines=(property_define,),
        harness=harness.contract,
        property_manifest_hash=formal_manifest.manifest_hash,
        gold_snapshot_hash=snapshot.source_hash,
        gate_snapshot_hash=None,
    )

    assert binding.effective_binding_hash == canonical_sha256(
        binding, exclude=frozenset({"effective_binding_hash"})
    )
    assert clocks.clock_graph_hash == canonical_sha256(
        clocks, exclude=frozenset({"clock_graph_hash"})
    )
    assert cdc.inventory_hash == canonical_sha256(
        cdc, exclude=frozenset({"inventory_hash"})
    )
    assert formal_model.behavioral_elaboration_delta == "NONE"
