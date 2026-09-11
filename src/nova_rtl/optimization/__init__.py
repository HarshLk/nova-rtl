"""M4 deterministic strict-equivalence optimization flow."""

from nova_rtl.optimization.flow import (
    M4CandidateBundle,
    M4StoredCandidateBundle,
    M4TerminalCandidateBundle,
    OptimizationFlowError,
    inspect_candidate,
    optimize_strict_vertical_slice,
    verify_candidate_bundle,
)
from nova_rtl.optimization.signoff import (
    M4SignoffError,
    run_m4_signoff,
    verify_m4_dependency_snapshot,
    verify_m4_signoff,
)

__all__ = [
    "M4CandidateBundle",
    "M4StoredCandidateBundle",
    "M4TerminalCandidateBundle",
    "OptimizationFlowError",
    "M4SignoffError",
    "inspect_candidate",
    "optimize_strict_vertical_slice",
    "run_m4_signoff",
    "verify_m4_dependency_snapshot",
    "verify_candidate_bundle",
    "verify_m4_signoff",
]
