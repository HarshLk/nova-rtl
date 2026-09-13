"""Deterministic balancing of explicitly safe associative bitwise trees."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import EntityId, NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.transforms.registry import TransformCapabilityMetadata
from nova_rtl.transforms.syntax import SourceEdit


class BooleanTreeError(ValueError):
    """The proposed boolean tree is not safe for deterministic balancing."""


class BooleanTreeParameters(StrictContract):
    operator: Literal["&", "|", "^"]
    max_operands: int = Field(default=16, strict=True, ge=4, le=64)


class BooleanTreeContext(StrictContract):
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
    operand_width: int = Field(strict=True, gt=0)
    equal_widths_verified: bool
    unsigned_verified: bool
    four_state_associative: bool

    @field_validator("clock_domain_ids", "protected_neighbor_ids")
    @classmethod
    def identities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context identities must be unique")
        return value

    @model_validator(mode="after")
    def source_range_is_nonempty(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("boolean tree source range must be nonempty")
        return self


class BooleanTreeMatch(StrictContract):
    context: BooleanTreeContext
    assignment_target: NonEmptyString
    operator: Literal["&", "|", "^"]
    operands: tuple[NonEmptyString, ...] = Field(min_length=4, max_length=64)
    match_hash: str

    @model_validator(mode="after")
    def hash_is_valid(self) -> Self:
        if self.match_hash != canonical_sha256(self, exclude=frozenset({"match_hash"})):
            raise ValueError("boolean tree match hash is not canonical")
        return self


_ASSIGNMENT = re.compile(
    r"^\s*(?P<target>[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]\r\n]+\])?)\s*=\s*"
    r"(?P<expression>[^;]+)\s*;\s*$"
)
_OPERAND = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_$.]*(?:\[[^\]\r\n]+\])?|"
    r"\d+(?:'[sS]?[bBoOdDhH][0-9a-fA-F_]+)?)$"
)


def _balanced(operator: str, operands: tuple[str, ...]) -> str:
    if len(operands) == 1:
        return operands[0]
    midpoint = len(operands) // 2
    left = _balanced(operator, operands[:midpoint])
    right = _balanced(operator, operands[midpoint:])
    return f"({left} {operator} {right})"


def _four_state_binary(operator: str, left: str, right: str) -> str:
    lhs = "x" if left == "z" else left
    rhs = "x" if right == "z" else right
    if operator == "&":
        if "0" in (lhs, rhs):
            return "0"
        return "1" if lhs == rhs == "1" else "x"
    if operator == "|":
        if "1" in (lhs, rhs):
            return "1"
        return "0" if lhs == rhs == "0" else "x"
    if "x" in (lhs, rhs):
        return "x"
    return "1" if lhs != rhs else "0"


class BalanceBooleanTree:
    """Balance flat bitwise chains only when semantic safety is pre-established."""

    parameter_model: ClassVar[type[BooleanTreeParameters]] = BooleanTreeParameters
    metadata: ClassVar[TransformCapabilityMetadata] = TransformCapabilityMetadata(
        operation="BALANCE_BOOLEAN_TREE",
        family="LOGIC_RESTRUCTURING",
        correctness_contract="STRICT_SEQ_EQUIV",
        implementation_version="transform:v1:boolean_tree",
        allowed_ast_node_kinds=("ExpressionStatement",),
        prohibited_contexts=(
            "AMBIGUOUS_X_SEMANTICS",
            "CLOCK_LOGIC",
            "MIXED_OPERATOR_TREE",
            "MULTI_CLOCK_CONE",
            "PROTECTED_NODE",
            "RESET_LOGIC",
            "SHORT_CIRCUIT_EXPRESSION",
            "SIGNED_OR_WIDTH_AMBIGUITY",
        ),
        expected_structural_effects=("REDUCE_BOOLEAN_DEPTH",),
        conflicts=(),
    )

    def match(self, context: BooleanTreeContext) -> BooleanTreeMatch:
        if context.owner_hierarchy != context.expected_owner_hierarchy:
            raise BooleanTreeError("source owner does not match proposal owner hierarchy")
        if context.protected_neighbor_ids:
            raise BooleanTreeError("boolean tree has a protected neighbor")
        if len(context.clock_domain_ids) != 1:
            raise BooleanTreeError("boolean tree must belong to exactly one clock domain")
        if not context.equal_widths_verified:
            raise BooleanTreeError("operand width equality is not verified")
        if not context.unsigned_verified:
            raise BooleanTreeError("unsigned operand interpretation is not verified")
        if not context.four_state_associative:
            raise BooleanTreeError("four-state associativity is not verified")
        if "&&" in context.source_text or "||" in context.source_text:
            raise BooleanTreeError("only bitwise operators are supported")
        if re.search(r"'[sS]?[bBoOdDhH][^\s;]*[xXzZ?]", context.source_text):
            raise BooleanTreeError("X/Z literal makes the tree ambiguous")
        matched = _ASSIGNMENT.fullmatch(context.source_text)
        if matched is None or any(char in matched.group("expression") for char in "()"):
            raise BooleanTreeError("expected a flat associative chain assignment")
        expression = matched.group("expression")
        present = tuple(operator for operator in ("&", "|", "^") if operator in expression)
        if len(present) != 1:
            raise BooleanTreeError("expected a single associative operator")
        operator = present[0]
        operands = tuple(item.strip() for item in expression.split(operator))
        if len(operands) < 4 or any(_OPERAND.fullmatch(item) is None for item in operands):
            raise BooleanTreeError("expected a flat associative chain with safe operands")
        payload = {
            "context": context.model_dump(mode="json"),
            "assignment_target": matched.group("target"),
            "operator": operator,
            "operands": operands,
        }
        return BooleanTreeMatch(
            context=context,
            assignment_target=matched.group("target"),
            operator=operator,
            operands=operands,
            match_hash=canonical_sha256(payload),
        )

    def preflight(
        self, match: BooleanTreeMatch, parameters: BooleanTreeParameters
    ) -> None:
        if match.operator != parameters.operator:
            raise BooleanTreeError("requested operator does not match the source tree")
        if len(match.operands) > parameters.max_operands:
            raise BooleanTreeError("operand count exceeds configured bound")

    def rewrite(
        self, match: BooleanTreeMatch, parameters: BooleanTreeParameters
    ) -> SourceEdit:
        self.preflight(match, parameters)
        replacement = (
            f"{match.assignment_target} = {_balanced(match.operator, match.operands)};"
        )
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
        self, match: BooleanTreeMatch, parameters: BooleanTreeParameters
    ) -> str:
        identity = canonical_sha256(
            {
                "implementation_version": self.metadata.implementation_version,
                "match_hash": match.match_hash,
                "parameters": parameters.model_dump(mode="json"),
            }
        )
        return "boolean_tree:v1:" + identity.removeprefix("sha256:")

    @staticmethod
    def evaluate_serial(operator: str, values: tuple[str, ...]) -> str:
        result = values[0]
        for value in values[1:]:
            result = _four_state_binary(operator, result, value)
        return result

    @classmethod
    def evaluate_balanced(cls, operator: str, values: tuple[str, ...]) -> str:
        if len(values) == 1:
            return "x" if values[0] == "z" else values[0]
        midpoint = len(values) // 2
        return _four_state_binary(
            operator,
            cls.evaluate_balanced(operator, values[:midpoint]),
            cls.evaluate_balanced(operator, values[midpoint:]),
        )


__all__ = [
    "BalanceBooleanTree",
    "BooleanTreeContext",
    "BooleanTreeError",
    "BooleanTreeMatch",
    "BooleanTreeParameters",
]
