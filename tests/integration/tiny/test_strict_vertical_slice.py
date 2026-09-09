from __future__ import annotations

import json
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
from nova_rtl.transforms.priority_mux import (
    PriorityMuxContext,
    PriorityMuxParameters,
    RestructurePriorityMux,
)

ROOT = Path(__file__).resolve().parents[3]
TOOL_RECEIPT = ROOT / ".nova-tools" / "toolchain-receipt.json"
SERIAL = (
    "if (req[0]) grant = value0; else if (req[1]) grant = value1; "
    "else if (req[2]) grant = value2; else grant = fallback;"
)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _module(statement: str) -> str:
    return f"""module priority_mux(
    input logic [2:0] req,
    input logic [7:0] value0, value1, value2, fallback,
    output logic [7:0] grant
);
  always_comb begin
    {statement}
  end
endmodule
"""


def _correct_rewrite() -> str:
    context = PriorityMuxContext(
        relative_path="priority_mux.sv",
        source_text=SERIAL,
        start_line=7,
        start_column=5,
        end_line=7,
        end_column=5 + len(SERIAL),
        owner_hierarchy="priority_mux",
        expected_owner_hierarchy="priority_mux",
        clock_domain_ids=("clk_core",),
        protected_neighbor_ids=(),
    )
    capability = RestructurePriorityMux()
    return capability.rewrite(
        capability.match(context), PriorityMuxParameters(max_branches=8)
    ).replacement


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
        pytest.skip("real strict proof requires a hydrated .nova-tools toolchain")
    receipt = ToolchainReceipt.model_validate(json.loads(TOOL_RECEIPT.read_text()))
    tools = {tool.tool_id: tool for tool in receipt.tool_fingerprints}
    return tools["eqy"], tools["yosys"]


def _run(tmp_path: Path, gate_statement: str, candidate_id: str):  # type: ignore[no-untyped-def]
    gold = tmp_path / candidate_id / "gold"
    gate = tmp_path / candidate_id / "gate"
    gold.mkdir(parents=True)
    gate.mkdir(parents=True)
    gold.joinpath("priority_mux.sv").write_text(_module(SERIAL))
    gate.joinpath("priority_mux.sv").write_text(_module(gate_statement))
    gold_hash = source_snapshot_hash(gold, ("priority_mux.sv",))
    gate_hash = source_snapshot_hash(gate, ("priority_mux.sv",))
    plan = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("priority_mux.sv",),
        top="priority_mux",
        formal_model=_formal_model(gold_hash, gate_hash, candidate_id),
    )
    eqy, yosys = _tools()
    return run_strict_equivalence(
        plan=plan,
        gold_root=gold,
        gate_root=gate,
        workspace_root=tmp_path / candidate_id / "proof",
        artifact_store=ArtifactStore(tmp_path / candidate_id / "artifacts"),
        eqy_fingerprint=eqy,
        yosys_fingerprint=yosys,
        run_id="run_m4_formal",
        candidate_id=candidate_id,
    )


@pytest.mark.integration
def test_bad_priority_rewrite_fails_eqy(tmp_path: Path) -> None:
    bad = (
        "if (req[2]) grant = value2; else if (req[1]) grant = value1; "
        "else if (req[0]) grant = value0; else grant = fallback;"
    )
    result = _run(tmp_path, bad, "cand_bad_priority")

    assert result.outcome == "FAIL"
    assert result.counterexample_artifact_id is not None
    assert any(partition.status == "FAIL" for partition in result.partitions)


@pytest.mark.integration
def test_registered_priority_rewrite_passes_strict_eqy(tmp_path: Path) -> None:
    result = _run(tmp_path, _correct_rewrite(), "cand_good_priority")

    assert result.outcome == "PASS"
    assert result.contract == "STRICT_SEQ_EQUIV"
    assert result.counterexample_artifact_id is None
