from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.platform import ToolchainReceipt
from nova_rtl.contracts.verification import FormalModelContract
from nova_rtl.formal.compose import (
    compose_strict_equivalence,
    run_strict_equivalence,
    source_snapshot_hash,
)
from nova_rtl.transforms.boolean_tree import (
    BalanceBooleanTree,
    BooleanTreeContext,
    BooleanTreeParameters,
)
from nova_rtl.transforms.fsm_decode import (
    FsmDecodeContext,
    FsmDecodeParameters,
    RestructureFsmDecode,
)
from nova_rtl.transforms.predicate import (
    FactorCommonPredicate,
    PredicateContext,
    PredicateParameters,
)

ROOT = Path(__file__).resolve().parents[3]
TOOL_RECEIPT = ROOT / ".nova-tools/toolchain-receipt.json"


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _formal_model(gold_hash: str, gate_hash: str, candidate_id: str) -> FormalModelContract:
    return FormalModelContract(
        formal_model_contract_id=f"formal_model_{candidate_id}",
        candidate_id=candidate_id,
        functional_rtl_hash=gold_hash,
        parameter_hash=_hash("3"),
        gold_snapshot_hash=gold_hash,
        gate_snapshot_hash=gate_hash,
        property_manifest_hash=_hash("4"),
        master_clock_model="INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
        generated_clock_model="DERIVED_FROM_PROTECTED_DIVIDER_STATE",
        multiclock_enabled=True,
        reset_assumption_hash=_hash("5"),
        environment_assumption_hash=_hash("6"),
        proof_scope_policy="WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
        behavioral_elaboration_delta="NONE",
    )


def _tools():  # type: ignore[no-untyped-def]
    if not TOOL_RECEIPT.is_file():
        pytest.skip("real transform proof matrix requires a hydrated toolchain")
    receipt = ToolchainReceipt.model_validate_json(TOOL_RECEIPT.read_bytes())
    tools = {tool.tool_id: tool for tool in receipt.tool_fingerprints}
    return tools["eqy"], tools["yosys"]


def _prove(tmp_path: Path, gold_text: str, gate_text: str, case_id: str) -> str:
    gold = tmp_path / case_id / "gold"
    gate = tmp_path / case_id / "gate"
    gold.mkdir(parents=True)
    gate.mkdir(parents=True)
    gold.joinpath("transform_case.sv").write_text(gold_text)
    gate.joinpath("transform_case.sv").write_text(gate_text)
    gold_hash = source_snapshot_hash(gold, ("transform_case.sv",))
    gate_hash = source_snapshot_hash(gate, ("transform_case.sv",))
    plan = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("transform_case.sv",),
        top="transform_case",
        formal_model=_formal_model(gold_hash, gate_hash, case_id),
    )
    eqy, yosys = _tools()
    return run_strict_equivalence(
        plan=plan,
        gold_root=gold,
        gate_root=gate,
        workspace_root=tmp_path / case_id / "proof",
        artifact_store=ArtifactStore(tmp_path / case_id / "artifacts"),
        eqy_fingerprint=eqy,
        yosys_fingerprint=yosys,
        run_id="run_m5_formal",
        candidate_id=case_id,
    ).outcome


def _boolean_statement() -> str:
    source = "y = a & b & c & d;"
    context = BooleanTreeContext(
        relative_path="transform_case.sv",
        source_text=source,
        start_line=2,
        start_column=3,
        end_line=2,
        end_column=3 + len(source),
        owner_hierarchy="transform_case",
        expected_owner_hierarchy="transform_case",
        clock_domain_ids=("clk_test",),
        protected_neighbor_ids=(),
        operand_width=8,
        equal_widths_verified=True,
        unsigned_verified=True,
        four_state_associative=True,
    )
    capability = BalanceBooleanTree()
    return capability.rewrite(
        capability.match(context),
        BooleanTreeParameters(operator="&", max_operands=8),
    ).replacement


def _predicate_statement() -> str:
    source = "y = (enable & a) | (enable & b) | (enable & c);"
    context = PredicateContext(
        relative_path="transform_case.sv",
        source_text=source,
        start_line=2,
        start_column=3,
        end_line=2,
        end_column=3 + len(source),
        owner_hierarchy="transform_case",
        expected_owner_hierarchy="transform_case",
        clock_domain_ids=("clk_test",),
        protected_neighbor_ids=(),
        equal_widths_verified=True,
        unsigned_verified=True,
        predicate_pure=True,
        four_state_distributive=True,
        estimated_fanout_after=3,
    )
    capability = FactorCommonPredicate()
    return capability.rewrite(
        capability.match(context),
        PredicateParameters(max_terms=8, max_fanout=8),
    ).replacement


def _fsm_statement() -> str:
    source = (
        "if (state == 2'b00) begin next = 2'b01; busy = 1'b0; end "
        "else if (state == 2'b01) begin next = 2'b00; busy = 1'b1; end "
        "else begin next = 2'b00; busy = 1'b0; end"
    )
    context = FsmDecodeContext(
        relative_path="transform_case.sv",
        source_text=source,
        start_line=4,
        start_column=3,
        end_line=4,
        end_column=3 + len(source),
        owner_hierarchy="transform_case",
        expected_owner_hierarchy="transform_case",
        clock_domain_ids=("clk_test",),
        protected_neighbor_ids=(),
        state_signal="state",
        state_width_verified=True,
        state_register_protected=True,
        reset_semantics_verified=True,
        enable_semantics_verified=True,
        complete_output_assignments=True,
    )
    capability = RestructureFsmDecode()
    return capability.rewrite(
        capability.match(context), FsmDecodeParameters(max_states=8)
    ).replacement


def _module(inputs: str, outputs: str, declarations: str, statement: str) -> str:
    return (
        f"module transform_case({inputs}, {outputs});\n"
        f"  {declarations}\n"
        "  always_comb begin\n"
        f"    {statement}\n"
        "  end\n"
        "endmodule\n"
    )


@pytest.mark.parametrize(
    ("case_id", "gold", "gate"),
    (
        (
            "boolean_correct",
            _module(
                "input logic [7:0] a, b, c, d",
                "output logic [7:0] y",
                "",
                "y = a & b & c & d;",
            ),
            _module(
                "input logic [7:0] a, b, c, d",
                "output logic [7:0] y",
                "",
                _boolean_statement(),
            ),
        ),
        (
            "predicate_correct",
            _module(
                "input logic [7:0] enable, a, b, c",
                "output logic [7:0] y",
                "",
                "y = (enable & a) | (enable & b) | (enable & c);",
            ),
            _module(
                "input logic [7:0] enable, a, b, c",
                "output logic [7:0] y",
                "",
                _predicate_statement(),
            ),
        ),
        (
            "fsm_correct",
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                "if (state == 2'b00) begin next = 2'b01; busy = 1'b0; end "
                "else if (state == 2'b01) begin next = 2'b00; busy = 1'b1; end "
                "else begin next = 2'b00; busy = 1'b0; end",
            ),
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                _fsm_statement(),
            ),
        ),
    ),
)
def test_registered_transform_passes_strict_equivalence(
    tmp_path: Path, case_id: str, gold: str, gate: str
) -> None:
    assert _prove(tmp_path, gold, gate, case_id) == "PASS"


@pytest.mark.parametrize(
    ("case_id", "gold", "gate"),
    (
        (
            "boolean_lost_operand",
            _module("input logic a,b,c,d", "output logic y", "", "y = a & b & c & d;"),
            _module("input logic a,b,c,d", "output logic y", "", "y = (a & b) & c;"),
        ),
        (
            "boolean_wrong_operator",
            _module("input logic a,b,c,d", "output logic y", "", "y = a & b & c & d;"),
            _module("input logic a,b,c,d", "output logic y", "", "y = (a & b) | (c & d);"),
        ),
        (
            "predicate_altered_enable",
            _module(
                "input logic enable,other,a,b,c",
                "output logic y",
                "",
                "y = (enable & a) | (enable & b) | (enable & c);",
            ),
            _module(
                "input logic enable,other,a,b,c",
                "output logic y",
                "",
                "y = enable & (a | b) | (other & c);",
            ),
        ),
        (
            "predicate_lost_term",
            _module(
                "input logic enable,a,b,c",
                "output logic y",
                "",
                "y = (enable & a) | (enable & b) | (enable & c);",
            ),
            _module(
                "input logic enable,a,b,c",
                "output logic y",
                "",
                "y = enable & (a | b);",
            ),
        ),
        (
            "fsm_reset_output_change",
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                "if (state == 2'b00) begin next = 2'b01; busy = 1'b0; end "
                "else if (state == 2'b01) begin next = 2'b00; busy = 1'b1; end "
                "else begin next = 2'b00; busy = 1'b0; end",
            ),
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                "case (state) 2'b00: begin next = 2'b01; busy = 1'b1; end "
                "2'b01: begin next = 2'b00; busy = 1'b1; end "
                "default: begin next = 2'b00; busy = 1'b0; end endcase",
            ),
        ),
        (
            "fsm_missing_default_assignment",
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                "if (state == 2'b00) begin next = 2'b01; busy = 1'b0; end "
                "else if (state == 2'b01) begin next = 2'b00; busy = 1'b1; end "
                "else begin next = 2'b00; busy = 1'b0; end",
            ),
            _module(
                "input logic [1:0] state",
                "output logic [1:0] next, output logic busy",
                "",
                "case (state) 2'b00: begin next = 2'b01; busy = 1'b0; end "
                "2'b01: begin next = 2'b00; busy = 1'b1; end "
                "default: begin next = 2'b00; busy = 1'b1; end endcase",
            ),
        ),
    ),
)
def test_semantic_mutation_fails_strict_equivalence(
    tmp_path: Path, case_id: str, gold: str, gate: str
) -> None:
    assert _prove(tmp_path, gold, gate, case_id) == "FAIL"
