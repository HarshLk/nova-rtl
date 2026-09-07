"""Formal compilation-identity and multiclock harness preflights."""

from nova_rtl.formal.compose import (
    FormalCompositionError,
    StrictEquivalencePlan,
    compose_strict_equivalence,
    run_strict_equivalence,
    source_snapshot_hash,
)
from nova_rtl.formal.harness import (
    MulticlockHarnessAssets,
    MulticlockHarnessContract,
    render_multiclock_harness,
)
from nova_rtl.formal.preflight import (
    CompilationDefine,
    FormalCompilationIdentity,
    preflight_formal_model,
)

__all__ = [
    "CompilationDefine",
    "FormalCompositionError",
    "FormalCompilationIdentity",
    "MulticlockHarnessAssets",
    "MulticlockHarnessContract",
    "StrictEquivalencePlan",
    "compose_strict_equivalence",
    "preflight_formal_model",
    "render_multiclock_harness",
    "run_strict_equivalence",
    "source_snapshot_hash",
]
