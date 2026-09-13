from __future__ import annotations

import pytest

from nova_rtl.transforms.fsm_decode import (
    FsmDecodeContext,
    FsmDecodeError,
    FsmDecodeParameters,
    RestructureFsmDecode,
)

SOURCE = (
    "if (state == IDLE) begin next_state = RUN; busy = 1'b0; end "
    "else if (state == RUN) begin next_state = IDLE; busy = 1'b1; end "
    "else begin next_state = IDLE; busy = 1'b0; end"
)


def _context(source: str = SOURCE) -> FsmDecodeContext:
    return FsmDecodeContext(
        relative_path="rtl/fsm_decode.sv",
        source_text=source,
        start_line=8,
        start_column=5,
        end_line=8,
        end_column=5 + len(source),
        owner_hierarchy="scheduler_fsm",
        expected_owner_hierarchy="scheduler_fsm",
        clock_domain_ids=("clk_scheduler",),
        protected_neighbor_ids=(),
        state_signal="state",
        state_width_verified=True,
        state_register_protected=True,
        reset_semantics_verified=True,
        enable_semantics_verified=True,
        complete_output_assignments=True,
    )


def test_fsm_decode_rewrite_is_deterministic_and_does_not_edit_state_storage() -> None:
    capability = RestructureFsmDecode()
    parameters = FsmDecodeParameters(max_states=8)
    match = capability.match(_context())

    first = capability.rewrite(match, parameters)
    second = capability.rewrite(match, parameters)

    assert first == second
    assert first.replacement == (
        "case (state)\n"
        "  IDLE: begin next_state = RUN; busy = 1'b0; end\n"
        "  RUN: begin next_state = IDLE; busy = 1'b1; end\n"
        "  default: begin next_state = IDLE; busy = 1'b0; end\n"
        "endcase"
    )
    assert "state <=" not in first.replacement
    assert capability.metadata.correctness_contract == "STRICT_SEQ_EQUIV"
    assert "REDUCE_FSM_DECODE_LEVELS" in capability.metadata.expected_structural_effects
    with pytest.raises(FsmDecodeError, match="if/else"):
        capability.match(_context(first.replacement))


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        ("00", "IDLE"),
        ("01", "RUN"),
        ("10", "DEFAULT"),
        ("11", "DEFAULT"),
        ("0x", "DEFAULT"),
        ("zz", "DEFAULT"),
    ),
)
def test_fsm_decode_case_preserves_branch_selection(state: str, expected: str) -> None:
    labels = {"IDLE": "00", "RUN": "01"}

    assert RestructureFsmDecode.select_if_branch(state, labels) == expected
    assert RestructureFsmDecode.select_case_branch(state, labels) == expected


@pytest.mark.parametrize(
    ("source", "changes", "message"),
    (
        (SOURCE.replace("== IDLE", "=== IDLE"), {}, "equality"),
        (SOURCE.rsplit(" else begin", 1)[0], {}, "complete final else"),
        (SOURCE.replace("== IDLE", "== 2'bx1", 1), {}, "state label"),
        (SOURCE, {"state_width_verified": False}, "width"),
        (SOURCE, {"state_register_protected": False}, "state register"),
        (SOURCE, {"reset_semantics_verified": False}, "reset"),
        (SOURCE, {"enable_semantics_verified": False}, "enable"),
        (SOURCE, {"complete_output_assignments": False}, "output"),
        (SOURCE, {"protected_neighbor_ids": ("reset_sync",)}, "protected"),
        (SOURCE, {"clock_domain_ids": ("clk_a", "clk_b")}, "clock domain"),
    ),
)
def test_fsm_decode_rejects_unsafe_or_incomplete_cases(
    source: str, changes: dict[str, object], message: str
) -> None:
    context = _context(source).model_copy(update=changes)

    with pytest.raises(FsmDecodeError, match=message):
        RestructureFsmDecode().match(context)


def test_fsm_decode_preflight_enforces_state_bound() -> None:
    source = (
        "if (state == S0) begin next_state = S1; end "
        "else if (state == S1) begin next_state = S2; end "
        "else if (state == S2) begin next_state = S3; end "
        "else begin next_state = S0; end"
    )
    capability = RestructureFsmDecode()
    match = capability.match(_context(source))

    with pytest.raises(FsmDecodeError, match="configured bound"):
        capability.preflight(match, FsmDecodeParameters(max_states=2))
