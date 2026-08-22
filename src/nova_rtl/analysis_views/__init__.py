"""Fail-closed analysis-view comparison and aggregation authority."""

from nova_rtl.analysis_views.aggregation import (
    AggregatedMetrics,
    IncompleteRequiredViewError,
    aggregate_required_views,
)
from nova_rtl.analysis_views.comparability import (
    ComparabilityResult,
    IncomparableResultsError,
    assert_comparable,
)

__all__ = [
    "AggregatedMetrics",
    "ComparabilityResult",
    "IncomparableResultsError",
    "IncompleteRequiredViewError",
    "aggregate_required_views",
    "assert_comparable",
]
