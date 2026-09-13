from __future__ import annotations

import itertools

import pytest

from nova_rtl.transforms.boolean_tree import (
    BalanceBooleanTree,
    BooleanTreeContext,
    BooleanTreeError,
    BooleanTreeParameters,
)


def _context(source: str = "result = a & b & c & d;") -> BooleanTreeContext:
    return BooleanTreeContext(
        relative_path="rtl/boolean_tree.sv",
        source_text=source,
        start_line=4,
        start_column=5,
        end_line=4,
        end_column=5 + len(source),
        owner_hierarchy="boolean_tree",
        expected_owner_hierarchy="boolean_tree",
        clock_domain_ids=("clk_compute",),
        protected_neighbor_ids=(),
        operand_width=1,
        equal_widths_verified=True,
        unsigned_verified=True,
        four_state_associative=True,
    )


def test_boolean_tree_rewrite_is_deterministic_balanced_and_idempotent() -> None:
    capability = BalanceBooleanTree()
    parameters = BooleanTreeParameters(operator="&", max_operands=8)
    match = capability.match(_context())

    first = capability.rewrite(match, parameters)
    second = capability.rewrite(match, parameters)

    assert first == second
    assert first.replacement == "result = ((a & b) & (c & d));"
    assert capability.metadata.correctness_contract == "STRICT_SEQ_EQUIV"
    assert "REDUCE_BOOLEAN_DEPTH" in capability.metadata.expected_structural_effects
    with pytest.raises(BooleanTreeError, match="flat associative chain"):
        capability.match(_context(first.replacement))


def test_boolean_tree_balancing_preserves_exhaustive_four_state_results() -> None:
    capability = BalanceBooleanTree()
    match = capability.match(_context())

    for values in itertools.product(("0", "1", "x", "z"), repeat=4):
        assert capability.evaluate_serial(
            match.operator, values
        ) == capability.evaluate_balanced(match.operator, values)


@pytest.mark.parametrize(
    ("source", "changes", "message"),
    (
        ("result = a && b && c && d;", {}, "bitwise"),
        ("result = a & b | c & d;", {}, "single associative operator"),
        ("result = a & 1'bx & c & d;", {}, "X/Z literal"),
        ("result = a & b & c & d;", {"equal_widths_verified": False}, "width"),
        ("result = a & b & c & d;", {"unsigned_verified": False}, "unsigned"),
        ("result = a & b & c & d;", {"four_state_associative": False}, "four-state"),
        ("result = a & b & c & d;", {"protected_neighbor_ids": ("cdc_sync",)}, "protected"),
        ("result = a & b & c & d;", {"clock_domain_ids": ("clk_a", "clk_b")}, "clock domain"),
    ),
)
def test_boolean_tree_rejects_unsafe_or_ambiguous_cases(
    source: str, changes: dict[str, object], message: str
) -> None:
    context = _context(source).model_copy(update=changes)

    with pytest.raises(BooleanTreeError, match=message):
        BalanceBooleanTree().match(context)


def test_boolean_tree_preflight_enforces_operand_bounds() -> None:
    capability = BalanceBooleanTree()
    match = capability.match(_context("result = a & b & c & d & e;"))

    with pytest.raises(BooleanTreeError, match="configured bound"):
        capability.preflight(
            match,
            BooleanTreeParameters(operator="&", max_operands=4),
        )
