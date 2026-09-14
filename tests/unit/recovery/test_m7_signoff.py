from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.contracts.recovery import M7GateEvidence
from nova_rtl.recovery import signoff


def _gate(content: bytes, *, size_delta: int = 0) -> M7GateEvidence:
    return M7GateEvidence(
        evidence_id="m7_recovery_safety_matrix",
        argv=signoff._recovery_matrix_argv(),
        output_relative_path="reports/m7-recovery-safety-matrix.txt",
        output_hash="sha256:" + sha256(content).hexdigest(),
        output_size_bytes=len(content) + size_delta,
        passed_test_count=33,
    )


def test_recovery_matrix_evidence_preserves_exact_output(tmp_path: Path) -> None:
    content = b"................................. [100%]\n33 passed in 1.00s\n"
    output = tmp_path / "reports/m7-recovery-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(content)

    signoff._verify_gate_evidence(tmp_path, _gate(content))


def test_recovery_matrix_rejects_changed_output(tmp_path: Path) -> None:
    content = b"33 passed in 1.00s\n"
    output = tmp_path / "reports/m7-recovery-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(b"32 passed in 1.00s\n")

    with pytest.raises(signoff.M7SignoffError, match="identity changed"):
        signoff._verify_gate_evidence(tmp_path, _gate(content))


def test_m7_input_hash_normalizes_nested_contracts() -> None:
    content = b"33 passed in 1.00s\n"
    first = signoff._input_set_hash({"gate": _gate(content), "policy": "m7"})
    second = signoff._input_set_hash(
        {"gate": _gate(content).model_dump(mode="json"), "policy": "m7"}
    )

    assert first == second
