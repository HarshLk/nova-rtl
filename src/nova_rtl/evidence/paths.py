"""Deterministic shared-cone clustering and root-cause classification."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence

from nova_rtl.contracts.analysis import CDCInventory, CriticalPathRecord
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.optimization import RootCause
from nova_rtl.evidence.models import PathCluster, PathFeatureVector
from nova_rtl.evidence.source_map import MappedObjectRecord, SourceMapSnapshot

ROOT_CAUSE_CATEGORIES = (
    "DEEP_PRIORITY_CHAIN",
    "UNBALANCED_BOOLEAN_TREE",
    "WIDE_COMPARATOR",
    "SERIAL_ARITHMETIC",
    "HIGH_FANOUT_CONTROL",
    "MUX_AFTER_ARITHMETIC",
    "REPEATED_DECODE",
    "FSM_DECODE_DEPTH",
    "RESOURCE_ARBITRATION",
    "PLACEMENT_OR_WIRE_DOMINATED",
    "CLOCK_OR_CONSTRAINT_ISSUE",
    "CDC_ADJACENT_UNSAFE_TO_EDIT",
    "UNCLASSIFIED",
)


class PathClusteringError(ValueError):
    """Critical paths cannot form deterministic source-mapped clusters."""


def _shared_cone(path: CriticalPathRecord) -> frozenset[str]:
    sequence = path.object_sequence
    return frozenset(sequence[1:-1] or sequence)


def _connected(first: CriticalPathRecord, second: CriticalPathRecord) -> bool:
    return bool(
        _shared_cone(first) & _shared_cone(second)
        or first.endpoint == second.endpoint
        or set(first.source_span_refs) & set(second.source_span_refs)
    )


def _partition(
    paths: tuple[CriticalPathRecord, ...], domain_by_path: Mapping[str, str]
) -> tuple[tuple[CriticalPathRecord, ...], ...]:
    parent = list(range(len(paths)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = root(first)
        second_root = root(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    for first_index, first in enumerate(paths):
        for second_index in range(first_index + 1, len(paths)):
            second = paths[second_index]
            if (
                domain_by_path[first.path_id] == domain_by_path[second.path_id]
                and _connected(first, second)
            ):
                union(first_index, second_index)

    groups: dict[int, list[CriticalPathRecord]] = {}
    for index, path in enumerate(paths):
        groups.setdefault(root(index), []).append(path)
    return tuple(
        tuple(sorted(group, key=lambda item: item.path_id))
        for _, group in sorted(groups.items())
    )


def _type_features(records: Sequence[MappedObjectRecord]) -> tuple[int, int, int, int]:
    cell_types = tuple((item.cell_type or "").upper() for item in records)
    mux_depth = sum(
        "MUX" in cell_type or cell_type.startswith(("AO", "OA"))
        for cell_type in cell_types
    )
    boolean_depth = sum(
        any(token in cell_type for token in ("AND", "OR", "XOR", "INV", "MUX", "AO"))
        for cell_type in cell_types
    )
    comparator_depth = sum(
        any(token in cell_type for token in ("XNOR", "CMP", "EQUAL"))
        for cell_type in cell_types
    )
    arithmetic_depth = sum(
        cell_type.startswith(("FA", "HA")) or "ADD" in cell_type
        for cell_type in cell_types
    )
    return mux_depth, boolean_depth, comparator_depth, arithmetic_depth


def _root_causes(
    features: PathFeatureVector,
    records: Sequence[MappedObjectRecord],
) -> tuple[RootCause, ...]:
    identity = " ".join(
        f"{item.semantic_name} {item.module_type} {item.cell_type or ''}".lower()
        for item in records
    )
    causes: list[RootCause] = []

    def add(category: str, confidence: float) -> None:
        if category not in {item.category for item in causes}:
            causes.append(RootCause(category=category, confidence=confidence))

    if features.protection_distance == 0:
        add("CDC_ADJACENT_UNSAFE_TO_EDIT", 1.0)
    elif features.physical_dominance >= 0.65:
        add("PLACEMENT_OR_WIRE_DOMINATED", 0.95)
    else:
        if (
            features.mux_depth >= 3
            or ("priority" in identity and features.logic_depth >= 3)
            or (
                "timing_opportunity_lane" in identity
                and features.logic_depth >= 4
            )
        ):
            add("DEEP_PRIORITY_CHAIN", 0.95)
        if features.boolean_depth >= 6:
            add("UNBALANCED_BOOLEAN_TREE", 0.85)
        if features.comparator_depth >= 3:
            add("WIDE_COMPARATOR", 0.82)
        if features.arithmetic_depth >= 3:
            add("SERIAL_ARITHMETIC", 0.82)
        if features.max_fanout >= 16:
            add("HIGH_FANOUT_CONTROL", 0.8)
        if features.mux_depth and features.arithmetic_depth:
            add("MUX_AFTER_ARITHMETIC", 0.78)
        if features.repeated_predicate_count >= 2:
            add("REPEATED_DECODE", 0.75)
        if "fsm" in identity and features.boolean_depth >= 3:
            add("FSM_DECODE_DEPTH", 0.74)
        if any(token in identity for token in ("arbiter", "scheduler", "resource")):
            add("RESOURCE_ARBITRATION", 0.72)
        if features.logic_depth <= 1 and features.cell_delay_fraction <= 0.25:
            add("CLOCK_OR_CONSTRAINT_ISSUE", 0.7)
    if not causes:
        add("UNCLASSIFIED", 0.5)
    order = {category: index for index, category in enumerate(ROOT_CAUSE_CATEGORIES)}
    return tuple(sorted(causes, key=lambda item: (-item.confidence, order[item.category])))


def cluster_paths(
    paths: Sequence[CriticalPathRecord],
    *,
    evidence_snapshot_hash: str,
    source_map: SourceMapSnapshot,
    clock_domain_by_id: Mapping[str, str],
    cdc_inventory: CDCInventory | None = None,
    tns_by_analysis_view: Mapping[str, float] | None = None,
    wns_by_analysis_view: Mapping[str, float] | None = None,
) -> tuple[PathCluster, ...]:
    """Cluster source-mapped paths by shared cone and classify root causes."""

    ordered_paths = tuple(sorted(paths, key=lambda item: item.path_id))
    if not ordered_paths:
        raise PathClusteringError("at least one critical path is required")
    if any(path.candidate_id != source_map.candidate_id for path in ordered_paths):
        raise PathClusteringError("critical path candidate differs from source map")
    object_by_name = {item.semantic_name: item for item in source_map.mapped_objects}
    span_by_id = {item.source_span_id: item for item in source_map.source_spans}
    domain_by_path = {}
    for path in ordered_paths:
        domain = clock_domain_by_id.get(path.capture_clock_id)
        if domain is None:
            raise PathClusteringError(
                f"path {path.path_id} capture clock has no domain mapping"
            )
        missing = set(path.object_sequence) - set(object_by_name)
        if missing:
            raise PathClusteringError(
                f"path {path.path_id} contains unmapped object: {sorted(missing)[0]}"
            )
        if set(path.source_span_refs) - set(span_by_id):
            raise PathClusteringError(f"path {path.path_id} contains unknown source span")
        domain_by_path[path.path_id] = domain

    cdc_endpoints = (
        {
            endpoint
            for crossing in cdc_inventory.crossings
            for endpoint in (crossing.source_object, crossing.destination_object)
        }
        if cdc_inventory is not None
        else set()
    )

    def matches_cdc_endpoint(semantic_name: str, endpoint: str) -> bool:
        structural_name = semantic_name.split(":", maxsplit=1)[-1]
        base_name = structural_name.split("[", maxsplit=1)[0]
        normalized_endpoint = endpoint.split(":", maxsplit=1)[-1]
        return base_name == normalized_endpoint or base_name.startswith(
            f"{normalized_endpoint}/"
        )

    def matches_endpoint_leaf(semantic_name: str, endpoint: str) -> bool:
        structural_name = semantic_name.split(":", maxsplit=1)[-1]
        base_name = structural_name.split("[", maxsplit=1)[0]
        normalized_endpoint = endpoint.split(":", maxsplit=1)[-1]
        return base_name.rsplit("/", maxsplit=1)[-1] == normalized_endpoint.rsplit(
            "/", maxsplit=1
        )[-1]

    def object_matches_cdc_endpoint(item: MappedObjectRecord, endpoint: str) -> bool:
        return matches_cdc_endpoint(item.semantic_name, endpoint) or any(
            matches_cdc_endpoint(alias, endpoint) for alias in item.structural_aliases
        )

    def objects_for_endpoint(endpoint: str) -> tuple[MappedObjectRecord, ...]:
        exact = tuple(
            item
            for item in source_map.mapped_objects
            if object_matches_cdc_endpoint(item, endpoint)
        )
        if exact:
            return exact
        return tuple(
            item
            for item in source_map.mapped_objects
            if any(
                matches_endpoint_leaf(alias, endpoint)
                for alias in item.structural_aliases
            )
        )

    cdc_objects_by_endpoint = {
        endpoint: objects_for_endpoint(endpoint) for endpoint in cdc_endpoints
    }
    cdc_object_ids = {
        item.object_id
        for objects in cdc_objects_by_endpoint.values()
        for item in objects
    }

    cdc_object_names = {
        item.semantic_name
        for item in source_map.mapped_objects
        if item.object_id in cdc_object_ids
    }
    if cdc_inventory is not None:
        for crossing in cdc_inventory.crossings:
            for role, endpoint in (
                ("source", crossing.source_object),
                ("destination", crossing.destination_object),
            ):
                if not cdc_objects_by_endpoint[endpoint]:
                    raise PathClusteringError(
                        f"CDC crossing {crossing.crossing_id} has unresolved {role} endpoint"
                    )
    cdc_neighbor_names = {
        edge.destination_object
        for edge in source_map.connectivity_edges
        if edge.source_object in cdc_object_names
    } | {
        edge.source_object
        for edge in source_map.connectivity_edges
        if edge.destination_object in cdc_object_names
    }

    negative_by_view = {
        view_id: sum(
            max(-path.slack_ns, 0.0)
            for path in ordered_paths
            if path.analysis_view_id == view_id
        )
        for view_id in {path.analysis_view_id for path in ordered_paths}
    }
    if tns_by_analysis_view is None:
        measured_tns_by_view = negative_by_view
    else:
        missing_views = set(negative_by_view) - set(tns_by_analysis_view)
        if missing_views:
            raise PathClusteringError(
                f"missing measured TNS for analysis view: {sorted(missing_views)[0]}"
            )
        measured_tns_by_view = {}
        for view_id, reported_negative in negative_by_view.items():
            measured_tns = float(tns_by_analysis_view[view_id])
            if not math.isfinite(measured_tns) or measured_tns > 1e-6:
                raise PathClusteringError(
                    f"invalid measured TNS for analysis view: {view_id}"
                )
            measured_negative = abs(min(measured_tns, 0.0))
            if (reported_negative > 1e-6) != (measured_negative > 1e-6):
                raise PathClusteringError(
                    f"measured TNS disagrees with critical paths for analysis view: {view_id}"
                )
            worst_negative = max(
                (
                    max(-path.slack_ns, 0.0)
                    for path in ordered_paths
                    if path.analysis_view_id == view_id
                ),
                default=0.0,
            )
            if measured_negative + 1e-4 < worst_negative:
                raise PathClusteringError(
                    f"measured TNS is smaller than WNS for analysis view: {view_id}"
                )
            measured_tns_by_view[view_id] = measured_negative
    if wns_by_analysis_view is not None:
        missing_wns = set(negative_by_view) - set(wns_by_analysis_view)
        if missing_wns:
            raise PathClusteringError(
                f"missing measured WNS for analysis view: {sorted(missing_wns)[0]}"
            )
        for view_id in negative_by_view:
            measured_wns = float(wns_by_analysis_view[view_id])
            parsed_wns = min(
                path.slack_ns
                for path in ordered_paths
                if path.analysis_view_id == view_id
            )
            if not math.isfinite(measured_wns) or abs(measured_wns - parsed_wns) > 1e-4:
                raise PathClusteringError(
                    f"measured WNS disagrees with critical paths for analysis view: {view_id}"
                )
    negative_total = sum(measured_tns_by_view.values())
    clusters = []
    for group in _partition(ordered_paths, domain_by_path):
        worst = min(group, key=lambda item: (item.slack_ns, item.path_id))
        object_names = tuple(name for path in group for name in path.object_sequence)
        object_counts = Counter(object_names)
        records = tuple(object_by_name[name] for name in object_names)
        ranked_objects = sorted(
            object_counts,
            key=lambda name: (-object_counts[name], object_by_name[name].object_id),
        )
        dominant_object_ids = tuple(
            sorted({object_by_name[name].object_id for name in ranked_objects[:16]})
        )
        source_span_ids = tuple(
            sorted({span for path in group for span in path.source_span_refs})
        )
        protected_neighbor_ids = tuple(
            sorted(
                {
                    item.object_id
                    for item in records
                    if item.protected
                    or item.object_id in cdc_object_ids
                    or item.semantic_name in cdc_neighbor_names
                }
            )
        )
        cell_delay = sum(path.cell_delay_ns for path in group)
        net_delay = sum(path.net_delay_ns for path in group)
        total_delay = cell_delay + net_delay
        cell_fraction = cell_delay / total_delay if total_delay > 0 else 0.5
        cell_fraction = min(max(cell_fraction, 0.0), 1.0)
        net_fraction = 1.0 - cell_fraction
        type_features = tuple(
            _type_features(tuple(object_by_name[name] for name in path.object_sequence))
            for path in group
        )
        mux_depth = max(item[0] for item in type_features)
        boolean_depth = max(item[1] for item in type_features)
        comparator_depth = max(item[2] for item in type_features)
        arithmetic_depth = max(item[3] for item in type_features)
        distinct_owner_spans = {
            (item.owner_hierarchy, span)
            for item in records
            for span in item.source_span_ids
        }
        span_occurrences = Counter(span for _, span in distinct_owner_spans)
        repeated_predicates = sum(max(count - 1, 0) for count in span_occurrences.values())
        reconvergence = sum(max(count - 1, 0) for count in object_counts.values())
        mapping_confidence = (
            sum(span_by_id[span].mapping_confidence for span in source_span_ids)
            / len(source_span_ids)
            if source_span_ids
            else 0.0
        )
        cluster_negative = sum(
            measured_tns_by_view[path.analysis_view_id]
            * max(-path.slack_ns, 0.0)
            / negative_by_view[path.analysis_view_id]
            if negative_by_view[path.analysis_view_id]
            else 0.0
            for path in group
        )
        tns_share = 100.0 * cluster_negative / negative_total if negative_total else 0.0
        features = PathFeatureVector(
            worst_slack_ns=worst.slack_ns,
            tns_share_percent=min(tns_share, 100.0),
            affected_endpoints=len({path.endpoint for path in group}),
            cell_delay_fraction=cell_fraction,
            net_delay_fraction=net_fraction,
            logic_depth=max(path.logic_depth for path in group),
            max_fanout=max(path.max_fanout for path in group),
            mux_depth=mux_depth,
            boolean_depth=boolean_depth,
            comparator_depth=comparator_depth,
            arithmetic_depth=arithmetic_depth,
            repeated_predicate_count=repeated_predicates,
            reconvergence_count=reconvergence,
            physical_dominance=net_fraction,
            source_mapping_confidence=mapping_confidence,
            protection_distance=0 if protected_neighbor_ids else None,
            estimated_proof_cost=min(
                1.0,
                0.1 + max(path.logic_depth for path in group) / 50.0 + len(group) / 100.0,
            ),
        )
        root_causes = _root_causes(features, records)
        path_ids = tuple(path.path_id for path in group)
        cluster_id = "cluster_" + canonical_sha256(
            {
                "evidence_snapshot_hash": evidence_snapshot_hash,
                "path_ids": path_ids,
                "target_domain": domain_by_path[worst.path_id],
            }
        ).removeprefix("sha256:")[:24]
        payload = {
            "cluster_id": cluster_id,
            "candidate_id": source_map.candidate_id,
            "evidence_snapshot_hash": evidence_snapshot_hash,
            "target_domain": domain_by_path[worst.path_id],
            "worst_analysis_view_id": worst.analysis_view_id,
            "path_ids": path_ids,
            "analysis_view_ids": tuple(
                sorted({path.analysis_view_id for path in group})
            ),
            "dominant_object_ids": dominant_object_ids,
            "source_span_ids": source_span_ids,
            "protected_neighbor_ids": protected_neighbor_ids,
            "features": features.model_dump(mode="json"),
            "root_causes": tuple(item.model_dump(mode="json") for item in root_causes),
        }
        clusters.append(PathCluster(**payload, cluster_hash=canonical_sha256(payload)))
    return tuple(
        sorted(
            clusters,
            key=lambda item: (item.features.worst_slack_ns, item.cluster_id),
        )
    )


__all__ = [
    "PathClusteringError",
    "ROOT_CAUSE_CATEGORIES",
    "cluster_paths",
]
