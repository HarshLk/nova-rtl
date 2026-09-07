from __future__ import annotations

from pathlib import Path

import pytest

from nova_rtl.contracts.verification import FormalModelContract
from nova_rtl.formal.compose import (
    FormalCompositionError,
    compose_strict_equivalence,
    source_snapshot_hash,
)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _model(gold_hash: str, gate_hash: str) -> FormalModelContract:
    return FormalModelContract(
        formal_model_contract_id="formal_model_candidate",
        candidate_id="cand_priority_mux",
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


def test_strict_composition_is_deterministic_and_snapshot_bound(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gate = tmp_path / "gate"
    gold.mkdir()
    gate.mkdir()
    source = "module m(input logic a, output logic y); assign y = a; endmodule\n"
    gold.joinpath("m.sv").write_text(source)
    gate.joinpath("m.sv").write_text(source)
    gold_hash = source_snapshot_hash(gold, ("m.sv",))
    gate_hash = source_snapshot_hash(gate, ("m.sv",))

    first = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("m.sv",),
        top="m",
        formal_model=_model(gold_hash, gate_hash),
    )
    second = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("m.sv",),
        top="m",
        formal_model=_model(gold_hash, gate_hash),
    )

    assert first == second
    assert "[gold]" in first.recipe
    assert "read -sv gold/m.sv" in first.recipe
    assert "read -sv gate/m.sv" in first.recipe
    assert "use sat" in first.recipe
    assert first.formal_model_contract_id == "formal_model_candidate"


def test_strict_composition_rejects_changed_source_after_planning(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gate = tmp_path / "gate"
    gold.mkdir()
    gate.mkdir()
    gold.joinpath("m.sv").write_text("module m; endmodule\n")
    gate.joinpath("m.sv").write_text("module m; endmodule\n")
    original_gold = source_snapshot_hash(gold, ("m.sv",))
    original_gate = source_snapshot_hash(gate, ("m.sv",))
    model = _model(original_gold, original_gate)
    gold.joinpath("m.sv").write_text("module changed; endmodule\n")

    with pytest.raises(FormalCompositionError, match="functional RTL hash"):
        compose_strict_equivalence(
            gold_root=gold,
            gate_root=gate,
            source_paths=("m.sv",),
            top="m",
            formal_model=model,
        )
