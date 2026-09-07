from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.contracts.verification import FormalModelContract
from nova_rtl.formal.compose import (
    FormalCompositionError,
    compose_strict_equivalence,
    run_strict_equivalence,
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


def test_strict_execution_prevents_toolchain_bytecode_writes(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gate = tmp_path / "gate"
    gold.mkdir()
    gate.mkdir()
    source = "module m(input logic a, output logic y); assign y = a; endmodule\n"
    gold.joinpath("m.sv").write_text(source)
    gate.joinpath("m.sv").write_text(source)
    gold_hash = source_snapshot_hash(gold, ("m.sv",))
    gate_hash = source_snapshot_hash(gate, ("m.sv",))
    plan = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("m.sv",),
        top="m",
        formal_model=_model(gold_hash, gate_hash),
    )
    eqy = tmp_path / "eqy"
    yosys = tmp_path / "yosys"
    eqy.write_text(
        "#!/bin/sh\n"
        "test \"$PYTHONDONTWRITEBYTECODE\" = 1 || exit 9\n"
        "echo '[status] PASS'\n"
        "echo 'Successfully proved designs equivalent'\n"
    )
    yosys.write_text("#!/bin/sh\nexit 0\n")
    eqy.chmod(0o755)
    yosys.chmod(0o755)

    def fingerprint(path: Path, tool_id: str, version_args: tuple[str, ...]) -> ToolFingerprint:
        return ToolFingerprint(
            tool_id=tool_id,
            executable=str(path.resolve()),
            version="test",
            version_args=version_args,
            executable_sha256="sha256:" + sha256(path.read_bytes()).hexdigest(),
            build_hash=_hash("a"),
            adapter_version="formal-v1",
        )

    result = run_strict_equivalence(
        plan=plan,
        gold_root=gold,
        gate_root=gate,
        workspace_root=tmp_path / "proof",
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        eqy_fingerprint=fingerprint(eqy, "eqy", ("--version",)),
        yosys_fingerprint=fingerprint(yosys, "yosys", ("-V",)),
        run_id="run_test",
        candidate_id="cand_priority_mux",
    )

    assert result.outcome == "PASS"
