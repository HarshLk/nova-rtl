"""Deterministic, four-state-safe priority-mux restructuring capability."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import EntityId, NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.transforms.registry import TransformCapabilityMetadata
from nova_rtl.transforms.syntax import SourceEdit


class PriorityMuxError(ValueError):
    """The targeted source is not a safely supported priority mux."""


class PriorityMuxParameters(StrictContract):
    """Bound the recognized priority chain; semantic behavior is never configurable."""

    max_branches: int = Field(default=8, strict=True, ge=2, le=16)


class PriorityMuxContext(StrictContract):
    """Proposal-owned source slice and structural safety facts."""

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

    @field_validator("clock_domain_ids", "protected_neighbor_ids")
    @classmethod
    def identities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context identities must be unique")
        return value

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("priority mux source range must be nonempty")
        return self


class PriorityBranch(StrictContract):
    """One condition and blocking-assignment value from the serial chain."""

    condition: NonEmptyString
    value: NonEmptyString


class PriorityMuxMatch(StrictContract):
    """Fully parsed supported priority chain."""

    context: PriorityMuxContext
    assignment_target: NonEmptyString
    branches: tuple[PriorityBranch, ...] = Field(min_length=2, max_length=16)
    default_value: NonEmptyString
    match_hash: str

    @model_validator(mode="after")
    def hash_is_valid(self) -> Self:
        expected = canonical_sha256(self, exclude=frozenset({"match_hash"}))
        if self.match_hash != expected:
            raise ValueError("priority mux match hash is not canonical")
        return self


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    start: int
    end: int
    kind: str


_DOUBLE_OPERATORS = (
    "===",
    "!==",
    "<<<",
    ">>>",
    "++",
    "--",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "<<",
    ">>",
    "->",
)
_SIDE_EFFECT_OPERATORS = frozenset(
    {"++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "->"}
)
_ALLOWED_SYSTEM_CASTS = frozenset({"$signed", "$unsigned"})


def _lex(source: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index) or source.startswith("/*", index):
            raise PriorityMuxError("comments inside targeted priority chain are unsupported")
        start = index
        if character == '"':
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise PriorityMuxError("unterminated string literal")
            tokens.append(_Token(source[start:index], start, index, "STRING"))
            continue
        if character == "\\":
            index += 1
            while index < len(source) and not source[index].isspace():
                index += 1
            tokens.append(_Token(source[start:index], start, index, "IDENTIFIER"))
            continue
        if character == "$" or character.isalpha() or character == "_":
            index += 1
            while index < len(source) and (
                source[index].isalnum() or source[index] in {"_", "$"}
            ):
                index += 1
            tokens.append(_Token(source[start:index], start, index, "IDENTIFIER"))
            continue
        if character.isdigit() or character == "'":
            index += 1
            while index < len(source) and (
                source[index].isalnum() or source[index] in {"_", "'", "?"}
            ):
                index += 1
            tokens.append(_Token(source[start:index], start, index, "NUMBER"))
            continue
        operator = next(
            (item for item in _DOUBLE_OPERATORS if source.startswith(item, index)), None
        )
        if operator is not None:
            index += len(operator)
            tokens.append(_Token(operator, start, index, "OPERATOR"))
            continue
        if character in "()[]{};=?:,.~!&|^+-*/%<>":
            index += 1
            tokens.append(_Token(character, start, index, "SYMBOL"))
            continue
        raise PriorityMuxError(f"unsupported SystemVerilog token: {character!r}")
    return tuple(tokens)


def _expression_text(source: str, tokens: tuple[_Token, ...]) -> str:
    if not tokens:
        raise PriorityMuxError("priority mux expression cannot be empty")
    if any(token.text in _SIDE_EFFECT_OPERATORS for token in tokens):
        raise PriorityMuxError("priority mux expressions must be side-effect-free")
    for index, token in enumerate(tokens[:-1]):
        if (
            token.kind == "IDENTIFIER"
            and tokens[index + 1].text == "("
            and token.text not in _ALLOWED_SYSTEM_CASTS
        ):
            raise PriorityMuxError("priority mux expressions must be side-effect-free")
    for token in tokens:
        if token.kind == "NUMBER" and any(item in token.text.lower() for item in ("x", "z", "?")):
            raise PriorityMuxError("priority mux source has ambiguous X/Z literals")
    return source[tokens[0].start : tokens[-1].end].strip()


class _PriorityParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = _lex(source)
        self.index = 0

    def _peek(self) -> str | None:
        return self.tokens[self.index].text if self.index < len(self.tokens) else None

    def _expect(self, text: str) -> None:
        if self._peek() != text:
            raise PriorityMuxError(f"expected {text!r} in priority chain")
        self.index += 1

    def _parenthesized_expression(self) -> tuple[_Token, ...]:
        self._expect("(")
        start = self.index
        depth = 1
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
                if depth == 0:
                    expression = self.tokens[start : self.index]
                    self.index += 1
                    return expression
            self.index += 1
        raise PriorityMuxError("unterminated priority condition")

    def _assignment(self) -> tuple[str, str]:
        start = self.index
        depths = {"(": 0, "[": 0, "{": 0}
        closing = {")": "(", "]": "[", "}": "{"}
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.text in depths:
                depths[token.text] += 1
            elif token.text in closing:
                opener = closing[token.text]
                depths[opener] -= 1
                if depths[opener] < 0:
                    raise PriorityMuxError("unbalanced delimiter in priority assignment")
            elif token.text == ";" and not any(depths.values()):
                statement = self.tokens[start : self.index]
                self.index += 1
                break
            self.index += 1
        else:
            raise PriorityMuxError("priority branches require simple blocking assignments")
        if not statement or any(depths.values()):
            raise PriorityMuxError("priority branches require simple blocking assignments")
        equals = [index for index, token in enumerate(statement) if token.text == "="]
        if len(equals) != 1:
            raise PriorityMuxError("priority branches require simple blocking assignments")
        equal = equals[0]
        target_tokens = statement[:equal]
        value_tokens = statement[equal + 1 :]
        if not target_tokens or not value_tokens:
            raise PriorityMuxError("priority branches require simple blocking assignments")
        if any(token.text in _SIDE_EFFECT_OPERATORS for token in statement):
            raise PriorityMuxError("priority branches require simple blocking assignments")
        allowed_target = {"IDENTIFIER", "NUMBER"}
        if any(
            token.kind not in allowed_target and token.text not in {"[", "]", ":", "."}
            for token in target_tokens
        ):
            raise PriorityMuxError("priority branches require a simple assignment target")
        target = "".join(token.text for token in target_tokens)
        return target, _expression_text(self.source, value_tokens)

    def parse(self) -> tuple[str, tuple[PriorityBranch, ...], str]:
        branches: list[PriorityBranch] = []
        assignment_target: str | None = None
        while True:
            self._expect("if")
            condition_tokens = self._parenthesized_expression()
            condition = _expression_text(self.source, condition_tokens)
            target, value = self._assignment()
            if assignment_target is None:
                assignment_target = target
            elif assignment_target != target:
                raise PriorityMuxError("priority branches must use the same assignment target")
            branches.append(PriorityBranch(condition=condition, value=value))
            if self._peek() != "else":
                raise PriorityMuxError("priority chain requires a complete default assignment")
            self.index += 1
            if self._peek() == "if":
                continue
            default_target, default_value = self._assignment()
            if assignment_target != default_target:
                raise PriorityMuxError("priority branches must use the same assignment target")
            if self.index != len(self.tokens):
                raise PriorityMuxError("unsupported statements follow priority chain")
            if len(branches) < 2:
                raise PriorityMuxError("priority chain requires at least two conditional branches")
            assert assignment_target is not None
            return assignment_target, tuple(branches), default_value


class RestructurePriorityMux:
    """Registered bounded serial-priority to parallel-predecode rewrite."""

    parameter_model: ClassVar[type[PriorityMuxParameters]] = PriorityMuxParameters
    metadata: ClassVar[TransformCapabilityMetadata] = TransformCapabilityMetadata(
        operation="RESTRUCTURE_PRIORITY_MUX",
        family="LOGIC_RESTRUCTURING",
        correctness_contract="STRICT_SEQ_EQUIV",
        implementation_version="transform:v1:priority_mux",
        allowed_ast_node_kinds=("ConditionalStatement",),
        prohibited_contexts=(
            "AMBIGUOUS_X_SEMANTICS",
            "CDC_ADJACENT",
            "CLOCK_LOGIC",
            "MULTI_CLOCK_CONE",
            "RESET_LOGIC",
            "SIDE_EFFECTING_EXPRESSION",
        ),
        expected_structural_effects=("BALANCED_PREDECODE", "REDUCE_PRIORITY_DEPTH"),
        conflicts=(),
    )

    def match(self, context: PriorityMuxContext) -> PriorityMuxMatch:
        if context.owner_hierarchy != context.expected_owner_hierarchy:
            raise PriorityMuxError("source owner does not match proposal owner hierarchy")
        if context.protected_neighbor_ids:
            raise PriorityMuxError("priority mux has a protected neighbor")
        if len(context.clock_domain_ids) != 1:
            raise PriorityMuxError("priority mux must belong to exactly one clock domain")
        target, branches, default = _PriorityParser(context.source_text).parse()
        payload = {
            "context": context.model_dump(mode="json"),
            "assignment_target": target,
            "branches": tuple(branch.model_dump(mode="json") for branch in branches),
            "default_value": default,
        }
        return PriorityMuxMatch(
            context=context,
            assignment_target=target,
            branches=branches,
            default_value=default,
            match_hash=canonical_sha256(payload),
        )

    def rewrite(
        self, match: PriorityMuxMatch, parameters: PriorityMuxParameters
    ) -> SourceEdit:
        count = len(match.branches)
        if count > parameters.max_branches:
            raise PriorityMuxError("priority branch count exceeds configured bound")
        suffix = match.match_hash.removeprefix("sha256:")[:10]
        block_name = f"nova_priority_mux_{suffix}"
        lines = [
            f"begin : {block_name}",
            f"  logic [{count - 1}:0] nova_priority_cond;",
            f"  logic [{count - 1}:0] nova_priority_match;",
        ]
        for index, branch in enumerate(match.branches):
            lines.append(
                f"  nova_priority_cond[{index}] = (({branch.condition}) === 1'b1);"
            )
        lines.append("  nova_priority_match[0] = nova_priority_cond[0];")
        for index in range(1, count):
            lines.append(
                f"  nova_priority_match[{index}] = nova_priority_cond[{index}] "
                f"& ~(|nova_priority_cond[{index - 1}:0]);"
            )
        lines.append("  case (1'b1)")
        for index, branch in enumerate(match.branches):
            lines.append(
                f"    nova_priority_match[{index}]: "
                f"{match.assignment_target} = {branch.value};"
            )
        lines.extend(
            (
                f"    default: {match.assignment_target} = {match.default_value};",
                "  endcase",
                "end",
            )
        )
        source_hash = "sha256:" + sha256(
            match.context.source_text.encode("utf-8")
        ).hexdigest()
        return SourceEdit(
            relative_path=match.context.relative_path,
            start_line=match.context.start_line,
            start_column=match.context.start_column,
            end_line=match.context.end_line,
            end_column=match.context.end_column,
            expected_text_hash=source_hash,
            replacement="\n".join(lines),
            ast_node_kind="ConditionalStatement",
        )

    def preflight(
        self, match: PriorityMuxMatch, parameters: PriorityMuxParameters
    ) -> None:
        """Reject unsupported branch counts before source materialization."""

        if len(match.branches) > parameters.max_branches:
            raise PriorityMuxError("priority branch count exceeds configured bound")

    def fingerprint(
        self, match: PriorityMuxMatch, parameters: PriorityMuxParameters
    ) -> str:
        identity = canonical_sha256(
            {
                "implementation_version": self.metadata.implementation_version,
                "match_hash": match.match_hash,
                "parameters": parameters.model_dump(mode="json"),
            }
        )
        return "priority_mux:v1:" + identity.removeprefix("sha256:")

    @staticmethod
    def selected_branch(match: PriorityMuxMatch, conditions: tuple[str, ...]) -> int:
        """Reference four-state selector used by exhaustive semantic tests."""

        if len(conditions) != len(match.branches) or any(
            condition not in {"0", "1", "x", "z"} for condition in conditions
        ):
            raise PriorityMuxError("condition vector does not match priority branches")
        return next(
            (index for index, condition in enumerate(conditions) if condition == "1"),
            len(conditions),
        )


__all__ = [
    "PriorityBranch",
    "PriorityMuxContext",
    "PriorityMuxError",
    "PriorityMuxMatch",
    "PriorityMuxParameters",
    "RestructurePriorityMux",
]
