"""Validation of benchmark snapshots against explicit topology expectations."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from nova_rtl.benchmark.constraints import load_constraint_contract
from nova_rtl.benchmark.formal import load_formal_manifest
from nova_rtl.benchmark.power import load_power_workload
from nova_rtl.contracts.analysis import CDCInventory, ClockInventory
from nova_rtl.contracts.base import canonical_sha256
from nova_rtl.contracts.benchmark import (
    BenchmarkExpectations,
    BenchmarkSnapshot,
    BenchmarkValidation,
    BenchmarkValidationEvidence,
)


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def validate_benchmark(
    snapshot: BenchmarkSnapshot,
    expectations: BenchmarkExpectations,
    *,
    artifact_root: Path | None = None,
    evidence: BenchmarkValidationEvidence | None = None,
) -> BenchmarkValidation:
    """Validate topology and, when supplied, exact generated artifacts and elaboration."""

    mismatches: list[str] = []
    if snapshot.expectations.expectations_hash != expectations.expectations_hash:
        mismatches.append("snapshot expectations hash differs from requested expectations")
    if snapshot.expected_master_clocks != expectations.expected_master_clocks:
        mismatches.append("master clock count differs from requested expectations")
    if snapshot.expected_generated_per_master != expectations.expected_generated_per_master:
        mismatches.append("generated clocks per master differ from requested expectations")
    if snapshot.expected_generated_total != expectations.expected_generated_total:
        mismatches.append("generated clock total differs from requested expectations")
    without_consumers = tuple(
        item.clock_id for item in expectations.generated_clocks if not item.active_consumers
    )
    artifact_mismatches: list[str] = []
    clock_mismatches: list[str] = []
    cdc_mismatches: list[str] = []
    unreachable: list[str] = []
    unexpected_unconstrained: list[str] = []
    if artifact_root is not None:
        identities: dict[str, str] = {}
        for item in snapshot.generated_files:
            path = artifact_root / item.relative_path
            if not path.is_file():
                artifact_mismatches.append(item.relative_path)
                continue
            content = path.read_bytes()
            actual_hash = _hash_bytes(content)
            identities[item.relative_path] = actual_hash
            if actual_hash != item.sha256 or len(content) != item.size_bytes:
                artifact_mismatches.append(item.relative_path)

        rtl_identities = {
            path: digest for path, digest in sorted(identities.items()) if path.startswith("rtl/")
        }
        if canonical_sha256(rtl_identities) != snapshot.source_hash:
            artifact_mismatches.append("<rtl-source-identity>")

        try:
            clock_inventory = ClockInventory.model_validate_json(
                (artifact_root / "expected/clock_inventory.json").read_text(encoding="utf-8")
            )
            if clock_inventory.clock_graph_hash != snapshot.clock_inventory_hash:
                clock_mismatches.append("clock inventory hash differs from snapshot")
            if tuple(item.clock_id for item in clock_inventory.master_clocks) != (
                expectations.master_clock_ids
            ):
                clock_mismatches.append("master clock inventory differs from expectations")
            expected_lineage = {
                (item.master_clock_id, item.clock_id) for item in expectations.generated_clocks
            }
            actual_lineage = {
                (item.parent_clock_id, item.child_clock_id)
                for item in clock_inventory.lineage_edges
            }
            if actual_lineage != expected_lineage:
                clock_mismatches.append("generated clock lineage differs from expectations")
            inactive_inventory = tuple(
                item.clock_id
                for item in clock_inventory.generated_clocks
                if item.active_consumer_count <= 0
            )
            without_consumers = tuple(sorted(set(without_consumers) | set(inactive_inventory)))
        except (OSError, ValueError) as error:
            clock_mismatches.append(f"invalid clock inventory: {error}")

        try:
            cdc_inventory = CDCInventory.model_validate_json(
                (artifact_root / "expected/cdc_inventory.json").read_text(encoding="utf-8")
            )
            if cdc_inventory.inventory_hash != snapshot.cdc_inventory_hash:
                cdc_mismatches.append("CDC inventory hash differs from snapshot")
            if any(item.status != "APPROVED" for item in cdc_inventory.crossings):
                cdc_mismatches.append("CDC inventory contains a non-approved crossing")
            if any(
                (
                    cdc_inventory.new_unapproved_count,
                    cdc_inventory.changed_approved_structure_count,
                    cdc_inventory.removed_approved_structure_count,
                    cdc_inventory.ambiguous_count,
                )
            ):
                cdc_mismatches.append("CDC comparison counts are nonzero")
        except (OSError, ValueError) as error:
            cdc_mismatches.append(f"invalid CDC inventory: {error}")

        try:
            constraint = load_constraint_contract(
                artifact_root / "expected/constraint_contract.json"
            )
            sdc_hash = _hash_bytes((artifact_root / "constraints/nebula.sdc").read_bytes())
            if constraint.contract_hash != snapshot.constraint_contract_hash:
                clock_mismatches.append("constraint contract hash differs from snapshot")
            if constraint.sdc_hash != sdc_hash:
                clock_mismatches.append("SDC hash differs from constraint contract")
            if tuple(item.clock_id for item in constraint.generated_clocks) != tuple(
                item.clock_id for item in expectations.generated_clocks
            ):
                clock_mismatches.append("constraint generated clocks differ from expectations")
            if constraint.expected_unconstrained_endpoints != 0:
                unexpected_unconstrained.append("constraint_contract")
        except (OSError, ValueError) as error:
            clock_mismatches.append(f"invalid constraint contract: {error}")

        try:
            formal = load_formal_manifest(artifact_root / "formal/harnesses.json")
            if formal.manifest_hash != snapshot.formal_manifest_hash:
                artifact_mismatches.append("formal/harnesses.json#manifest_hash")
            source_identities = {
                path: _hash_bytes((artifact_root / path).read_bytes())
                for path in formal.source_files
            }
            if canonical_sha256(source_identities) != formal.source_hash:
                artifact_mismatches.append("<formal-source-identity>")
        except (OSError, ValueError) as error:
            artifact_mismatches.append(f"<invalid-formal-manifest:{error}>")

        try:
            workload = load_power_workload(artifact_root / "sim/power_workload.yaml")
            if workload.workload_hash != snapshot.power_workload_hash:
                artifact_mismatches.append("sim/power_workload.yaml#workload_hash")
        except (OSError, ValueError) as error:
            artifact_mismatches.append(f"<invalid-power-workload:{error}>")

        timing_source = artifact_root / "rtl/workload/timing_opportunity_lane.sv"
        top_source = artifact_root / "rtl/nebula_top.sv"
        if not timing_source.is_file() or "timing_opportunity_lane" not in timing_source.read_text(
            encoding="utf-8"
        ):
            unreachable.append("timing_opportunity_lane")
        if not top_source.is_file() or "benchmark_checksum <=" not in top_source.read_text(
            encoding="utf-8"
        ):
            unreachable.append("benchmark_checksum_observable_path")

    if evidence is not None:
        if evidence.constraint_contract_hash != snapshot.constraint_contract_hash:
            clock_mismatches.append("elaboration evidence uses a different constraint contract")
        clock_mismatches.extend(evidence.unresolved_selectors)
        without_consumers = tuple(
            sorted(set(without_consumers) | set(evidence.generated_clocks_without_consumers))
        )
        unexpected_unconstrained.extend(evidence.unexpected_unconstrained_endpoints)
        unreachable.extend(evidence.challenge_logic_without_observable_path)

    artifact_mismatches = sorted(set(artifact_mismatches))
    clock_mismatches = sorted(set(clock_mismatches))
    cdc_mismatches = sorted(set(cdc_mismatches))
    unreachable = sorted(set(unreachable))
    unexpected_unconstrained = sorted(set(unexpected_unconstrained))
    passes = not any(
        (
            mismatches,
            without_consumers,
            artifact_mismatches,
            clock_mismatches,
            cdc_mismatches,
            unreachable,
            unexpected_unconstrained,
        )
    )
    payload = {
        "schema_version": 1,
        "snapshot_hash": snapshot.snapshot_hash,
        "expectations_hash": expectations.expectations_hash,
        "status": "PASS" if passes else "FAIL",
        "expectation_mismatches": tuple(mismatches),
        "generated_without_consumers": without_consumers,
        "artifact_identity_mismatches": tuple(artifact_mismatches),
        "clock_lineage_mismatches": tuple(clock_mismatches),
        "cdc_inventory_mismatches": tuple(cdc_mismatches),
        "unreachable_challenge_logic": tuple(unreachable),
        "unexpected_unconstrained_endpoints": tuple(unexpected_unconstrained),
    }
    return BenchmarkValidation(
        **payload,
        validation_hash=canonical_sha256(payload),
    )


__all__ = ["validate_benchmark"]
