"""Immutable, content-addressed M3 design evidence graph construction."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Self

import zstandard
from pydantic import Field, model_validator

from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.analysis import (
    CDCInventory,
    ClockInventory,
    CriticalPathRecord,
    EvidenceGraphSnapshot,
)
from nova_rtl.contracts.base import (
    EvidenceRef,
    HashRef,
    StrictContract,
    canonical_json_bytes,
    canonical_sha256,
)
from nova_rtl.evidence.models import (
    EvidenceEdge,
    EvidenceInputIdentity,
    EvidenceNode,
)
from nova_rtl.evidence.source_map import SourceMapSnapshot

_GRAPH_ARTIFACT_ID = "evidence_graph_zstd"
_SOURCE_MAP_ARTIFACT_ID = "evidence_source_map"
_PATH_RECORD_ARTIFACT_ID = "evidence_critical_paths"
_PRODUCER_STAGE_ID = "stage_evidence_graph"
_STABLE_ARTIFACT_TIME = datetime(1970, 1, 1, tzinfo=UTC)


class EvidenceGraphBuildError(ValueError):
    """Evidence inputs cannot form a trustworthy immutable graph."""


class EvidenceGraphBuildInputs(StrictContract):
    """Typed M2 evidence consumed by deterministic M3 graph construction."""

    input_identity: EvidenceInputIdentity
    source_map: SourceMapSnapshot
    critical_paths: tuple[CriticalPathRecord, ...] = Field(min_length=1)
    clock_inventory: ClockInventory
    cdc_inventory: CDCInventory


class CriticalPathCollection(StrictContract):
    schema_version: Literal[1] = 1
    evidence_input_hash: HashRef
    records: tuple[CriticalPathRecord, ...] = Field(min_length=1)
    collection_hash: HashRef

    @model_validator(mode="after")
    def records_are_canonical_and_self_hashed(self) -> Self:
        ids = tuple(item.path_id for item in self.records)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("critical path records must be unique and canonically ordered")
        expected = canonical_sha256(self, exclude=frozenset({"collection_hash"}))
        if self.collection_hash != expected:
            raise ValueError("collection_hash does not match critical paths")
        return self


class EvidenceGraphDocument(StrictContract):
    schema_version: Literal[1] = 1
    evidence_input_hash: HashRef
    nodes: tuple[EvidenceNode, ...] = Field(min_length=1)
    edges: tuple[EvidenceEdge, ...]
    document_hash: HashRef

    @model_validator(mode="after")
    def graph_is_canonical_resolved_and_self_hashed(self) -> Self:
        node_ids = tuple(item.node_id for item in self.nodes)
        edge_ids = tuple(item.edge_id for item in self.edges)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("graph nodes must be unique and canonically ordered")
        if edge_ids != tuple(sorted(set(edge_ids))):
            raise ValueError("graph edges must be unique and canonically ordered")
        known_nodes = set(node_ids)
        if any(
            edge.source_node_id not in known_nodes or edge.target_node_id not in known_nodes
            for edge in self.edges
        ):
            raise ValueError("graph edge references an unknown node")
        expected = canonical_sha256(self, exclude=frozenset({"document_hash"}))
        if self.document_hash != expected:
            raise ValueError("document_hash does not match evidence graph")
        return self


def _stable_ref(ref: object) -> object:
    payload = ref.model_dump(mode="python")  # type: ignore[attr-defined]
    payload["created_at"] = _STABLE_ARTIFACT_TIME
    return type(ref).model_validate(payload)


def _entity_id(prefix: str, payload: Mapping[str, object]) -> str:
    digest = canonical_sha256(payload).removeprefix("sha256:")
    return f"{prefix}_{digest[:24]}"


def _evidence_ref(
    *,
    semantic_key: str,
    kind: str,
    artifact_id: str,
    snapshot_hash: str,
    json_pointer: str | None,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=_entity_id(
            "evidence",
            {"artifact_id": artifact_id, "kind": kind, "semantic_key": semantic_key},
        ),
        kind=kind,
        artifact_id=artifact_id,
        json_pointer=json_pointer,
        snapshot_hash=snapshot_hash,
    )


def _node_id(snapshot_hash: str, kind: str, semantic_id: str) -> str:
    return _entity_id(
        "node",
        {"kind": kind, "semantic_id": semantic_id, "snapshot_hash": snapshot_hash},
    )


def _edge_id(
    snapshot_hash: str,
    kind: str,
    source_node_id: str,
    target_node_id: str,
    attributes: Mapping[str, object],
) -> str:
    return _entity_id(
        "edge",
        {
            "attributes": dict(attributes),
            "kind": kind,
            "snapshot_hash": snapshot_hash,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
        },
    )


def _snapshot_hash(evidence_input_hash: str) -> str:
    return canonical_sha256(
        {
            "edge_schema_version": 1,
            "evidence_input_hash": evidence_input_hash,
            "node_schema_version": 1,
        }
    )


def _matches_structural_endpoint(semantic_name: str, endpoint: str) -> bool:
    structural_name = semantic_name.split(":", maxsplit=1)[-1]
    endpoint = endpoint.split(":", maxsplit=1)[-1]
    base_name = structural_name.split("[", maxsplit=1)[0]
    return base_name == endpoint or base_name.startswith(f"{endpoint}/")


def _matches_endpoint_leaf(semantic_name: str, endpoint: str) -> bool:
    structural_name = semantic_name.split(":", maxsplit=1)[-1]
    base_name = structural_name.split("[", maxsplit=1)[0]
    normalized_endpoint = endpoint.split(":", maxsplit=1)[-1]
    return base_name.rsplit("/", maxsplit=1)[-1] == normalized_endpoint.rsplit(
        "/", maxsplit=1
    )[-1]


def _validated_inputs(inputs: EvidenceGraphBuildInputs) -> None:
    identity = inputs.input_identity
    if inputs.source_map.candidate_id != identity.candidate_id:
        raise EvidenceGraphBuildError("source map candidate differs from evidence identity")
    if inputs.source_map.rtl_snapshot_hash != identity.rtl_snapshot_hash:
        raise EvidenceGraphBuildError("source map RTL snapshot hash differs")
    if inputs.source_map.synthesis_structure_hash != identity.synthesis_structure_hash:
        raise EvidenceGraphBuildError("source map synthesis structure hash differs")
    if inputs.clock_inventory.clock_graph_hash != identity.clock_inventory_hash:
        raise EvidenceGraphBuildError("clock inventory hash differs from evidence identity")
    if inputs.cdc_inventory.inventory_hash != identity.cdc_inventory_hash:
        raise EvidenceGraphBuildError("CDC inventory hash differs from evidence identity")
    if inputs.clock_inventory.candidate_id != identity.candidate_id:
        raise EvidenceGraphBuildError("clock inventory candidate differs")
    if inputs.cdc_inventory.candidate_id != identity.candidate_id:
        raise EvidenceGraphBuildError("CDC inventory candidate differs")
    known_views = {item.analysis_view_id for item in identity.analysis_views}
    known_spans = {item.source_span_id for item in inputs.source_map.source_spans}
    for path in inputs.critical_paths:
        if path.candidate_id != identity.candidate_id:
            raise EvidenceGraphBuildError(f"path {path.path_id} candidate differs")
        if path.analysis_view_id not in known_views:
            raise EvidenceGraphBuildError(f"path {path.path_id} uses an unknown analysis view")
        if missing := set(path.source_span_refs) - known_spans:
            raise EvidenceGraphBuildError(
                f"path {path.path_id} references unknown source span: {sorted(missing)[0]}"
            )


def _path_collection(inputs: EvidenceGraphBuildInputs) -> CriticalPathCollection:
    records = tuple(sorted(inputs.critical_paths, key=lambda item: item.path_id))
    payload = {
        "schema_version": 1,
        "evidence_input_hash": inputs.input_identity.identity_hash,
        "records": tuple(item.model_dump(mode="json") for item in records),
    }
    return CriticalPathCollection(
        evidence_input_hash=inputs.input_identity.identity_hash,
        records=records,
        collection_hash=canonical_sha256(payload),
    )


def _build_document(inputs: EvidenceGraphBuildInputs) -> EvidenceGraphDocument:
    snapshot_hash = _snapshot_hash(inputs.input_identity.identity_hash)
    candidate_id = inputs.input_identity.candidate_id
    nodes: dict[str, EvidenceNode] = {}
    semantic_nodes: dict[str, str] = {}

    def add_node(
        *,
        kind: str,
        semantic_id: str,
        semantic_name: str,
        attributes: dict[str, object],
        provenance: str,
        evidence_kind: str,
        artifact_id: str,
        pointer: str | None,
        protected: bool = False,
        protection_kinds: tuple[str, ...] = (),
    ) -> str:
        node_id = _node_id(snapshot_hash, kind, semantic_id)
        node = EvidenceNode(
            node_id=node_id,
            candidate_id=candidate_id,
            kind=kind,
            semantic_id=semantic_id,
            attributes=attributes,
            provenance=provenance,
            confidence=1.0,
            evidence_refs=(
                _evidence_ref(
                    semantic_key=semantic_id,
                    kind=evidence_kind,
                    artifact_id=artifact_id,
                    snapshot_hash=snapshot_hash,
                    json_pointer=pointer,
                ),
            ),
            protected=protected,
            protection_kinds=protection_kinds,
        )
        nodes[node_id] = node
        semantic_nodes[semantic_name] = node_id
        return node_id

    ordered_spans = tuple(
        sorted(inputs.source_map.source_spans, key=lambda item: item.source_span_id)
    )
    for index, span in enumerate(ordered_spans):
        add_node(
            kind="SOURCE_SPAN",
            semantic_id=span.source_span_id,
            semantic_name=f"source:{span.source_span_id}",
            attributes={
                "end_line": span.end_line,
                "mapping_confidence": span.mapping_confidence,
                "owner_hierarchy": span.owner_hierarchy,
                "relative_path": span.relative_path,
                "start_line": span.start_line,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="SOURCE_SPAN",
            artifact_id=_SOURCE_MAP_ARTIFACT_ID,
            pointer=f"/source_spans/{index}",
            protected=span.protected,
            protection_kinds=span.protection_kinds,
        )

    ordered_objects = tuple(
        sorted(inputs.source_map.mapped_objects, key=lambda item: item.semantic_name)
    )
    mapped_module_types = {mapped.module_type for mapped in ordered_objects}
    module_nodes: dict[tuple[str, str], str] = {}
    for item in ordered_objects:
        module_key = (item.owner_hierarchy, item.module_type)
        if module_key not in module_nodes:
            semantic_id = _entity_id(
                "module",
                {"module_type": item.module_type, "owner_hierarchy": item.owner_hierarchy},
            )
            module_nodes[module_key] = add_node(
                kind="MODULE",
                semantic_id=semantic_id,
                semantic_name=f"module:{item.owner_hierarchy}:{item.module_type}",
                attributes={
                    "module_type": item.module_type,
                    "owner_hierarchy": item.owner_hierarchy,
                },
                provenance="DETERMINISTIC_DERIVED",
                evidence_kind="CONE",
                artifact_id=_SOURCE_MAP_ARTIFACT_ID,
                pointer=None,
            )
    for index, item in enumerate(ordered_objects):
        if item.kind == "CELL" and "DFF" in (item.cell_type or "").upper():
            node_kind = "REGISTER"
        elif item.kind == "CELL" and item.cell_type not in mapped_module_types:
            node_kind = "EXPRESSION"
        else:
            node_kind = item.kind
        add_node(
            kind=node_kind,
            semantic_id=item.object_id,
            semantic_name=item.semantic_name,
            attributes={
                "cell_type": item.cell_type,
                "fanout": item.fanout,
                "module_type": item.module_type,
                "owner_hierarchy": item.owner_hierarchy,
                "semantic_name": item.semantic_name,
                "structural_alias_count": len(item.structural_aliases),
            },
            provenance="TOOL_MEASURED",
            evidence_kind="CONE",
            artifact_id=_SOURCE_MAP_ARTIFACT_ID,
            pointer=f"/mapped_objects/{index}",
            protected=item.protected,
            protection_kinds=item.protection_kinds,
        )

    connection_counts = Counter(
        edge.net_id for edge in inputs.source_map.connectivity_edges
    )
    for net_id in sorted(connection_counts):
        add_node(
            kind="NET",
            semantic_id=net_id,
            semantic_name=f"net:{net_id}",
            attributes={"connection_count": connection_counts[net_id]},
            provenance="TOOL_MEASURED",
            evidence_kind="CONE",
            artifact_id=_SOURCE_MAP_ARTIFACT_ID,
            pointer=None,
        )

    constraint_node = add_node(
        kind="CONSTRAINT_BINDING",
        semantic_id=_entity_id(
            "constraint_binding",
            {"constraint_binding_hash": inputs.input_identity.constraint_binding_hash},
        ),
        semantic_name="constraint:resolved_binding",
        attributes={
            "constraint_binding_hash": inputs.input_identity.constraint_binding_hash,
        },
        provenance="TOOL_MEASURED",
        evidence_kind="BINDING",
        artifact_id=_GRAPH_ARTIFACT_ID,
        pointer=None,
    )

    for view in inputs.input_identity.analysis_views:
        add_node(
            kind="ANALYSIS_VIEW",
            semantic_id=view.analysis_view_id,
            semantic_name=f"view:{view.analysis_view_id}",
            attributes={
                "analysis_view_hash": view.analysis_view_hash,
                "openroad_stage_result_hash": view.openroad_stage_result_hash,
                "opensta_stage_result_hash": view.opensta_stage_result_hash,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="METRIC",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
        )
        add_node(
            kind="PHYSICAL_REGION",
            semantic_id=_entity_id(
                "physical_region",
                {
                    "analysis_view_id": view.analysis_view_id,
                    "openroad_stage_result_hash": view.openroad_stage_result_hash,
                },
            ),
            semantic_name=f"physical:{view.analysis_view_id}",
            attributes={
                "analysis_view_id": view.analysis_view_id,
                "buffer_count": view.physical_buffer_count,
                "cell_count": view.physical_cell_count,
                "congestion_overflow": view.congestion_overflow,
                "openroad_stage_result_hash": view.openroad_stage_result_hash,
                "openroad_metrics_hash": view.openroad_metrics_hash,
                "physical_area_um2": view.physical_area_um2,
                "register_count": view.physical_register_count,
                "wirelength_um": view.wirelength_um,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="PHYSICAL",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
        )

    domain_nodes: dict[str, str] = {}
    domain_ids = {
        item.domain_id for item in inputs.clock_inventory.master_clocks
    } | {item.domain_id for item in inputs.clock_inventory.generated_clocks}
    for domain_id in sorted(domain_ids):
        domain_nodes[domain_id] = add_node(
            kind="CLOCK_DOMAIN",
            semantic_id=domain_id,
            semantic_name=f"domain:{domain_id}",
            attributes={"domain_id": domain_id},
            provenance="DETERMINISTIC_DERIVED",
            evidence_kind="CLOCK",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
            protected=True,
            protection_kinds=("CLOCK",),
        )

    for clock in inputs.clock_inventory.master_clocks:
        add_node(
            kind="CLOCK",
            semantic_id=clock.clock_id,
            semantic_name=f"clock:{clock.clock_id}",
            attributes={
                "active_consumer_count": clock.active_consumer_count,
                "domain_id": clock.domain_id,
                "period_ns": clock.period_ns,
                "source_object": clock.source_object,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="CLOCK",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
            protected=True,
            protection_kinds=("CLOCK",),
        )
    for clock in inputs.clock_inventory.generated_clocks:
        add_node(
            kind="GENERATED_CLOCK",
            semantic_id=clock.clock_id,
            semantic_name=f"clock:{clock.clock_id}",
            attributes={
                "active_consumer_count": clock.active_consumer_count,
                "divide_by": clock.divide_by,
                "domain_id": clock.domain_id,
                "master_clock_id": clock.master_clock_id,
                "source_object": clock.source_object,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="CLOCK",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
            protected=True,
            protection_kinds=("CLOCK",),
        )

    connectivity_neighbors: dict[str, set[str]] = {}
    for connection in inputs.source_map.connectivity_edges:
        connectivity_neighbors.setdefault(connection.source_object, set()).add(
            connection.destination_object
        )
        connectivity_neighbors.setdefault(connection.destination_object, set()).add(
            connection.source_object
        )

    for crossing in inputs.cdc_inventory.crossings:
        add_node(
            kind="CDC_STRUCTURE",
            semantic_id=crossing.crossing_id,
            semantic_name=f"cdc:{crossing.crossing_id}",
            attributes={
                "destination_domain_id": crossing.destination_domain_id,
                "destination_object": crossing.destination_object,
                "recognized_pattern": crossing.recognized_pattern,
                "signal_class": crossing.signal_class,
                "source_domain_id": crossing.source_domain_id,
                "source_object": crossing.source_object,
                "status": crossing.status,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="CDC",
            artifact_id=_GRAPH_ARTIFACT_ID,
            pointer=None,
            protected=True,
            protection_kinds=("CDC",),
        )

    ordered_paths = tuple(sorted(inputs.critical_paths, key=lambda item: item.path_id))
    path_group_nodes: dict[str, str] = {}
    for path_group in sorted({path.path_group for path in ordered_paths}):
        path_group_nodes[path_group] = add_node(
            kind="PATH_GROUP",
            semantic_id=_entity_id("path_group", {"path_group": path_group}),
            semantic_name=f"path_group:{path_group}",
            attributes={"path_group": path_group},
            provenance="DETERMINISTIC_DERIVED",
            evidence_kind="PATH",
            artifact_id=_PATH_RECORD_ARTIFACT_ID,
            pointer=None,
        )
    for index, path in enumerate(ordered_paths):
        add_node(
            kind="TIMING_PATH",
            semantic_id=path.path_id,
            semantic_name=f"path:{path.path_id}",
            attributes={
                "analysis_view_id": path.analysis_view_id,
                "capture_clock_id": path.capture_clock_id,
                "cell_delay_ns": path.cell_delay_ns,
                "launch_clock_id": path.launch_clock_id,
                "logic_depth": path.logic_depth,
                "max_fanout": path.max_fanout,
                "net_delay_ns": path.net_delay_ns,
                "path_group": path.path_group,
                "slack_ns": path.slack_ns,
            },
            provenance="TOOL_MEASURED",
            evidence_kind="PATH",
            artifact_id=_PATH_RECORD_ARTIFACT_ID,
            pointer=f"/records/{index}",
        )
        if path.slack_ns < 0:
            add_node(
                kind="VIOLATION",
                semantic_id=_entity_id(
                    "violation", {"path_id": path.path_id, "slack_ns": path.slack_ns}
                ),
                semantic_name=f"violation:{path.path_id}",
                attributes={
                    "analysis_view_id": path.analysis_view_id,
                    "path_id": path.path_id,
                    "slack_ns": path.slack_ns,
                },
                provenance="TOOL_MEASURED",
                evidence_kind="METRIC",
                artifact_id=_PATH_RECORD_ARTIFACT_ID,
                pointer=f"/records/{index}",
            )

    edges: dict[str, EvidenceEdge] = {}

    def add_edge(
        *,
        kind: str,
        source: str,
        target: str,
        attributes: dict[str, object] | None = None,
        provenance: str = "DETERMINISTIC_DERIVED",
    ) -> None:
        values = attributes or {}
        edge_id = _edge_id(snapshot_hash, kind, source, target, values)
        edges[edge_id] = EvidenceEdge(
            edge_id=edge_id,
            kind=kind,
            source_node_id=source,
            target_node_id=target,
            attributes=values,
            provenance=provenance,
            confidence=1.0,
            evidence_refs=(
                _evidence_ref(
                    semantic_key=edge_id,
                    kind="ARC",
                    artifact_id=_GRAPH_ARTIFACT_ID,
                    snapshot_hash=snapshot_hash,
                    json_pointer=None,
                ),
            ),
        )

    for item in ordered_objects:
        object_node = semantic_nodes[item.semantic_name]
        add_edge(
            kind="CONTAINS",
            source=module_nodes[(item.owner_hierarchy, item.module_type)],
            target=object_node,
        )
        for span_id in item.source_span_ids:
            add_edge(
                kind="MAPS_TO",
                source=object_node,
                target=semantic_nodes[f"source:{span_id}"],
            )

    for connection in inputs.source_map.connectivity_edges:
        source = semantic_nodes[connection.source_object]
        destination = semantic_nodes[connection.destination_object]
        net = semantic_nodes[f"net:{connection.net_id}"]
        add_edge(kind="CONNECTS", source=source, target=net)
        add_edge(kind="CONNECTS", source=net, target=destination)
        add_edge(kind="DATA_DEPENDENCY", source=source, target=destination)

    for edge in inputs.clock_inventory.lineage_edges:
        add_edge(
            kind="GENERATED_FROM",
            source=semantic_nodes[f"clock:{edge.child_clock_id}"],
            target=semantic_nodes[f"clock:{edge.parent_clock_id}"],
        )

    for clock in (*inputs.clock_inventory.master_clocks, *inputs.clock_inventory.generated_clocks):
        add_edge(
            kind="CONTAINS",
            source=domain_nodes[clock.domain_id],
            target=semantic_nodes[f"clock:{clock.clock_id}"],
        )

    for view in inputs.input_identity.analysis_views:
        add_edge(
            kind="RESOLVES_CONSTRAINT",
            source=semantic_nodes[f"view:{view.analysis_view_id}"],
            target=constraint_node,
        )
        add_edge(
            kind="PHYSICALLY_NEAR",
            source=semantic_nodes[f"view:{view.analysis_view_id}"],
            target=semantic_nodes[f"physical:{view.analysis_view_id}"],
        )

    for crossing in inputs.cdc_inventory.crossings:
        crossing_node = semantic_nodes[f"cdc:{crossing.crossing_id}"]
        for role, object_name in (
            ("source", crossing.source_object),
            ("destination", crossing.destination_object),
        ):
            matching_objects = tuple(
                item
                for item in ordered_objects
                if _matches_structural_endpoint(item.semantic_name, object_name)
                or any(
                    _matches_structural_endpoint(alias, object_name)
                    for alias in item.structural_aliases
                )
            )
            if not matching_objects:
                matching_objects = tuple(
                    item
                    for item in ordered_objects
                    if any(
                        _matches_endpoint_leaf(alias, object_name)
                        for alias in item.structural_aliases
                    )
                )
            matching_nodes = tuple(
                semantic_nodes[item.semantic_name] for item in matching_objects
            )
            if not matching_nodes:
                raise EvidenceGraphBuildError(
                    f"CDC crossing {crossing.crossing_id} has unresolved {role} endpoint"
                )
            for matched_object, object_node in zip(
                matching_objects, matching_nodes, strict=True
            ):
                add_edge(
                    kind="CROSSES_DOMAIN",
                    source=object_node,
                    target=crossing_node,
                    attributes={"role": role},
                )
                add_edge(kind="PROTECTED_BY", source=object_node, target=crossing_node)
                for neighbor in sorted(
                    connectivity_neighbors.get(matched_object.semantic_name, set())
                ):
                    add_edge(
                        kind="NEAR_PROTECTED_BOUNDARY",
                        source=semantic_nodes[neighbor],
                        target=crossing_node,
                        attributes={"role": role},
                    )

    for path in ordered_paths:
        path_node = semantic_nodes[f"path:{path.path_id}"]
        missing_objects = [name for name in path.object_sequence if name not in semantic_nodes]
        if missing_objects:
            raise EvidenceGraphBuildError(
                f"path {path.path_id} contains unmapped object: {missing_objects[0]}"
            )
        object_nodes = tuple(semantic_nodes[name] for name in path.object_sequence)
        add_edge(kind="LAUNCHES", source=path_node, target=object_nodes[0])
        add_edge(kind="CAPTURES", source=path_node, target=object_nodes[-1])
        for first, second in zip(object_nodes, object_nodes[1:], strict=False):
            add_edge(kind="TIMING_ARC", source=first, target=second)
        add_edge(
            kind="CLOCKED_BY",
            source=path_node,
            target=semantic_nodes[f"clock:{path.launch_clock_id}"],
            attributes={"role": "launch"},
        )
        add_edge(
            kind="CLOCKED_BY",
            source=path_node,
            target=semantic_nodes[f"clock:{path.capture_clock_id}"],
            attributes={"role": "capture"},
        )
        add_edge(
            kind="IN_ANALYSIS_VIEW",
            source=path_node,
            target=semantic_nodes[f"view:{path.analysis_view_id}"],
        )
        add_edge(
            kind="MEMBER_OF_PATH_GROUP",
            source=path_node,
            target=path_group_nodes[path.path_group],
        )
        violation_name = f"violation:{path.path_id}"
        if violation_name in semantic_nodes:
            add_edge(
                kind="HAS_VIOLATION",
                source=path_node,
                target=semantic_nodes[violation_name],
            )
        for span_id in path.source_span_refs:
            add_edge(
                kind="MAPS_TO",
                source=path_node,
                target=semantic_nodes[f"source:{span_id}"],
            )

    ordered_nodes = tuple(nodes[node_id] for node_id in sorted(nodes))
    ordered_edges = tuple(edges[edge_id] for edge_id in sorted(edges))
    payload = {
        "schema_version": 1,
        "evidence_input_hash": inputs.input_identity.identity_hash,
        "nodes": tuple(item.model_dump(mode="json") for item in ordered_nodes),
        "edges": tuple(item.model_dump(mode="json") for item in ordered_edges),
    }
    return EvidenceGraphDocument(
        evidence_input_hash=inputs.input_identity.identity_hash,
        nodes=ordered_nodes,
        edges=ordered_edges,
        document_hash=canonical_sha256(payload),
    )


def build_evidence_graph(
    inputs: EvidenceGraphBuildInputs,
    *,
    artifact_store: ArtifactStore,
    producer_stage_result_id: str = _PRODUCER_STAGE_ID,
) -> EvidenceGraphSnapshot:
    """Build and atomically publish a canonical compressed evidence graph."""

    _validated_inputs(inputs)
    paths = _path_collection(inputs)
    document = _build_document(inputs)
    source_ref = _stable_ref(
        artifact_store.put_named_bytes(
            canonical_json_bytes(inputs.source_map),
            artifact_id=_SOURCE_MAP_ARTIFACT_ID,
            media_type="application/json",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=producer_stage_result_id,
        )
    )
    path_ref = _stable_ref(
        artifact_store.put_named_bytes(
            canonical_json_bytes(paths),
            artifact_id=_PATH_RECORD_ARTIFACT_ID,
            media_type="application/json",
            classification="INTERNAL",
            producer_stage_result_id=producer_stage_result_id,
        )
    )
    graph_bytes = canonical_json_bytes(document)
    compressed = zstandard.ZstdCompressor(
        level=3,
        threads=0,
        write_checksum=True,
        write_content_size=True,
        write_dict_id=False,
    ).compress(graph_bytes)
    graph_ref = _stable_ref(
        artifact_store.put_named_bytes(
            compressed,
            artifact_id=_GRAPH_ARTIFACT_ID,
            media_type="application/zstd",
            classification="RESTRICTED_RTL",
            producer_stage_result_id=producer_stage_result_id,
        )
    )

    node_counts = dict(sorted(Counter(item.kind for item in document.nodes).items()))
    edge_counts = dict(sorted(Counter(item.kind for item in document.edges).items()))
    resolution_index = {
        item.node_id: tuple(ref.model_dump(mode="json") for ref in item.evidence_refs)
        for item in document.nodes
    } | {
        item.edge_id: tuple(ref.model_dump(mode="json") for ref in item.evidence_refs)
        for item in document.edges
    }
    payload = {
        "schema_version": 1,
        "evidence_input_hash": inputs.input_identity.identity_hash,
        "node_schema_version": 1,
        "edge_schema_version": 1,
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "graph_artifact": graph_ref.model_dump(mode="json"),
        "source_map_artifact": source_ref.model_dump(mode="json"),
        "path_record_artifact": path_ref.model_dump(mode="json"),
        "clock_inventory_id": "clock_inventory_"
        + inputs.clock_inventory.clock_graph_hash.removeprefix("sha256:")[:16],
        "cdc_inventory_id": "cdc_inventory_"
        + inputs.cdc_inventory.inventory_hash.removeprefix("sha256:")[:16],
        "evidence_resolution_index_hash": canonical_sha256(resolution_index),
    }
    snapshot_hash = _snapshot_hash(inputs.input_identity.identity_hash)
    indexed_payload = {**payload, "snapshot_hash": snapshot_hash}
    return EvidenceGraphSnapshot(
        **indexed_payload,
        snapshot_index_hash=canonical_sha256(indexed_payload),
    )


__all__ = [
    "CriticalPathCollection",
    "EvidenceGraphBuildError",
    "EvidenceGraphBuildInputs",
    "EvidenceGraphDocument",
    "build_evidence_graph",
]
