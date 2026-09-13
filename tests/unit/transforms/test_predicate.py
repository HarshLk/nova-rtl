from __future__ import annotations

import itertools

import pytest

from nova_rtl.transforms.predicate import (
    FactorCommonPredicate,
    PredicateContext,
    PredicateError,
    PredicateParameters,
)

SOURCE = "result = (enable & a) | (enable & b) | (enable & c);"


def _context(source: str = SOURCE) -> PredicateContext:
    return PredicateContext(
        relative_path="rtl/predicate.sv",
        source_text=source,
        start_line=5,
        start_column=5,
        end_line=5,
        end_column=5 + len(source),
        owner_hierarchy="predicate",
        expected_owner_hierarchy="predicate",
        clock_domain_ids=("clk_scheduler",),
        protected_neighbor_ids=(),
        equal_widths_verified=True,
        unsigned_verified=True,
        predicate_pure=True,
        four_state_distributive=True,
        estimated_fanout_after=3,
    )


def test_predicate_rewrite_is_deterministic_factored_and_idempotent() -> None:
    capability = FactorCommonPredicate()
    parameters = PredicateParameters(max_terms=8, max_fanout=8)
    match = capability.match(_context())

    first = capability.rewrite(match, parameters)
    second = capability.rewrite(match, parameters)

    assert first == second
    assert first.replacement == "result = enable & (a | b | c);"
    assert capability.metadata.correctness_contract == "STRICT_SEQ_EQUIV"
    assert "REDUCE_DUPLICATE_PREDICATE" in capability.metadata.expected_structural_effects
    with pytest.raises(PredicateError, match="repeated predicate"):
        capability.match(_context(first.replacement))


def test_predicate_factoring_preserves_exhaustive_four_state_results() -> None:
    capability = FactorCommonPredicate()

    for predicate, *values in itertools.product(("0", "1", "x", "z"), repeat=4):
        value_tuple = tuple(values)
        assert capability.evaluate_serial(predicate, value_tuple) == (
            capability.evaluate_factored(predicate, value_tuple)
        )


@pytest.mark.parametrize(
    ("source", "changes", "message"),
    (
        ("result = (enable & a) | (other & b) | (enable & c);", {}, "common"),
        ("result = (enable && a) || (enable && b) || (enable && c);", {}, "bitwise"),
        ("result = (enable & a) | (enable & 1'bx) | (enable & c);", {}, "X/Z"),
        (SOURCE, {"equal_widths_verified": False}, "width"),
        (SOURCE, {"unsigned_verified": False}, "unsigned"),
        (SOURCE, {"predicate_pure": False}, "pure"),
        (SOURCE, {"four_state_distributive": False}, "four-state"),
        (SOURCE, {"protected_neighbor_ids": ("reset_sync",)}, "protected"),
        (SOURCE, {"clock_domain_ids": ("clk_a", "clk_b")}, "clock domain"),
    ),
)
def test_predicate_rejects_unsafe_or_semantically_different_cases(
    source: str, changes: dict[str, object], message: str
) -> None:
    context = _context(source).model_copy(update=changes)

    with pytest.raises(PredicateError, match=message):
        FactorCommonPredicate().match(context)


def test_predicate_preflight_enforces_term_and_fanout_limits() -> None:
    capability = FactorCommonPredicate()
    match = capability.match(_context())

    with pytest.raises(PredicateError, match="fanout"):
        capability.preflight(match, PredicateParameters(max_terms=8, max_fanout=2))
    with pytest.raises(PredicateError, match="term count"):
        capability.preflight(
            capability.match(
                _context(
                    "result = (enable & a) | (enable & b) | "
                    "(enable & c) | (enable & d) | (enable & e);"
                )
            ),
            PredicateParameters(max_terms=4, max_fanout=8),
        )
