"""Compile normalized failures into machine-checkable repair directives."""

from __future__ import annotations

from hashlib import sha256

from nova_rtl.contracts.base import EvidenceRef, canonical_sha256
from nova_rtl.contracts.recovery import FailureEvent, RepairDirective
from nova_rtl.recovery.policy import RecoveryPolicyRegistry


class DirectiveCompilationError(ValueError):
    pass


DIRECTIVE_SEMANTICS: dict[str, dict[str, tuple[str, ...] | str]] = {
    "CRITICAL_PATH_MIGRATION": {
        "scope": "NEW_OR_SHARED_TIMING_CONE",
        "observed": ("target path improved while a sibling path became dominant",),
        "preserve": ("successful_local_mechanism", "constraint_identity"),
        "prohibit": ("identical_target_repetition", "protected_structure_edit"),
        "roles": ("timing_forensics",),
    },
    "PROTECTED_STRUCTURE_VIOLATION": {
        "scope": "NO_RTL_MUTATION",
        "observed": ("protected clock reset or CDC structure changed",),
        "preserve": ("baseline_protected_structure",),
        "prohibit": ("rtl_mutation", "constraint_edit"),
        "roles": (),
    },
    "CONSTRAINT_BINDING_DELTA": {
        "scope": "NO_RTL_MUTATION",
        "observed": ("immutable constraint selectors changed binding",),
        "preserve": ("constraint_identity",),
        "prohibit": ("constraint_edit", "rtl_mutation"),
        "roles": (),
    },
    "CDC_INVARIANT_DELTA": {
        "scope": "SAFE_TRANSFORM_OR_NEW_TARGET",
        "observed": ("approved CDC structural inventory changed",),
        "preserve": ("approved_cdc_fingerprint",),
        "prohibit": ("cdc_structure_edit", "constraint_edit"),
        "roles": (),
    },
    "INFRASTRUCTURE_TRANSIENT": {
        "scope": "IDENTICAL_TOOL_RETRY",
        "observed": ("tool execution ended with a transient infrastructure result",),
        "preserve": ("rtl_snapshot", "tool_recipe"),
        "prohibit": ("rtl_mutation", "recipe_mutation"),
        "roles": (),
    },
    "FORMAL_SEMANTIC_FAILURE": {
        "scope": "FORMAL_AWARE_LOCAL_REVISION",
        "observed": ("formal counterexample disproved the candidate contract",),
        "preserve": ("formal_model_identity", "constraint_identity"),
        "prohibit": ("assumption_weakening", "protected_structure_edit"),
        "roles": ("formal_critic",),
    },
    "AREA_POLICY_VIOLATION": {
        "scope": "LOWER_COST_LOCAL_REVISION",
        "observed": ("candidate exceeded the mapped or physical area policy",),
        "preserve": ("functional_contract", "constraint_identity"),
        "prohibit": ("budget_increase", "protected_structure_edit"),
        "roles": ("logic_specialist",),
    },
}


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{sha256(chr(0).join(parts).encode()).hexdigest()[:20]}"


def compile_directive(
    failure: FailureEvent,
    evidence: tuple[EvidenceRef, ...],
    rules: RecoveryPolicyRegistry,
) -> RepairDirective:
    """Compile semantic guidance without changing classification or authority."""

    rule = rules.rules[failure.failure_family]
    kinds = {item.kind for item in evidence}
    missing = set(rule.required_evidence_kinds) - kinds
    if missing:
        raise DirectiveCompilationError(
            f"required evidence kinds are missing: {', '.join(sorted(missing))}"
        )
    supplied_ids = {item.evidence_id for item in evidence}
    primary_ids = {item.evidence_id for item in failure.primary_evidence_refs}
    if not primary_ids.issubset(supplied_ids):
        raise DirectiveCompilationError("failure primary evidence is not in compiler input")

    semantics = DIRECTIVE_SEMANTICS.get(
        failure.failure_family,
        {
            "scope": "POLICY_BOUNDED_RECOVERY",
            "observed": (f"normalized failure {failure.failure_family}",),
            "preserve": ("functional_contract", "constraint_identity"),
            "prohibit": ("protected_structure_edit", "budget_increase"),
            "roles": (),
        },
    )
    recommended = (
        ("REJECT_CANDIDATE",)
        if failure.failure_family == "PROTECTED_STRUCTURE_VIOLATION"
        else (rule.allowed_actions[0],)
    )
    directive_id = _stable_id(
        "directive",
        failure.failure_event_id,
        rule.rule_id,
        rules.policy_hash,
        canonical_sha256(
            {
                "semantics": semantics,
                "recommended": recommended,
                "evidence_ids": tuple(sorted(supplied_ids)),
            }
        ),
    )
    return RepairDirective(
        repair_directive_id=directive_id,
        failure_event_id=failure.failure_event_id,
        allowed_scope=str(semantics["scope"]),
        observed=tuple(semantics["observed"]),
        preserve=tuple(semantics["preserve"]),
        prohibit=tuple(semantics["prohibit"]),
        recommended_actions=recommended,
        recommended_roles=tuple(semantics["roles"]),
        evidence_refs=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        compiler_rule_refs=(rule.rule_id,),
    )


__all__ = ["DirectiveCompilationError", "compile_directive"]
