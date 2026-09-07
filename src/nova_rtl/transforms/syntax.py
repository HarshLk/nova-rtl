"""Pinned-Slang syntax parsing and exact source-edit authorization."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.evidence.models import SourceSpanRecord


class SyntaxBackendError(ValueError):
    """Syntax evidence or a requested source edit fails closed."""


_SLANG_KIND_VOCABULARY = {
    "Conditional": "ConditionalStatement",
}


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("source path must be a normalized relative path")
    return value


class SyntaxNodeRange(StrictContract):
    """Exact source range emitted by the pinned Slang AST."""

    kind: NonEmptyString
    relative_path: str
    start_line: int = Field(strict=True, ge=1)
    start_column: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    end_column: int = Field(strict=True, ge=1)

    @field_validator("relative_path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_path(value)

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("syntax node end cannot precede its start")
        return self


class ParsedSlangAst(StrictContract):
    """Address-normalized Slang syntax identity and source-located nodes."""

    schema_version: Literal[1] = 1
    nodes: tuple[SyntaxNodeRange, ...]
    ast_hash: HashRef

    @model_validator(mode="after")
    def nodes_are_canonical(self) -> Self:
        keys = tuple(
            (
                node.relative_path,
                node.start_line,
                node.start_column,
                node.end_line,
                node.end_column,
                node.kind,
            )
            for node in self.nodes
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("syntax nodes must be unique and canonically ordered")
        return self


class SourceEdit(StrictContract):
    """One exact half-open replacement proposed by a registered transform."""

    relative_path: str
    start_line: int = Field(strict=True, ge=1)
    start_column: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    end_column: int = Field(strict=True, ge=1)
    expected_text_hash: HashRef
    replacement: str
    ast_node_kind: NonEmptyString

    @field_validator("relative_path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_path(value)

    @model_validator(mode="after")
    def range_is_nonempty(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("source edit must have a nonempty ordered range")
        return self


class AppliedSourceEdit(StrictContract):
    """Deterministic result of an authorized exact-span replacement."""

    schema_version: Literal[1] = 1
    relative_path: str
    changed_span_ids: tuple[EntityId, ...]
    before_hash: HashRef
    after_hash: HashRef
    replacement_hash: HashRef
    ast_hash: HashRef
    ast_node_kind: NonEmptyString
    source_range: tuple[int, int, int, int]
    edit_hash: HashRef

    @field_validator("relative_path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_path(value)


def _without_process_addresses(value: object) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if key == "addr":
                continue
            normalized[str(key)] = _without_process_addresses(item)
        return normalized
    if isinstance(value, list):
        return [_without_process_addresses(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"^[0-9]+(?=\s+[A-Za-z_$])", "<address>", value)
    return value


def parse_slang_ast(payload: object) -> ParsedSlangAst:
    """Normalize Slang JSON and extract every exact single-file AST source range."""

    if not isinstance(payload, Mapping):
        raise SyntaxBackendError("Slang AST must be a JSON object")
    nodes: set[tuple[str, int, int, int, int, str]] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            range_keys = {
                "source_file_start",
                "source_file_end",
                "source_line_start",
                "source_line_end",
                "source_column_start",
                "source_column_end",
            }
            present = range_keys.intersection(value)
            if present:
                if present != range_keys:
                    raise SyntaxBackendError("Slang AST contains an incomplete source range")
                start_path = value["source_file_start"]
                end_path = value["source_file_end"]
                if start_path != end_path:
                    raise SyntaxBackendError("Slang AST contains a cross-file source range")
                kind = value.get("kind")
                coordinates = (
                    value["source_line_start"],
                    value["source_column_start"],
                    value["source_line_end"],
                    value["source_column_end"],
                )
                if not isinstance(start_path, str) or not isinstance(kind, str):
                    raise SyntaxBackendError("Slang AST source range lacks path or node kind")
                if any(not isinstance(item, int) or isinstance(item, bool) for item in coordinates):
                    raise SyntaxBackendError("Slang AST source coordinates must be integers")
                try:
                    normalized = _normalized_path(start_path)
                    node = SyntaxNodeRange(
                        kind=_SLANG_KIND_VOCABULARY.get(kind, kind),
                        relative_path=normalized,
                        start_line=coordinates[0],
                        start_column=coordinates[1],
                        end_line=coordinates[2],
                        end_column=coordinates[3],
                    )
                except ValueError as error:
                    raise SyntaxBackendError(str(error)) from error
                nodes.add(
                    (
                        node.relative_path,
                        node.start_line,
                        node.start_column,
                        node.end_line,
                        node.end_column,
                        node.kind,
                    )
                )
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    normalized_payload = _without_process_addresses(payload)
    ordered_nodes = tuple(
        SyntaxNodeRange(
            relative_path=path,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            kind=kind,
        )
        for path, start_line, start_column, end_line, end_column, kind in sorted(nodes)
    )
    return ParsedSlangAst(nodes=ordered_nodes, ast_hash=canonical_sha256(normalized_payload))


def _position_to_offset(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    if line > len(lines):
        raise SyntaxBackendError("source coordinate line exceeds file length")
    line_text = lines[line - 1]
    content = line_text.removesuffix("\n").removesuffix("\r")
    if column > len(content) + 1:
        raise SyntaxBackendError("source coordinate column exceeds line length")
    return sum(len(item) for item in lines[: line - 1]) + column - 1


def _line_span_text(source: str, span: SourceSpanRecord) -> str:
    lines = source.splitlines()
    if span.end_line > len(lines):
        raise SyntaxBackendError("authorized source span exceeds file length")
    return "\n".join(lines[span.start_line - 1 : span.end_line])


def _ranges_overlap(first: SourceSpanRecord, edit: SourceEdit) -> bool:
    if first.relative_path != edit.relative_path:
        return False
    return (first.start_line, first.start_column) < (edit.end_line, edit.end_column) and (
        edit.start_line,
        edit.start_column,
    ) < (first.end_line, first.end_column)


def apply_authorized_edit(
    *,
    repository_root: Path,
    parsed_ast: ParsedSlangAst,
    authorized_span: SourceSpanRecord,
    protected_spans: Sequence[SourceSpanRecord],
    edit: SourceEdit,
    allowed_ast_node_kinds: Sequence[str],
) -> AppliedSourceEdit:
    """Apply one exact AST-node edit after all source ownership checks pass."""

    if edit.relative_path != authorized_span.relative_path:
        raise SyntaxBackendError("source edit targets a file outside authorized span")
    if authorized_span.protected:
        raise SyntaxBackendError("authorized source span is protected")
    if (edit.start_line, edit.start_column) < (
        authorized_span.start_line,
        authorized_span.start_column,
    ) or (edit.end_line, edit.end_column) > (
        authorized_span.end_line,
        authorized_span.end_column,
    ):
        raise SyntaxBackendError("source edit is outside authorized span")
    if any(span.protected and _ranges_overlap(span, edit) for span in protected_spans):
        raise SyntaxBackendError("source edit overlaps a protected span")
    if edit.ast_node_kind not in set(allowed_ast_node_kinds):
        raise SyntaxBackendError("source edit AST node kind is not registered")
    exact_node = SyntaxNodeRange(
        kind=edit.ast_node_kind,
        relative_path=edit.relative_path,
        start_line=edit.start_line,
        start_column=edit.start_column,
        end_line=edit.end_line,
        end_column=edit.end_column,
    )
    if exact_node not in parsed_ast.nodes:
        raise SyntaxBackendError("source edit does not match an exact Slang AST node range")

    root = repository_root.resolve()
    source_path = (root / edit.relative_path).resolve()
    if root not in source_path.parents:
        raise SyntaxBackendError("source edit path escapes repository root")
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SyntaxBackendError(f"cannot read authorized UTF-8 source: {error}") from error
    if _hash_text(_line_span_text(source, authorized_span)) != authorized_span.source_text_hash:
        raise SyntaxBackendError("authorized source span hash no longer matches")
    start_offset = _position_to_offset(source, edit.start_line, edit.start_column)
    end_offset = _position_to_offset(source, edit.end_line, edit.end_column)
    original = source[start_offset:end_offset]
    if _hash_text(original) != edit.expected_text_hash:
        raise SyntaxBackendError("source edit expected text hash no longer matches")
    rewritten = source[:start_offset] + edit.replacement + source[end_offset:]
    before_hash = _hash_text(source)
    after_hash = _hash_text(rewritten)
    replacement_hash = _hash_text(edit.replacement)
    payload = {
        "schema_version": 1,
        "relative_path": edit.relative_path,
        "changed_span_ids": (authorized_span.source_span_id,),
        "before_hash": before_hash,
        "after_hash": after_hash,
        "replacement_hash": replacement_hash,
        "ast_hash": parsed_ast.ast_hash,
        "ast_node_kind": edit.ast_node_kind,
        "source_range": (
            edit.start_line,
            edit.start_column,
            edit.end_line,
            edit.end_column,
        ),
    }
    source_path.write_text(rewritten, encoding="utf-8")
    return AppliedSourceEdit(**payload, edit_hash=canonical_sha256(payload))


def _hash_text(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


class SlangSyntaxBackend:
    """Run a pinned Slang executable by argv and normalize its source-located AST."""

    def __init__(self, executable: Path, expected_sha256: HashRef) -> None:
        self._executable = executable.resolve()
        self._expected_sha256 = expected_sha256

    def parse_files(
        self,
        *,
        repository_root: Path,
        source_paths: Sequence[str],
        top: str,
        include_directories: Sequence[str] = (),
        timeout_seconds: int = 60,
    ) -> ParsedSlangAst:
        root = repository_root.resolve()
        try:
            actual_hash = "sha256:" + sha256(self._executable.read_bytes()).hexdigest()
        except OSError as error:
            raise SyntaxBackendError(f"cannot read pinned Slang executable: {error}") from error
        if actual_hash != self._expected_sha256:
            raise SyntaxBackendError("pinned Slang executable hash does not match")
        normalized_paths = tuple(_normalized_path(path) for path in source_paths)
        normalized_includes = tuple(
            _normalized_path(path) for path in include_directories
        )
        if not normalized_paths or len(normalized_paths) != len(set(normalized_paths)):
            raise SyntaxBackendError("source paths must be nonempty and unique")
        for relative_path in normalized_paths:
            path = (root / relative_path).resolve()
            if root not in path.parents or not path.is_file():
                raise SyntaxBackendError(
                    f"Slang source is missing or escapes root: {relative_path}"
                )

        with tempfile.TemporaryDirectory(prefix="nova-slang-") as temporary:
            ast_path = Path(temporary) / "ast.json"
            argv = (
                str(self._executable),
                "--std",
                "1800-2017",
                "--single-unit",
                "--top",
                top,
                *(item for path in normalized_includes for item in ("-I", path)),
                "--ast-json",
                str(ast_path),
                "--ast-json-source-info",
                *normalized_paths,
            )
            environment = {
                "HOME": temporary,
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TZ": "UTC",
            }
            try:
                completed = subprocess.run(
                    argv,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise SyntaxBackendError(f"Slang execution failed: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise SyntaxBackendError(f"Slang rejected candidate syntax: {detail}")
            try:
                ast_payload: Any = json.loads(ast_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SyntaxBackendError(
                    f"Slang AST output is missing or malformed: {error}"
                ) from error
        return parse_slang_ast(ast_payload)


__all__ = [
    "AppliedSourceEdit",
    "ParsedSlangAst",
    "SlangSyntaxBackend",
    "SourceEdit",
    "SyntaxBackendError",
    "SyntaxNodeRange",
    "apply_authorized_edit",
    "parse_slang_ast",
]
