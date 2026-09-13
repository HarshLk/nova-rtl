"""Strict restructuring of complete combinational FSM decode chains."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import EntityId, NonEmptyString, StrictContract, canonical_sha256
from nova_rtl.transforms.registry import TransformCapabilityMetadata
from nova_rtl.transforms.syntax import SourceEdit


class FsmDecodeError(ValueError):
    """The targeted FSM decode is incomplete or semantically unsafe."""


class FsmDecodeParameters(StrictContract):
    max_states: int = Field(default=16, strict=True, ge=2, le=64)


class FsmDecodeContext(StrictContract):
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
    state_signal: NonEmptyString
    state_width_verified: bool
    state_register_protected: bool
    reset_semantics_verified: bool
    enable_semantics_verified: bool
    complete_output_assignments: bool

    @field_validator("clock_domain_ids", "protected_neighbor_ids")
    @classmethod
    def identities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context identities must be unique")
        return value

    @model_validator(mode="after")
    def source_range_is_nonempty(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("FSM decode source range must be nonempty")
        return self


class FsmDecodeBranch(StrictContract):
    state_label: NonEmptyString
    body: NonEmptyString


class FsmDecodeMatch(StrictContract):
    context: FsmDecodeContext
    state_signal: NonEmptyString
    branches: tuple[FsmDecodeBranch, ...] = Field(min_length=2, max_length=64)
    default_body: NonEmptyString
    match_hash: str

    @model_validator(mode="after")
    def hash_is_valid(self) -> Self:
        if len({branch.state_label for branch in self.branches}) != len(self.branches):
            raise ValueError("FSM decode state labels must be unique")
        if self.match_hash != canonical_sha256(
            self, exclude=frozenset({"match_hash"})
        ):
            raise ValueError("FSM decode match hash is not canonical")
        return self


_LABEL = r"(?:[A-Za-z_][A-Za-z0-9_$]*|\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]+)"
_BRANCH = re.compile(
    rf"\s*(?:else\s+)?if\s*\(\s*(?P<signal>[A-Za-z_][A-Za-z0-9_$]*)"
    rf"\s*==\s*(?P<label>{_LABEL})\s*\)\s*begin\s*"
    r"(?P<body>.*?)\s*end",
    flags=re.DOTALL,
)
_DEFAULT = re.compile(
    r"\s*else\s+begin\s*(?P<body>.*?)\s*end\s*$",
    flags=re.DOTALL,
)


class RestructureFsmDecode:
    """Convert a verified complete if-chain into a direct state case decode."""

    parameter_model: ClassVar[type[FsmDecodeParameters]] = FsmDecodeParameters
    metadata: ClassVar[TransformCapabilityMetadata] = TransformCapabilityMetadata(
        operation="FSM_DECODE_RESTRUCTURE",
        family="LOGIC_RESTRUCTURING",
        correctness_contract="STRICT_SEQ_EQUIV",
        implementation_version="transform:v1:fsm_decode",
        allowed_ast_node_kinds=("ConditionalStatement",),
        prohibited_contexts=(
            "CLOCK_LOGIC",
            "INCOMPLETE_ASSIGNMENT",
            "MULTI_CLOCK_CONE",
            "PROTECTED_NODE",
            "RESET_LOGIC",
            "STATE_REGISTER_EDIT",
            "UNVERIFIED_ENABLE",
            "X_SENSITIVE_EQUALITY",
        ),
        expected_structural_effects=("REDUCE_FSM_DECODE_LEVELS",),
        conflicts=(),
    )

    def match(self, context: FsmDecodeContext) -> FsmDecodeMatch:
        if context.owner_hierarchy != context.expected_owner_hierarchy:
            raise FsmDecodeError("source owner does not match proposal owner hierarchy")
        if context.protected_neighbor_ids:
            raise FsmDecodeError("FSM decode has a protected neighbor")
        if len(context.clock_domain_ids) != 1:
            raise FsmDecodeError("FSM decode must belong to exactly one clock domain")
        if not context.state_width_verified:
            raise FsmDecodeError("state width is not verified")
        if not context.state_register_protected:
            raise FsmDecodeError("state register must remain protected")
        if not context.reset_semantics_verified:
            raise FsmDecodeError("reset semantics are not verified")
        if not context.enable_semantics_verified:
            raise FsmDecodeError("enable semantics are not verified")
        if not context.complete_output_assignments:
            raise FsmDecodeError("output assignment coverage is incomplete")
        if "===" in context.source_text or "!==" in context.source_text:
            raise FsmDecodeError("case equality is unsupported for FSM restructuring")
        if re.search(
            r"==\s*\d+'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]*[xXzZ?]",
            context.source_text,
        ):
            raise FsmDecodeError("X/Z state label is unsupported")

        branches: list[FsmDecodeBranch] = []
        position = 0
        while True:
            matched = _BRANCH.match(context.source_text, position)
            if matched is None:
                break
            signal = matched.group("signal")
            label = matched.group("label")
            body = matched.group("body").strip()
            if signal != context.state_signal:
                raise FsmDecodeError("FSM branches do not use the declared state signal")
            if not body:
                raise FsmDecodeError("FSM branch body cannot be empty")
            branches.append(FsmDecodeBranch(state_label=label, body=body))
            position = matched.end()
        final = _DEFAULT.fullmatch(context.source_text[position:])
        if len(branches) < 2 or final is None or not final.group("body").strip():
            raise FsmDecodeError("FSM if/else chain requires a complete final else")
        payload = {
            "context": context.model_dump(mode="json"),
            "state_signal": context.state_signal,
            "branches": tuple(branch.model_dump(mode="json") for branch in branches),
            "default_body": final.group("body").strip(),
        }
        return FsmDecodeMatch(
            context=context,
            state_signal=context.state_signal,
            branches=tuple(branches),
            default_body=final.group("body").strip(),
            match_hash=canonical_sha256(payload),
        )

    def preflight(
        self, match: FsmDecodeMatch, parameters: FsmDecodeParameters
    ) -> None:
        if len(match.branches) > parameters.max_states:
            raise FsmDecodeError("FSM state count exceeds configured bound")

    def rewrite(
        self, match: FsmDecodeMatch, parameters: FsmDecodeParameters
    ) -> SourceEdit:
        self.preflight(match, parameters)
        lines = [f"case ({match.state_signal})"]
        lines.extend(
            f"  {branch.state_label}: begin {branch.body} end"
            for branch in match.branches
        )
        lines.extend((f"  default: begin {match.default_body} end", "endcase"))
        return SourceEdit(
            relative_path=match.context.relative_path,
            start_line=match.context.start_line,
            start_column=match.context.start_column,
            end_line=match.context.end_line,
            end_column=match.context.end_column,
            expected_text_hash="sha256:"
            + sha256(match.context.source_text.encode("utf-8")).hexdigest(),
            replacement="\n".join(lines),
            ast_node_kind="ConditionalStatement",
        )

    def fingerprint(
        self, match: FsmDecodeMatch, parameters: FsmDecodeParameters
    ) -> str:
        identity = canonical_sha256(
            {
                "implementation_version": self.metadata.implementation_version,
                "match_hash": match.match_hash,
                "parameters": parameters.model_dump(mode="json"),
            }
        )
        return "fsm_decode:v1:" + identity.removeprefix("sha256:")

    @staticmethod
    def select_if_branch(state: str, labels: dict[str, str]) -> str:
        if any(character.lower() in {"x", "z"} for character in state):
            return "DEFAULT"
        return next((name for name, value in labels.items() if state == value), "DEFAULT")

    @staticmethod
    def select_case_branch(state: str, labels: dict[str, str]) -> str:
        return next((name for name, value in labels.items() if state == value), "DEFAULT")


__all__ = [
    "FsmDecodeBranch",
    "FsmDecodeContext",
    "FsmDecodeError",
    "FsmDecodeMatch",
    "FsmDecodeParameters",
    "RestructureFsmDecode",
]
