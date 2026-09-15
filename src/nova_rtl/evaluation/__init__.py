"""Ordered candidate evaluation, feasibility, and release experiments."""

from nova_rtl.evaluation.ablation import compare_variants
from nova_rtl.evaluation.cascade import EvaluationCascade
from nova_rtl.evaluation.feasibility import is_feasible
from nova_rtl.evaluation.frequency import evaluate_frequency_sweep

__all__ = [
    "EvaluationCascade",
    "compare_variants",
    "evaluate_frequency_sweep",
    "is_feasible",
]
