from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.platform import ToolFingerprint
from nova_rtl.contracts.verification import FormalModelContract
from nova_rtl.formal.compose import (
    FormalCompositionError,
    build_composition_manifest,
    compose_strict_equivalence,
    run_strict_equivalence,
    source_snapshot_hash,
)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _hash_text(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()


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
    gate.joinpath("m.sv").write_text(
        "module m (input logic a, output logic y); assign y = a; endmodule\n"
    )
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
    gate.joinpath("m.sv").write_text(
        "module m (input logic a, output logic y); assign y = a; endmodule\n"
    )
    gold_hash = source_snapshot_hash(gold, ("m.sv",))
    gate_hash = source_snapshot_hash(gate, ("m.sv",))
    plan = compose_strict_equivalence(
        gold_root=gold,
        gate_root=gate,
        source_paths=("m.sv",),
        top="m",
        formal_model=_model(gold_hash, gate_hash),
    )
    composition = build_composition_manifest(
        candidate_id="cand_priority_mux",
        parent_root=gold,
        candidate_root=gate,
        full_source_paths=("m.sv",),
        changed_source_paths=("m.sv",),
        changed_span_ids=("source_m",),
        proof_plan=plan,
        parameterizations=("TOP=m",),
        boundary_inputs=("a",),
        boundary_outputs=("y",),
        state_elements=(),
        assumption_hashes=(_hash("5"), _hash("6")),
        discharge_obligations=(
            "ALL_INSTANTIATIONS_COVERED",
            "NO_STATE_IN_SCOPE",
            "UNCHANGED_FILES_BYTE_IDENTICAL",
        ),
    )
    eqy = tmp_path / "eqy"
    yosys = tmp_path / "yosys"
    eqy.write_text(
        "#!/bin/sh\n"
        'test "$PYTHONDONTWRITEBYTECODE" = 1 || exit 9\n'
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
        composition_manifest=composition,
    )

    assert result.outcome == "PASS"
    assert result.proof_scope == "COMPOSITIONALLY_CLOSED"
    assert result.composition_manifest_artifact_id is not None
    assert result.composition_manifest_artifact_id in {
        item.artifact_id for item in result.raw_artifacts
    }


def test_composition_manifest_binds_full_snapshot_and_exact_closed_scope(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "full-gold"
    gate = tmp_path / "full-gate"
    gold.mkdir()
    gate.mkdir()
    unchanged = "module helper; endmodule\n"
    gold.joinpath("helper.sv").write_text(unchanged)
    gate.joinpath("helper.sv").write_text(unchanged)
    gold.joinpath("target.sv").write_text("module target; wire x = 1'b0; endmodule\n")
    gate.joinpath("target.sv").write_text("module target; wire x = (1'b0); endmodule\n")
    proof_gold = tmp_path / "proof-gold"
    proof_gate = tmp_path / "proof-gate"
    proof_gold.mkdir()
    proof_gate.mkdir()
    proof_gold.joinpath("target.sv").write_bytes(gold.joinpath("target.sv").read_bytes())
    proof_gate.joinpath("target.sv").write_bytes(gate.joinpath("target.sv").read_bytes())
    gold_hash = source_snapshot_hash(proof_gold, ("target.sv",))
    gate_hash = source_snapshot_hash(proof_gate, ("target.sv",))
    plan = compose_strict_equivalence(
        gold_root=proof_gold,
        gate_root=proof_gate,
        source_paths=("target.sv",),
        top="target",
        formal_model=_model(gold_hash, gate_hash),
    )

    manifest = build_composition_manifest(
        candidate_id="cand_priority_mux",
        parent_root=gold,
        candidate_root=gate,
        full_source_paths=("helper.sv", "target.sv"),
        changed_source_paths=("target.sv",),
        changed_span_ids=("source_target",),
        proof_plan=plan,
        parameterizations=("FAMILY=0", "FAMILY=1", "FAMILY=2", "FAMILY=3", "FAMILY=4"),
        boundary_inputs=("lane_input",),
        boundary_outputs=("lane_output",),
        state_elements=(),
        assumption_hashes=(_hash("5"), _hash("6")),
        discharge_obligations=(
            "ALL_INSTANTIATIONS_COVERED",
            "UNCHANGED_FILES_BYTE_IDENTICAL",
            "NO_STATE_IN_SCOPE",
        ),
    )

    assert manifest.changed_source_paths == ("target.sv",)
    assert manifest.unchanged_source_hashes == {"helper.sv": _hash_text(unchanged)}
    assert manifest.proof_plan_hash == plan.plan_hash
    assert manifest.manifest_hash == canonical_sha256(
        manifest, exclude=frozenset({"manifest_hash"})
    )

    gate.joinpath("helper.sv").write_text("module changed_helper; endmodule\n")
    with pytest.raises(FormalCompositionError, match="outside compositional scope"):
        build_composition_manifest(
            candidate_id="cand_priority_mux",
            parent_root=gold,
            candidate_root=gate,
            full_source_paths=("helper.sv", "target.sv"),
            changed_source_paths=("target.sv",),
            changed_span_ids=("source_target",),
            proof_plan=plan,
            parameterizations=("FAMILY=0",),
            boundary_inputs=("lane_input",),
            boundary_outputs=("lane_output",),
            state_elements=(),
            assumption_hashes=(_hash("5"), _hash("6")),
            discharge_obligations=("UNCHANGED_FILES_BYTE_IDENTICAL",),
        )
