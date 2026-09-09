"""Fail-closed analysis-view comparison and aggregation authority."""

from nova_rtl.analysis_views.aggregation import (
    AggregatedMetrics,
    IncompleteRequiredViewError,
    aggregate_required_views,
)
from nova_rtl.analysis_views.comparability import (
    ApprovedIdentityRemap,
    ComparabilityResult,
    IncomparableResultsError,
    assert_comparable,
)

__all__ = [
    "ApprovedIdentityRemap",
    "AggregatedMetrics",
    "ComparabilityResult",
    "IncomparableResultsError",
    "IncompleteRequiredViewError",
    "aggregate_required_views",
    "assert_comparable",
]
