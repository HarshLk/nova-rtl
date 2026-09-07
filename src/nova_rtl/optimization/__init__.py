"""M4 deterministic strict-equivalence optimization flow."""

from nova_rtl.optimization.flow import (
    M4CandidateBundle,
    OptimizationFlowError,
    inspect_candidate,
    optimize_strict_vertical_slice,
    verify_candidate_bundle,
)

__all__ = [
    "M4CandidateBundle",
    "OptimizationFlowError",
    "inspect_candidate",
    "optimize_strict_vertical_slice",
    "verify_candidate_bundle",
]
