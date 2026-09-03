from __future__ import annotations

import pytest

from nova_rtl.evidence.execution import EvidenceExecutionError, normalize_evidence_stages


def test_evidence_stage_set_is_complete_and_order_independent() -> None:
    assert normalize_evidence_stages((" opportunities ", "EVIDENCE")) == (
        "evidence",
        "opportunities",
    )


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        (("evidence",), "missing: opportunities"),
        (("evidence", "opportunities", "yosys"), "unknown M3 stage aliases: yosys"),
        ((), "missing: evidence, opportunities"),
    ],
)
def test_evidence_stage_set_fails_closed(
    stages: tuple[str, ...], message: str
) -> None:
    with pytest.raises(EvidenceExecutionError, match=message):
        normalize_evidence_stages(stages)
