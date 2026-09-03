"""Deterministic Yosys structure-to-RTL source mapping."""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

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
    structural_aliases: tuple[NonEmptyString, ...] = ()

    @model_serializer(mode="wrap")
    def serialize_legacy_compatibly(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = handler(self)
        if "structural_aliases" not in self.model_fields_set:
            serialized.pop("structural_aliases", None)
        return serialized

    @field_validator("source_span_ids", "protection_kinds", "structural_aliases")
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


class MappedConnectivityEdge(StrictContract):
    """One directionally verified synthesized-net connection between mapped pins."""

    net_id: EntityId
    source_object: NonEmptyString
    destination_object: NonEmptyString


class SourceMapSnapshot(StrictContract):
    """Canonical source spans and structural names for one synthesized candidate."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    rtl_snapshot_hash: HashRef
    synthesis_structure_hash: HashRef
    mapped_objects: tuple[MappedObjectRecord, ...] = Field(min_length=1)
    connectivity_edges: tuple[MappedConnectivityEdge, ...] = ()
    source_spans: tuple[SourceSpanRecord, ...] = Field(min_length=1)
    source_map_hash: HashRef

    @model_serializer(mode="wrap")
    def serialize_legacy_compatibly(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = handler(self)
        if "connectivity_edges" not in self.model_fields_set:
            serialized.pop("connectivity_edges", None)
        return serialized

    @model_validator(mode="after")
    def contents_are_canonical_and_self_hashed(self) -> Self:
        object_names = tuple(item.semantic_name for item in self.mapped_objects)
        span_ids = tuple(item.source_span_id for item in self.source_spans)
        _canonical_unique(object_names, "mapped object names")
        _canonical_unique(span_ids, "source span IDs")
        known_spans = set(span_ids)
        if any(set(item.source_span_ids) - known_spans for item in self.mapped_objects):
            raise ValueError("mapped object references an unknown source span")
        edge_keys = tuple(
            (item.net_id, item.source_object, item.destination_object)
            for item in self.connectivity_edges
        )
        if edge_keys != tuple(sorted(set(edge_keys))):
            raise ValueError("connectivity edges must be unique and canonically ordered")
        known_objects = set(object_names)
        if any(
            item.source_object not in known_objects
            or item.destination_object not in known_objects
            for item in self.connectivity_edges
        ):
            raise ValueError("connectivity edge references an unknown mapped object")
        if "connectivity_edges" not in self.model_fields_set:
            expected = canonical_sha256(
                self.model_dump(
                    mode="json", exclude={"connectivity_edges", "source_map_hash"}
                )
            )
        else:
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


def _emitted_cells(
    mapped_netlist: str,
) -> dict[str, tuple[tuple[str, str, dict[str, str]], ...]]:
    modules: dict[str, list[tuple[str, str, dict[str, str]]]] = {}
    current: str | None = None
    pending: tuple[str, str, dict[str, str]] | None = None
    for line in mapped_netlist.splitlines():
        if line.startswith("module "):
            current, _ = _identifier(line.removeprefix("module "))
            if current in modules:
                raise SourceMapError(f"duplicate mapped netlist module: {current}")
            modules[current] = []
        elif line.startswith("endmodule"):
            if pending is not None:
                raise SourceMapError("unterminated mapped netlist cell instance")
            current = None
        elif pending is not None:
            if line.strip() == ");":
                if current is None:
                    raise SourceMapError("mapped netlist cell is outside a module")
                modules[current].append(pending)
                pending = None
                continue
            match = re.fullmatch(r"\s*\.(?P<port>\\?\S+)\((?P<signal>.*)\),?\s*", line)
            if match is None:
                raise SourceMapError("mapped netlist cell connection is not canonical")
            port = match.group("port").removeprefix("\\").rstrip()
            signal = re.sub(r"\s+", "", match.group("signal"))
            if not signal or port in pending[2]:
                raise SourceMapError("mapped netlist cell connection is invalid")
            pending[2][port] = signal
        elif current is not None and line.startswith("  ") and line.rstrip().endswith("("):
            cell_type, rest = _identifier(line)
            cell_name, rest = _identifier(rest)
            if rest.strip() == "(":
                pending = (cell_type, cell_name, {})
    if pending is not None:
        raise SourceMapError("unterminated mapped netlist cell instance")
    if not modules:
        raise SourceMapError("mapped netlist contains no modules")
    return {name: tuple(cells) for name, cells in modules.items()}


def _normalize_constant(signal: str) -> str:
    signal = signal.removeprefix("\\")
    match = re.fullmatch(r"(?:\d+)?'[sS]?[bBoOdDhH]([01xXzZ])", signal)
    return match.group(1).lower().replace("z", "x") if match is not None else signal


def _module_port_aliases(module: Mapping[str, Any]) -> dict[int | str, set[str]]:
    aliases: dict[int | str, set[str]] = {}
    raw_ports = module.get("ports", {})
    if not isinstance(raw_ports, Mapping):
        return aliases
    for port_name, raw_port in raw_ports.items():
        if not isinstance(raw_port, Mapping):
            continue
        bits = raw_port.get("bits", [])
        if not isinstance(bits, list):
            continue
        offset = raw_port.get("offset", 0)
        if not isinstance(offset, int):
            continue
        for position, bit in enumerate(bits):
            if not isinstance(bit, int):
                continue
            alias = str(port_name) if len(bits) == 1 else f"{port_name}[{offset + position}]"
            aliases.setdefault(bit, set()).add(alias)
    raw_netnames = module.get("netnames", {})
    if isinstance(raw_netnames, Mapping):
        for net_name, raw_net in raw_netnames.items():
            if str(net_name).startswith("$") or not isinstance(raw_net, Mapping):
                continue
            bits = raw_net.get("bits", [])
            if not isinstance(bits, list):
                continue
            offset = raw_net.get("offset", 0)
            if not isinstance(offset, int):
                continue
            for position, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                alias = (
                    str(net_name)
                    if len(bits) == 1
                    else f"{net_name}[{offset + position}]"
                )
                aliases.setdefault(bit, set()).add(alias)
    return aliases


def _verify_connectivity_alignment(
    module_name: str,
    module: Mapping[str, Any],
    json_cells: tuple[tuple[object, object], ...],
    emitted_cells: tuple[tuple[str, str, dict[str, str]], ...],
) -> None:
    """Prove positional renaming by comparing scalar connectivity equivalence classes."""

    raw_by_endpoint: dict[tuple[int, str], int | str] = {}
    emitted_by_endpoint: dict[tuple[int, str], str] = {}
    stable_aliases = _module_port_aliases(module)
    for index, ((json_name, raw_cell), (_, _, emitted_connections)) in enumerate(
        zip(json_cells, emitted_cells, strict=True)
    ):
        if not isinstance(raw_cell, Mapping):
            raise SourceMapError(f"invalid cell mapping in module {module_name}")
        raw_connections = raw_cell.get("connections", {})
        if not isinstance(raw_connections, Mapping):
            raise SourceMapError(f"invalid cell connectivity in module {module_name}")
        if set(raw_connections) != set(emitted_connections):
            raise SourceMapError(
                f"cell port set differs for emitted module {module_name}"
            )
        for port, bits in raw_connections.items():
            if not isinstance(bits, list) or len(bits) != 1:
                continue
            endpoint = (index, str(port))
            signal = emitted_connections[str(port)]
            if signal.startswith("{") or "," in signal:
                raise SourceMapError(
                    f"scalar cell connection differs for emitted module {module_name}"
                )
            bit = bits[0]
            normalized_signal = _normalize_constant(signal)
            normalized_bit = _normalize_constant(str(bit)) if isinstance(bit, str) else bit
            aliases = stable_aliases.get(bit, set())
            if aliases and normalized_signal not in aliases:
                raise SourceMapError(
                    f"cell connectivity differs for emitted module {module_name}: "
                    f"{json_name}/{port} expected {sorted(aliases)}, got {normalized_signal}"
                )
            if isinstance(bit, str) and normalized_signal != normalized_bit:
                raise SourceMapError(
                    f"cell constant connectivity differs for emitted module {module_name}"
                )
            raw_by_endpoint[endpoint] = bit
            emitted_by_endpoint[endpoint] = normalized_signal

    def shared_groups(values: Mapping[tuple[int, str], object]) -> set[tuple[tuple[int, str], ...]]:
        groups: dict[object, list[tuple[int, str]]] = {}
        for endpoint, value in values.items():
            groups.setdefault(value, []).append(endpoint)
        return {
            tuple(sorted(endpoints))
            for endpoints in groups.values()
            if len(endpoints) > 1
        }

    if shared_groups(raw_by_endpoint) != shared_groups(emitted_by_endpoint):
        raise SourceMapError(
            f"cell connectivity signature differs for emitted module {module_name}"
        )


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


def _has_selected_ancestor(value: str, selected: set[str]) -> bool:
    """Check hierarchical prefixes in bounded depth instead of scanning all selectors."""

    current = value
    while "/" in current:
        current = current.rsplit("/", maxsplit=1)[0]
        if current in selected:
            return True
    return False


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
    requested_endpoint_names: Sequence[str] = (),
) -> SourceMapSnapshot:
    """Map emitted Yosys cells and pins to normalized RTL bundle spans."""

    raw_modules = mapped_design.get("modules")
    if not isinstance(raw_modules, Mapping):
        raise SourceMapError("mapped design contains no module mapping")
    modules = dict(raw_modules)
    emitted = _emitted_cells(mapped_netlist)
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
        json_cell_names = tuple(str(name) for name, _ in json_cells)
        if json_cell_names != tuple(sorted(json_cell_names)):
            raise SourceMapError(
                f"cell JSON does not use canonical cell order in module {module_name}"
            )
        netlist_cells = emitted[module_name]
        if len(json_cells) != len(netlist_cells):
            raise SourceMapError(f"cell count differs for emitted module {module_name}")
        _verify_connectivity_alignment(module_name, module, json_cells, netlist_cells)
        aligned = []
        renamed_numbers: list[int] = []
        for (json_name, raw_cell), (emitted_type, emitted_name, _) in zip(
            json_cells, netlist_cells, strict=True
        ):
            if not isinstance(raw_cell, Mapping) or raw_cell.get("type") != emitted_type:
                raise SourceMapError(f"cell type order differs for emitted module {module_name}")
            if emitted_name != json_name:
                match = re.fullmatch(r"_(\d+)_", emitted_name)
                if match is None:
                    raise SourceMapError(
                        f"unexpected emitted cell rename in module {module_name}"
                    )
                renamed_numbers.append(int(match.group(1)))
            aligned.append((emitted_name, emitted_type, raw_cell))
        if renamed_numbers != sorted(set(renamed_numbers)):
            raise SourceMapError(
                f"emitted netlist does not use canonical cell order in module {module_name}"
            )
        renamed_cells[module_name] = tuple(aligned)

    requested = set(requested_object_names) if requested_object_names is not None else None
    if requested is not None and requested_endpoint_names:
        available_alias_bases: set[str] = set()

        def collect_aliases(module_name: str, hierarchy: str) -> None:
            raw_module = modules.get(module_name)
            if not isinstance(raw_module, Mapping):
                return
            raw_netnames = raw_module.get("netnames", {})
            if isinstance(raw_netnames, Mapping):
                for net_name, raw_net in raw_netnames.items():
                    if str(net_name).startswith("$") or not isinstance(raw_net, Mapping):
                        continue
                    path = f"{hierarchy}/{net_name}" if hierarchy else str(net_name)
                    available_alias_bases.add(path)
            for cell_name, cell_type, _ in renamed_cells.get(module_name, ()):
                if cell_type in renamed_cells:
                    path = f"{hierarchy}/{cell_name}" if hierarchy else cell_name
                    collect_aliases(cell_type, path)

        collect_aliases(top_names[0], "")
        for endpoint in requested_endpoint_names:
            normalized = endpoint.split(":", maxsplit=1)[-1]
            selected = normalized
            if not any(
                alias == normalized or alias.startswith(f"{normalized}/")
                for alias in available_alias_bases
            ):
                leaf = normalized.rsplit("/", maxsplit=1)[-1]
                leaf_matches = {
                    alias
                    for alias in available_alias_bases
                    if alias.rsplit("/", maxsplit=1)[-1] == leaf
                }
                if len(leaf_matches) != 1:
                    raise SourceMapError(
                        f"CDC endpoint alias is unresolved or ambiguous: {endpoint}"
                    )
                selected = next(iter(leaf_matches))
            requested.update(f"{prefix}:{selected}" for prefix in ("cell", "pin", "port"))
    bundle = _BundleIndex(rtl_bundle)
    objects: dict[str, MappedObjectRecord] = {}
    spans: dict[str, SourceSpanRecord] = {}
    pending_connectivity: set[tuple[str, str, str]] = set()
    requested_exact = requested or set()
    normalized_requested = {
        item.split(":", maxsplit=1)[-1] for item in requested_exact
    }

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
        structural_aliases: tuple[str, ...] = (),
    ) -> None:
        requested_base = semantic_name.split("[", maxsplit=1)[0]
        alias_bases = {item.split("[", maxsplit=1)[0] for item in structural_aliases}
        if (
            requested is not None
            and semantic_name not in requested_exact
            and requested_base not in requested_exact
            and not _has_selected_ancestor(requested_base, requested_exact)
            and not any(
                alias in normalized_requested
                or _has_selected_ancestor(alias, normalized_requested)
                for alias in alias_bases
            )
        ):
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
            structural_aliases=tuple(sorted(set(structural_aliases))),
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
        aliases_by_bit: dict[int, set[str]] = {}
        raw_netnames = raw_module.get("netnames", {})
        if isinstance(raw_netnames, Mapping):
            for net_name, raw_net in raw_netnames.items():
                if str(net_name).startswith("$") or not isinstance(raw_net, Mapping):
                    continue
                bits = raw_net.get("bits", [])
                if not isinstance(bits, list):
                    continue
                offset = raw_net.get("offset", 0)
                if not isinstance(offset, int):
                    continue
                net_path = f"{hierarchy}/{net_name}" if hierarchy else str(net_name)
                for position, bit in enumerate(bits):
                    if isinstance(bit, int):
                        alias = (
                            net_path
                            if len(bits) == 1
                            else f"{net_path}[{offset + position}]"
                        )
                        aliases_by_bit.setdefault(bit, set()).add(alias)
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
        endpoints_by_bit: dict[int, list[tuple[str, str]]] = {}
        if isinstance(raw_ports, Mapping):
            for port_name, raw_port in raw_ports.items():
                if not isinstance(raw_port, Mapping):
                    continue
                path = f"{hierarchy}/{port_name}" if hierarchy else str(port_name)
                direction = raw_port.get("direction")
                if direction not in {"input", "output", "inout"}:
                    raise SourceMapError(f"invalid direction for port {path}")
                raw_bits = raw_port.get("bits", [])
                if isinstance(raw_bits, list):
                    internal_direction = (
                        "output"
                        if direction == "input"
                        else "input" if direction == "output" else "inout"
                    )
                    for bit in raw_bits:
                        if isinstance(bit, int):
                            endpoints_by_bit.setdefault(bit, []).append(
                                (f"pin:{path}", internal_direction)
                            )
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
                        structural_aliases=tuple(
                            sorted(
                                {
                                    alias
                                    for bit in raw_bits
                                    if isinstance(bit, int)
                                    for alias in aliases_by_bit.get(bit, set())
                                }
                            )
                        ),
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
                                structural_aliases=tuple(
                                    sorted(aliases_by_bit.get(bits[index - offset], set()))
                                )
                                if isinstance(bits[index - offset], int)
                                else (),
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
            for port_name, direction in directions.items():
                bits = connections.get(port_name, [])
                if isinstance(bits, list):
                    for bit in bits:
                        if isinstance(bit, int):
                            endpoints_by_bit.setdefault(bit, []).append(
                                (f"pin:{cell_path}/{port_name}", str(direction))
                            )
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
                structural_aliases=tuple(
                    sorted(
                        {
                            alias
                            for bit in connected_bits
                            if isinstance(bit, int)
                            for alias in aliases_by_bit.get(bit, set())
                        }
                    )
                ),
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
                    structural_aliases=tuple(
                        sorted(
                            {
                                alias
                                for bit in bits
                                if isinstance(bit, int)
                                for alias in aliases_by_bit.get(bit, set())
                            }
                        )
                    )
                    if isinstance(bits, list)
                    else (),
                )
            if cell_type in renamed_cells:
                visit(cell_type, cell_path, cell_kinds)

        for bit, endpoints in endpoints_by_bit.items():
            sources = sorted(
                name
                for name, direction in endpoints
                if direction in {"output", "inout"} and name in objects
            )
            destinations = sorted(
                name
                for name, direction in endpoints
                if direction in {"input", "inout"} and name in objects
            )
            net_id = "net_" + canonical_sha256(
                {"bit": bit, "hierarchy": owner, "module_type": module_name}
            ).removeprefix("sha256:")[:24]
            for source in sources:
                for destination in destinations:
                    if source != destination:
                        pending_connectivity.add((net_id, source, destination))

    visit(top_names[0], "", ())
    if not objects:
        raise SourceMapError("source map contains no requested mapped objects")
    if not spans:
        raise SourceMapError("source map contains no RTL source spans")
    connectivity_edges = tuple(
        MappedConnectivityEdge(
            net_id=net_id,
            source_object=source,
            destination_object=destination,
        )
        for net_id, source, destination in sorted(pending_connectivity)
        if source in objects and destination in objects
    )
    payload = {
        "candidate_id": candidate_id,
        "rtl_snapshot_hash": rtl_snapshot_hash,
        "synthesis_structure_hash": synthesis_structure_hash,
        "mapped_objects": tuple(objects[name] for name in sorted(objects)),
        "connectivity_edges": connectivity_edges,
        "source_spans": tuple(spans[name] for name in sorted(spans)),
    }
    hash_payload = {
        "schema_version": 1,
        **payload,
        "mapped_objects": tuple(
            item.model_dump(mode="json") for item in payload["mapped_objects"]
        ),
        "connectivity_edges": tuple(
            item.model_dump(mode="json") for item in connectivity_edges
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
