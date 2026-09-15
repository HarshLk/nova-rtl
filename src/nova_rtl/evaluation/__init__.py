"""Ordered candidate evaluation, feasibility, and release experiments."""

from nova_rtl.evaluation.ablation import compare_variants
from nova_rtl.evaluation.acceptance import evaluate_acceptance
from nova_rtl.evaluation.cascade import EvaluationCascade
from nova_rtl.evaluation.feasibility import is_feasible
from nova_rtl.evaluation.frequency import evaluate_frequency_sweep
from nova_rtl.evaluation.reference import seal_reference_candidate

__all__ = [
    "EvaluationCascade",
    "compare_variants",
    "evaluate_acceptance",
    "evaluate_frequency_sweep",
    "is_feasible",
    "seal_reference_candidate",
]
