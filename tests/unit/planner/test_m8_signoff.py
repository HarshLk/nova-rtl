from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.contracts.planning import M8GateEvidence
from nova_rtl.optimization import council_signoff


def _gate(content: bytes, *, size_delta: int = 0) -> M8GateEvidence:
    return M8GateEvidence(
        evidence_id="m8_council_safety_matrix",
        argv=council_signoff._council_matrix_argv(),
        output_relative_path="reports/m8-council-safety-matrix.txt",
        output_hash="sha256:" + sha256(content).hexdigest(),
        output_size_bytes=len(content) + size_delta,
        passed_test_count=30,
    )


def test_council_matrix_evidence_preserves_exact_normalized_output(tmp_path: Path) -> None:
    raw = b".............................. [100%]\n30 passed in 2.91s\n"
    content = council_signoff._normalize_gate_output(raw)
    output = tmp_path / "reports/m8-council-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(content)

    council_signoff._verify_gate_evidence(tmp_path, _gate(content))
    assert b"30 passed in <elapsed>" in content


def test_council_matrix_rejects_changed_output(tmp_path: Path) -> None:
    content = b"30 passed in <elapsed>\n"
    output = tmp_path / "reports/m8-council-safety-matrix.txt"
    output.parent.mkdir()
    output.write_bytes(b"29 passed in <elapsed>\n")

    with pytest.raises(council_signoff.M8SignoffError, match="identity changed"):
        council_signoff._verify_gate_evidence(tmp_path, _gate(content))
