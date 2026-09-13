from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.contracts.planning import M6GateEvidence
from nova_rtl.optimization import planner_signoff as signoff


def _evidence(content: bytes, *, size_delta: int = 0) -> M6GateEvidence:
    return M6GateEvidence(
        evidence_id="m6_planner_safety_matrix",
        argv=signoff._planner_matrix_argv(),
        output_relative_path="reports/m6-planner-safety-matrix.txt",
        output_hash="sha256:" + sha256(content).hexdigest(),
        output_size_bytes=len(content) + size_delta,
        passed_test_count=27,
    )


def test_planner_matrix_evidence_verifies_exact_preserved_output(tmp_path: Path) -> None:
    content = b"........................... [100%]\n27 passed in 1.00s\n"
    output = tmp_path / "reports" / "m6-planner-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(content)

    signoff._verify_gate_evidence(tmp_path, _evidence(content))


def test_planner_matrix_evidence_rejects_changed_bytes(tmp_path: Path) -> None:
    content = b"27 passed in 1.00s\n"
    output = tmp_path / "reports" / "m6-planner-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(b"26 passed in 1.00s\n")

    with pytest.raises(signoff.M6SignoffError, match="identity changed"):
        signoff._verify_gate_evidence(tmp_path, _evidence(content))
