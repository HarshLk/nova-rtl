"""Deterministic factoring of verified pure common bitwise predicates."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import EntityId, NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.transforms.registry import TransformCapabilityMetadata
from nova_rtl.transforms.syntax import SourceEdit


class PredicateError(ValueError):
    """The proposed common-predicate rewrite is not semantically safe."""


class PredicateParameters(StrictContract):
    max_terms: int = Field(default=16, strict=True, ge=3, le=64)
    max_fanout: int = Field(default=32, strict=True, ge=1, le=1024)


class PredicateContext(StrictContract):
    relative_path: NonEmptyString
    source_text: NonEmptyString
    start_line: int = Field(strict=True, ge=1)
    start_column: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    end_column: int = Field(strict=True, ge=1)
    owner_hierarchy: NonEmptyString
    expected_owner_hierarchy: NonEmptyString
    clock_domain_ids: tuple[EntityId, ...]
    protected_neighbor_ids: tuple[EntityId, ...]
    equal_widths_verified: bool
    unsigned_verified: bool
    predicate_pure: bool
    four_state_distributive: bool
    estimated_fanout_after: int = Field(strict=True, ge=1)

    @field_validator("clock_domain_ids", "protected_neighbor_ids")
    @classmethod
    def identities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context identities must be unique")
        return value

    @model_validator(mode="after")
    def source_range_is_nonempty(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("predicate source range must be nonempty")
        return self


class PredicateMatch(StrictContract):
    context: PredicateContext
    assignment_target: NonEmptyString
    predicate: NonEmptyString
    values: tuple[NonEmptyString, ...] = Field(min_length=3, max_length=64)
    match_hash: str

    @model_validator(mode="after")
    def hash_is_valid(self) -> Self:
        if self.match_hash != canonical_sha256(
            self, exclude=frozenset({"match_hash"})
        ):
            raise ValueError("predicate match hash is not canonical")
        return self


_ASSIGNMENT = re.compile(
    r"^\s*(?P<target>[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]\r\n]+\])?)\s*=\s*"
    r"(?P<expression>[^;]+)\s*;\s*$"
)
_TERM = re.compile(
    r"^\(\s*(?P<predicate>[A-Za-z_][A-Za-z0-9_$.]*(?:\[[^\]\r\n]+\])?)"
    r"\s*&\s*(?P<value>[A-Za-z_][A-Za-z0-9_$.]*(?:\[[^\]\r\n]+\])?)\s*\)$"
)


def _four_state_binary(operator: str, left: str, right: str) -> str:
    lhs = "x" if left == "z" else left
    rhs = "x" if right == "z" else right
    if operator == "&":
        if "0" in (lhs, rhs):
            return "0"
        return "1" if lhs == rhs == "1" else "x"
    if "1" in (lhs, rhs):
        return "1"
    return "0" if lhs == rhs == "0" else "x"


def _fold(operator: str, values: tuple[str, ...]) -> str:
    result = values[0]
    for value in values[1:]:
        result = _four_state_binary(operator, result, value)
    return result


class FactorCommonPredicate:
    """Factor a repeated pure predicate without changing bitwise semantics."""

    parameter_model: ClassVar[type[PredicateParameters]] = PredicateParameters
    metadata: ClassVar[TransformCapabilityMetadata] = TransformCapabilityMetadata(
        operation="FACTOR_COMMON_PREDICATE",
        family="LOGIC_RESTRUCTURING",
        correctness_contract="STRICT_SEQ_EQUIV",
        implementation_version="transform:v1:common_predicate",
        allowed_ast_node_kinds=("ExpressionStatement",),
        prohibited_contexts=(
            "AMBIGUOUS_X_SEMANTICS",
            "CLOCK_LOGIC",
            "FANOUT_LIMIT_EXCEEDED",
            "IMPURE_PREDICATE",
            "MULTI_CLOCK_CONE",
            "PROTECTED_NODE",
            "RESET_LOGIC",
            "SIGNED_OR_WIDTH_AMBIGUITY",
        ),
        expected_structural_effects=("REDUCE_DUPLICATE_PREDICATE",),
        conflicts=(),
    )

    def match(self, context: PredicateContext) -> PredicateMatch:
        if context.owner_hierarchy != context.expected_owner_hierarchy:
            raise PredicateError("source owner does not match proposal owner hierarchy")
        if context.protected_neighbor_ids:
            raise PredicateError("predicate expression has a protected neighbor")
        if len(context.clock_domain_ids) != 1:
            raise PredicateError("predicate expression must belong to one clock domain")
        if not context.equal_widths_verified:
            raise PredicateError("operand width equality is not verified")
        if not context.unsigned_verified:
            raise PredicateError("unsigned operand interpretation is not verified")
        if not context.predicate_pure:
            raise PredicateError("common predicate is not proven pure")
        if not context.four_state_distributive:
            raise PredicateError("four-state distributivity is not verified")
        if "&&" in context.source_text or "||" in context.source_text:
            raise PredicateError("only bitwise predicate factoring is supported")
        if re.search(r"'[sS]?[bBoOdDhH][^\s;]*[xXzZ?]", context.source_text):
            raise PredicateError("X/Z literal makes predicate factoring ambiguous")
        assignment = _ASSIGNMENT.fullmatch(context.source_text)
        if assignment is None:
            raise PredicateError("expected a repeated predicate assignment")
        terms = tuple(item.strip() for item in assignment.group("expression").split("|"))
        parsed = tuple(_TERM.fullmatch(term) for term in terms)
        if len(terms) < 3 or any(item is None for item in parsed):
            raise PredicateError("expected a repeated predicate bitwise expression")
        predicates = tuple(item.group("predicate") for item in parsed if item is not None)
        if len(set(predicates)) != 1:
            raise PredicateError("terms do not share one common predicate")
        values = tuple(item.group("value") for item in parsed if item is not None)
        payload = {
            "context": context.model_dump(mode="json"),
            "assignment_target": assignment.group("target"),
            "predicate": predicates[0],
            "values": values,
        }
        return PredicateMatch(
            context=context,
            assignment_target=assignment.group("target"),
            predicate=predicates[0],
            values=values,
            match_hash=canonical_sha256(payload),
        )

    def preflight(
        self, match: PredicateMatch, parameters: PredicateParameters
    ) -> None:
        if len(match.values) > parameters.max_terms:
            raise PredicateError("predicate term count exceeds configured bound")
        if match.context.estimated_fanout_after > parameters.max_fanout:
            raise PredicateError("factored predicate exceeds configured fanout")

    def rewrite(
        self, match: PredicateMatch, parameters: PredicateParameters
    ) -> SourceEdit:
        self.preflight(match, parameters)
        values = " | ".join(match.values)
        replacement = f"{match.assignment_target} = {match.predicate} & ({values});"
        return SourceEdit(
            relative_path=match.context.relative_path,
            start_line=match.context.start_line,
            start_column=match.context.start_column,
            end_line=match.context.end_line,
            end_column=match.context.end_column,
            expected_text_hash="sha256:"
            + sha256(match.context.source_text.encode("utf-8")).hexdigest(),
            replacement=replacement,
            ast_node_kind="ExpressionStatement",
        )

    def fingerprint(
        self, match: PredicateMatch, parameters: PredicateParameters
    ) -> str:
        identity = canonical_sha256(
            {
                "implementation_version": self.metadata.implementation_version,
                "match_hash": match.match_hash,
                "parameters": parameters.model_dump(mode="json"),
            }
        )
        return "common_predicate:v1:" + identity.removeprefix("sha256:")

    @staticmethod
    def evaluate_serial(predicate: str, values: tuple[str, ...]) -> str:
        products = tuple(
            _four_state_binary("&", predicate, value) for value in values
        )
        return _fold("|", products)

    @staticmethod
    def evaluate_factored(predicate: str, values: tuple[str, ...]) -> str:
        return _four_state_binary("&", predicate, _fold("|", values))


__all__ = [
    "FactorCommonPredicate",
    "PredicateContext",
    "PredicateError",
    "PredicateMatch",
    "PredicateParameters",
]
