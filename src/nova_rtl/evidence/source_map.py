"""Deterministic Yosys structure-to-RTL source mapping."""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from nova_rtl.contracts.analysis import CriticalPathRecord
from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    NonNegativeInt,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.evidence.models import ProtectionKind, SourceSpanRecord

_SOURCE_MARKER = re.compile(r"^// NOVA_SOURCE (?P<path>\S+)$")
_BUNDLE_SPAN = re.compile(
    r"inputs/input_rtl_bundle:(?P<start_line>\d+)\.(?P<start_column>\d+)-"
    r"(?P<end_line>\d+)\.(?P<end_column>\d+)"
)
MappedObjectKind = Literal["CELL", "PIN", "PORT"]


class SourceMapError(ValueError):
    """Yosys structural evidence cannot be mapped without guessing."""


def _canonical_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


class MappedObjectRecord(StrictContract):
    """One emitted structural object and its direct RTL provenance."""

    object_id: EntityId
    semantic_name: NonEmptyString
    kind: MappedObjectKind
    owner_hierarchy: NonEmptyString
    module_type: NonEmptyString
    cell_type: NonEmptyString | None
    port_direction: Literal["input", "output", "inout"] | None
    source_span_ids: tuple[EntityId, ...]
    fanout: NonNegativeInt
    protected: bool
    protection_kinds: tuple[ProtectionKind, ...]

    @field_validator("source_span_ids", "protection_kinds")
    @classmethod
    def members_are_canonical(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _canonical_unique(value, getattr(info, "field_name", "members"))

    @model_validator(mode="after")
    def protection_is_coherent(self) -> Self:
        if self.protected != bool(self.protection_kinds):
            raise ValueError("protected must match protection_kinds")
        if self.kind == "PIN" and self.port_direction is None:
            raise ValueError("mapped pins require port_direction")
        return self


class SourceMapSnapshot(StrictContract):
    """Canonical source spans and structural names for one synthesized candidate."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    rtl_snapshot_hash: HashRef
    synthesis_structure_hash: HashRef
    mapped_objects: tuple[MappedObjectRecord, ...] = Field(min_length=1)
    source_spans: tuple[SourceSpanRecord, ...] = Field(min_length=1)
    source_map_hash: HashRef

    @model_validator(mode="after")
    def contents_are_canonical_and_self_hashed(self) -> Self:
        object_names = tuple(item.semantic_name for item in self.mapped_objects)
        span_ids = tuple(item.source_span_id for item in self.source_spans)
        _canonical_unique(object_names, "mapped object names")
        _canonical_unique(span_ids, "source span IDs")
        known_spans = set(span_ids)
        if any(set(item.source_span_ids) - known_spans for item in self.mapped_objects):
            raise ValueError("mapped object references an unknown source span")
        expected = canonical_sha256(self, exclude=frozenset({"source_map_hash"}))
        if self.source_map_hash != expected:
            raise ValueError("source_map_hash does not match canonical source map")
        return self


def _identifier(text: str) -> tuple[str, str]:
    remaining = text.lstrip()
    if remaining.startswith("\\"):
        match = re.match(r"\\(?P<name>\S+)(?P<rest>.*)", remaining)
    else:
        match = re.match(r"(?P<name>[^\s(]+)(?P<rest>.*)", remaining)
    if match is None:
        raise SourceMapError(f"cannot parse Verilog identifier from: {text!r}")
    return match.group("name"), match.group("rest")


def _emitted_cell_names(mapped_netlist: str) -> dict[str, tuple[tuple[str, str], ...]]:
    modules: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    for line in mapped_netlist.splitlines():
        if line.startswith("module "):
            current, _ = _identifier(line.removeprefix("module "))
            if current in modules:
                raise SourceMapError(f"duplicate mapped netlist module: {current}")
            modules[current] = []
        elif line.startswith("endmodule"):
            current = None
        elif current is not None and line.startswith("  ") and line.rstrip().endswith("("):
            cell_type, rest = _identifier(line)
            cell_name, rest = _identifier(rest)
            if rest.strip() == "(":
                modules[current].append((cell_type, cell_name))
    if not modules:
        raise SourceMapError("mapped netlist contains no modules")
    return {name: tuple(cells) for name, cells in modules.items()}


class _BundleIndex:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        markers = [
            (index, match.group("path"))
            for index, line in enumerate(self.lines, start=1)
            if (match := _SOURCE_MARKER.fullmatch(line)) is not None
        ]
        if not markers:
            raise SourceMapError("RTL bundle contains no NOVA_SOURCE markers")
        self.segments = tuple(
            (line, markers[index + 1][0] if index + 1 < len(markers) else len(self.lines) + 1, path)
            for index, (line, path) in enumerate(markers)
        )

    def resolve(self, start_line: int, end_line: int) -> tuple[str, int, int, list[str]]:
        for marker_line, next_marker_line, path in self.segments:
            if marker_line < start_line <= end_line < next_marker_line:
                return (
                    path,
                    start_line - marker_line,
                    end_line - marker_line,
                    self.lines[marker_line: next_marker_line - 1],
                )
        raise SourceMapError(
            f"Yosys source range {start_line}-{end_line} does not resolve in one RTL source"
        )


def _module_matches(module_type: str, protected_module: str) -> bool:
    return (
        module_type == protected_module
        or f"\\{protected_module}\\" in module_type
        or module_type.endswith(f"\\{protected_module}")
    )


def _protection_kinds(
    *,
    module_type: str,
    relative_path: str | None,
    protected_modules: Sequence[str],
    protected_path_patterns: Sequence[str],
) -> tuple[ProtectionKind, ...]:
    protected = any(_module_matches(module_type, item) for item in protected_modules)
    protected = protected or (
        relative_path is not None
        and any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in protected_path_patterns)
    )
    if not protected:
        return ()
    lowered = f"{module_type} {relative_path or ''}".lower()
    kinds: set[ProtectionKind] = set()
    if "clock" in lowered or "divider" in lowered:
        kinds.add("CLOCK")
    if "cdc" in lowered or "sync" in lowered or "fifo" in lowered:
        kinds.add("CDC")
    if "reset" in lowered:
        kinds.add("RESET")
    if not kinds:
        kinds.add("CDC")
    return tuple(sorted(kinds))


def _span_text(lines: list[str], start: int, start_column: int, end: int, end_column: int) -> str:
    selected = lines[start - 1 : end]
    if not selected:
        raise SourceMapError("Yosys source span selects no RTL text")
    selected[0] = selected[0][start_column - 1 :]
    if len(selected) == 1:
        selected[0] = selected[0][: max(end_column - start_column + 1, 0)]
    else:
        selected[-1] = selected[-1][:end_column]
    return "\n".join(selected)


def _source_spans(
    source_attribute: object,
    *,
    bundle: _BundleIndex,
    owner_hierarchy: str,
    module_type: str,
    rtl_snapshot_hash: str,
    protected_modules: Sequence[str],
    protected_path_patterns: Sequence[str],
) -> tuple[SourceSpanRecord, ...]:
    if not isinstance(source_attribute, str):
        return ()
    records = []
    for match in _BUNDLE_SPAN.finditer(source_attribute):
        start_line = int(match.group("start_line"))
        end_line = int(match.group("end_line"))
        start_column = int(match.group("start_column"))
        end_column = int(match.group("end_column"))
        path, local_start, local_end, source_lines = bundle.resolve(start_line, end_line)
        kinds = _protection_kinds(
            module_type=module_type,
            relative_path=path,
            protected_modules=protected_modules,
            protected_path_patterns=protected_path_patterns,
        )
        text = _span_text(source_lines, local_start, start_column, local_end, end_column)
        identity = {
            "end_column": end_column,
            "end_line": local_end,
            "owner_hierarchy": owner_hierarchy,
            "relative_path": path,
            "rtl_snapshot_hash": rtl_snapshot_hash,
            "start_column": start_column,
            "start_line": local_start,
        }
        span_id = "source_" + canonical_sha256(identity).removeprefix("sha256:")[:24]
        records.append(
            SourceSpanRecord(
                source_span_id=span_id,
                rtl_snapshot_hash=rtl_snapshot_hash,
                relative_path=path,
                start_line=local_start,
                start_column=start_column,
                end_line=local_end,
                end_column=end_column,
                owner_hierarchy=owner_hierarchy,
                source_text_hash="sha256:" + sha256(text.encode()).hexdigest(),
                mapping_confidence=1.0,
                protected=bool(kinds),
                protection_kinds=kinds,
            )
        )
    return tuple(records)


def _object_id(candidate_id: str, semantic_name: str) -> str:
    digest = canonical_sha256(
        {"candidate_id": candidate_id, "semantic_name": semantic_name}
    ).removeprefix("sha256:")
    return f"mapped_{digest[:24]}"


def build_source_map(
    mapped_design: Mapping[str, Any],
    *,
    mapped_netlist: str,
    rtl_bundle: str,
    candidate_id: str,
    rtl_snapshot_hash: str,
    synthesis_structure_hash: str,
    protected_modules: Sequence[str],
    protected_path_patterns: Sequence[str],
    requested_object_names: Sequence[str] | None = None,
) -> SourceMapSnapshot:
    """Map emitted Yosys cells and pins to normalized RTL bundle spans."""

    raw_modules = mapped_design.get("modules")
    if not isinstance(raw_modules, Mapping):
        raise SourceMapError("mapped design contains no module mapping")
    modules = dict(raw_modules)
    emitted = _emitted_cell_names(mapped_netlist)
    top_names = [
        name
        for name, module in modules.items()
        if isinstance(module, Mapping)
        and isinstance(module.get("attributes"), Mapping)
        and module["attributes"].get("top") in {1, "1", "00000000000000000000000000000001"}
    ]
    if len(top_names) != 1:
        raise SourceMapError("mapped design must identify exactly one top module")

    renamed_cells: dict[str, tuple[tuple[str, str, Mapping[str, Any]], ...]] = {}
    for module_name, module in modules.items():
        if module_name not in emitted or not isinstance(module, Mapping):
            continue
        raw_cells = module.get("cells", {})
        if not isinstance(raw_cells, Mapping):
            raise SourceMapError(f"module {module_name} has invalid cell mapping")
        json_cells = tuple(raw_cells.items())
        netlist_cells = emitted[module_name]
        if len(json_cells) != len(netlist_cells):
            raise SourceMapError(f"cell count differs for emitted module {module_name}")
        aligned = []
        for (_, raw_cell), (emitted_type, emitted_name) in zip(
            json_cells, netlist_cells, strict=True
        ):
            if not isinstance(raw_cell, Mapping) or raw_cell.get("type") != emitted_type:
                raise SourceMapError(f"cell type order differs for emitted module {module_name}")
            aligned.append((emitted_name, emitted_type, raw_cell))
        renamed_cells[module_name] = tuple(aligned)

    requested = set(requested_object_names) if requested_object_names is not None else None
    bundle = _BundleIndex(rtl_bundle)
    objects: dict[str, MappedObjectRecord] = {}
    spans: dict[str, SourceSpanRecord] = {}

    def add_object(
        *,
        semantic_name: str,
        kind: MappedObjectKind,
        owner: str,
        module_type: str,
        cell_type: str | None,
        direction: str | None,
        fanout: int,
        direct_spans: tuple[SourceSpanRecord, ...],
        inherited_kinds: tuple[ProtectionKind, ...],
    ) -> None:
        if requested is not None and semantic_name not in requested:
            return
        for span in direct_spans:
            spans[span.source_span_id] = span
        span_ids = tuple(sorted(span.source_span_id for span in direct_spans))
        direct_kinds = {kind for span in direct_spans for kind in span.protection_kinds}
        kinds = tuple(sorted(set(inherited_kinds) | direct_kinds))
        objects[semantic_name] = MappedObjectRecord(
            object_id=_object_id(candidate_id, semantic_name),
            semantic_name=semantic_name,
            kind=kind,
            owner_hierarchy=owner,
            module_type=module_type,
            cell_type=cell_type,
            port_direction=direction,
            source_span_ids=span_ids,
            fanout=fanout,
            protected=bool(kinds),
            protection_kinds=kinds,
        )

    def visit(module_name: str, hierarchy: str, inherited: tuple[ProtectionKind, ...]) -> None:
        raw_module = modules.get(module_name)
        if not isinstance(raw_module, Mapping) or module_name not in renamed_cells:
            raise SourceMapError(
                f"hierarchical module is absent from mapped netlist: {module_name}"
            )
        module_kinds = tuple(
            sorted(
                set(inherited)
                | set(
                    _protection_kinds(
                        module_type=module_name,
                        relative_path=None,
                        protected_modules=protected_modules,
                        protected_path_patterns=protected_path_patterns,
                    )
                )
            )
        )
        owner = hierarchy or module_name
        attributes = raw_module.get("attributes", {})
        module_spans = _source_spans(
            attributes.get("src") if isinstance(attributes, Mapping) else None,
            bundle=bundle,
            owner_hierarchy=owner,
            module_type=module_name,
            rtl_snapshot_hash=rtl_snapshot_hash,
            protected_modules=protected_modules,
            protected_path_patterns=protected_path_patterns,
        )
        raw_ports = raw_module.get("ports", {})
        if isinstance(raw_ports, Mapping):
            for port_name, raw_port in raw_ports.items():
                if not isinstance(raw_port, Mapping):
                    continue
                path = f"{hierarchy}/{port_name}" if hierarchy else str(port_name)
                direction = raw_port.get("direction")
                if direction not in {"input", "output", "inout"}:
                    raise SourceMapError(f"invalid direction for port {path}")
                for prefix, kind in (("port", "PORT"), ("pin", "PIN")):
                    add_object(
                        semantic_name=f"{prefix}:{path}",
                        kind=kind,
                        owner=owner,
                        module_type=module_name,
                        cell_type=None,
                        direction=direction if kind == "PIN" else None,
                        fanout=0,
                        direct_spans=module_spans,
                        inherited_kinds=module_kinds,
                    )
                    bits = raw_port.get("bits", [])
                    if isinstance(bits, list) and len(bits) > 1:
                        offset = raw_port.get("offset", 0)
                        if not isinstance(offset, int):
                            raise SourceMapError(f"invalid bit offset for port {path}")
                        for index in range(offset, offset + len(bits)):
                            add_object(
                                semantic_name=f"{prefix}:{path}[{index}]",
                                kind=kind,
                                owner=owner,
                                module_type=module_name,
                                cell_type=None,
                                direction=direction if kind == "PIN" else None,
                                fanout=0,
                                direct_spans=module_spans,
                                inherited_kinds=module_kinds,
                            )

        aligned_cells = renamed_cells[module_name]
        load_counts: Counter[int | str] = Counter()
        for _, _, raw_cell in aligned_cells:
            directions = raw_cell.get("port_directions", {})
            connections = raw_cell.get("connections", {})
            if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
                raise SourceMapError(f"invalid cell connectivity in module {module_name}")
            for port_name, bits in connections.items():
                if directions.get(port_name) == "input" and isinstance(bits, list):
                    load_counts.update(bits)

        for cell_name, cell_type, raw_cell in aligned_cells:
            cell_path = f"{hierarchy}/{cell_name}" if hierarchy else cell_name
            cell_kinds = tuple(
                sorted(
                    set(module_kinds)
                    | set(
                        _protection_kinds(
                            module_type=cell_type,
                            relative_path=None,
                            protected_modules=protected_modules,
                            protected_path_patterns=protected_path_patterns,
                        )
                    )
                )
            )
            attributes = raw_cell.get("attributes", {})
            direct_spans = _source_spans(
                attributes.get("src") if isinstance(attributes, Mapping) else None,
                bundle=bundle,
                owner_hierarchy=cell_path,
                module_type=cell_type,
                rtl_snapshot_hash=rtl_snapshot_hash,
                protected_modules=protected_modules,
                protected_path_patterns=protected_path_patterns,
            )
            directions = raw_cell.get("port_directions", {})
            connections = raw_cell.get("connections", {})
            if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
                raise SourceMapError(f"invalid connectivity for cell {cell_path}")
            connected_bits = [
                bit
                for bits in connections.values()
                if isinstance(bits, list)
                for bit in bits
            ]
            add_object(
                semantic_name=f"cell:{cell_path}",
                kind="CELL",
                owner=owner,
                module_type=module_name,
                cell_type=cell_type,
                direction=None,
                fanout=max((load_counts[bit] for bit in connected_bits), default=0),
                direct_spans=direct_spans,
                inherited_kinds=cell_kinds,
            )
            for port_name, direction in directions.items():
                if direction not in {"input", "output", "inout"}:
                    raise SourceMapError(f"invalid direction for pin {cell_path}/{port_name}")
                bits = connections.get(port_name, [])
                fanout = (
                    max((load_counts[bit] for bit in bits), default=0)
                    if isinstance(bits, list)
                    else 0
                )
                add_object(
                    semantic_name=f"pin:{cell_path}/{port_name}",
                    kind="PIN",
                    owner=cell_path,
                    module_type=module_name,
                    cell_type=cell_type,
                    direction=direction,
                    fanout=fanout,
                    direct_spans=direct_spans,
                    inherited_kinds=cell_kinds,
                )
            if cell_type in renamed_cells:
                visit(cell_type, cell_path, cell_kinds)

    visit(top_names[0], "", ())
    if not objects:
        raise SourceMapError("source map contains no requested mapped objects")
    if not spans:
        raise SourceMapError("source map contains no RTL source spans")
    payload = {
        "candidate_id": candidate_id,
        "rtl_snapshot_hash": rtl_snapshot_hash,
        "synthesis_structure_hash": synthesis_structure_hash,
        "mapped_objects": tuple(objects[name] for name in sorted(objects)),
        "source_spans": tuple(spans[name] for name in sorted(spans)),
    }
    hash_payload = {
        "schema_version": 1,
        **payload,
        "mapped_objects": tuple(
            item.model_dump(mode="json") for item in payload["mapped_objects"]
        ),
        "source_spans": tuple(item.model_dump(mode="json") for item in payload["source_spans"]),
    }
    return SourceMapSnapshot(**payload, source_map_hash=canonical_sha256(hash_payload))


def enrich_critical_paths(
    paths: Sequence[CriticalPathRecord], source_map: SourceMapSnapshot
) -> tuple[CriticalPathRecord, ...]:
    """Attach source spans and structural fanout without changing path identity."""

    mapped = {item.semantic_name: item for item in source_map.mapped_objects}
    enriched = []
    for path in paths:
        if path.candidate_id != source_map.candidate_id:
            raise SourceMapError(f"path {path.path_id} candidate differs from source map")
        records = [mapped[name] for name in path.object_sequence if name in mapped]
        for endpoint in (path.startpoint, path.endpoint):
            if endpoint in mapped:
                records.append(mapped[endpoint])
        span_ids = tuple(sorted({span for record in records for span in record.source_span_ids}))
        if not span_ids:
            raise SourceMapError(f"path {path.path_id} resolves to no RTL source span")
        payload = path.model_dump(mode="json")
        payload.update(
            source_span_refs=span_ids,
            max_fanout=max((record.fanout for record in records), default=0),
        )
        enriched.append(CriticalPathRecord.model_validate(payload))
    return tuple(sorted(enriched, key=lambda item: item.path_id))


__all__ = [
    "MappedObjectRecord",
    "SourceMapError",
    "SourceMapSnapshot",
    "build_source_map",
    "enrich_critical_paths",
]
