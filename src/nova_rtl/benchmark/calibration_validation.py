"""Post-synthesis structural checks for the selected full calibration sample."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nova_rtl.contracts.benchmark import BenchmarkConfig

EXPECTED_CDC_INSTANCES = (
    "u_compute_to_control",
    "u_dma_to_control",
    "u_ingress_to_compute_fifo",
    "u_ingress_to_control",
    "u_ingress_to_schedule_pulse",
    "u_reset_compute",
    "u_reset_control",
    "u_reset_dma",
    "u_reset_ingress",
    "u_reset_schedule",
    "u_schedule_to_control",
    "u_schedule_to_dma_level",
)
_SUBSYSTEM_TYPES = {
    "ingress": "ingress_subsystem",
    "schedule": "scheduler_subsystem",
    "dma": "dma_subsystem",
    "compute": "compute_subsystem",
    "control": "control_subsystem",
}


@dataclass(frozen=True)
class MappedStructureEvidence:
    master_clock_count: int
    generated_clock_count: int
    active_consumer_count: int
    cdc_instance_count: int
    challenge_family_ids: tuple[str, ...]
    challenge_lane_count: int
    findings: tuple[str, ...]


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bits(value: object) -> tuple[object, ...]:
    raw = _as_mapping(value).get("bits", ())
    return tuple(raw) if isinstance(raw, list | tuple) else ()


def _binary_parameter(module: Mapping[str, Any], name: str) -> int | None:
    value = _as_mapping(module.get("parameter_default_values")).get(name)
    if not isinstance(value, str) or not value or set(value) - {"0", "1"}:
        return None
    return int(value, 2)


def inspect_full_mapped_design(
    mapped_design: Mapping[str, Any],
    config: BenchmarkConfig,
) -> MappedStructureEvidence:
    """Check the fixed five-domain, 105-clock structure in Yosys JSON."""

    findings: list[str] = []
    modules = _as_mapping(mapped_design.get("modules"))
    top = _as_mapping(modules.get(config.top_module))
    ports = _as_mapping(top.get("ports"))
    netnames = _as_mapping(top.get("netnames"))
    top_cells = _as_mapping(top.get("cells"))

    master_count = 0
    generated_count = 0
    for domain in config.master_domains:
        port = _as_mapping(ports.get(domain.master_clock_id))
        if port.get("direction") == "input" and len(_bits(port)) == 1:
            master_count += 1
        else:
            findings.append(f"master clock is missing after synthesis: {domain.master_clock_id}")
        bus_name = f"{domain.domain_id}_generated_clocks"
        width = len(_bits(netnames.get(bus_name)))
        generated_count += width
        if width != config.generated_clocks_per_master:
            findings.append(
                "generated clock bus width mismatch: "
                f"{domain.domain_id}={width}, expected={config.generated_clocks_per_master}"
            )

    divider_modules = tuple(
        _as_mapping(module)
        for name, module in modules.items()
        if "clock_divider_bank" in str(name)
    )
    divider_cells = {
        str(name)
        for module in divider_modules
        for name in _as_mapping(module.get("cells"))
        if str(name).endswith(".u_divider")
    }
    divider_banks = tuple(
        name for name in top_cells if str(name).endswith("_dividers")
    )
    if len(divider_banks) != 5:
        findings.append(f"post-synthesis divider bank count is {len(divider_banks)}, expected 5")
    if len(divider_cells) != config.generated_clocks_per_master:
        findings.append(
            "post-synthesis divider ratio count is "
            f"{len(divider_cells)}, expected {config.generated_clocks_per_master}"
        )

    domain_modules = {
        str(name): _as_mapping(module)
        for name, module in modules.items()
        if "benchmark_domain" in str(name) or str(name).startswith("domain_family_")
    }
    active_consumers = 0
    lane_count = 0
    family_ids: set[str] = set()
    for module in domain_modules.values():
        cells = _as_mapping(module.get("cells"))
        consumers = sum("gen_consumers[" in str(name) for name in cells)
        lanes = sum("gen_lanes[" in str(name) for name in cells)
        active_consumers += consumers
        lane_count += lanes
        family = _binary_parameter(module, "OPPORTUNITY_FAMILY")
        scale = _binary_parameter(module, "WORKLOAD_SCALE")
        clocks = _binary_parameter(module, "GENERATED_CLOCKS")
        if family is not None:
            family_ids.add(f"family_{family}")
        if scale != config.workload_scale:
            findings.append(
                f"challenge domain workload scale is {scale}, expected {config.workload_scale}"
            )
        if clocks != config.generated_clocks_per_master:
            findings.append(
                f"challenge domain generated-clock count is {clocks}, "
                f"expected {config.generated_clocks_per_master}"
            )
        checksum = _as_mapping(_as_mapping(module.get("ports")).get("checksum"))
        if checksum.get("direction") != "output" or not _bits(checksum):
            findings.append("challenge domain checksum is not observable after synthesis")

    expected_consumers = len(config.master_domains) * config.generated_clocks_per_master
    if active_consumers != expected_consumers:
        findings.append(
            f"active generated-clock consumer count is {active_consumers}, "
            f"expected {expected_consumers}"
        )
    expected_lanes = len(config.master_domains) * config.workload_scale
    if lane_count != expected_lanes:
        findings.append(
            f"reachable challenge lane count is {lane_count}, expected {expected_lanes}"
        )
    expected_families = {f"family_{index}" for index in range(5)}
    if family_ids != expected_families:
        findings.append(
            "editable timing family inventory differs: "
            f"observed={','.join(sorted(family_ids))}"
        )

    for domain in config.master_domains:
        subsystem_type = _SUBSYSTEM_TYPES[domain.domain_id]
        subsystem = _as_mapping(modules.get(subsystem_type))
        domain_cell = _as_mapping(_as_mapping(subsystem.get("cells")).get("u_domain"))
        if str(domain_cell.get("type", "")) not in domain_modules:
            findings.append(f"challenge domain is not retained by {subsystem_type}")
            continue
        output_bits = _bits(_as_mapping(subsystem.get("ports")).get("checksum"))
        connected_bits = tuple(_as_mapping(domain_cell.get("connections")).get("checksum", ()))
        if not output_bits or connected_bits != output_bits:
            findings.append(f"challenge checksum is not connected through {subsystem_type}")
        if not _bits(netnames.get(f"{domain.domain_id}_checksum")):
            findings.append(f"top-level checksum path is missing for {domain.domain_id}")
    benchmark_checksum = _as_mapping(ports.get("benchmark_checksum"))
    if benchmark_checksum.get("direction") != "output" or not _bits(benchmark_checksum):
        findings.append("observable benchmark checksum output is missing")

    observed_cdc = tuple(sorted(set(EXPECTED_CDC_INSTANCES) & set(top_cells)))
    missing_cdc = tuple(sorted(set(EXPECTED_CDC_INSTANCES) - set(observed_cdc)))
    unexpected_cdc = tuple(
        sorted(
            name
            for name in top_cells
            if (str(name).startswith("u_reset_") or "_to_" in str(name))
            and name not in EXPECTED_CDC_INSTANCES
        )
    )
    if missing_cdc:
        findings.append(f"approved CDC instances missing: {','.join(missing_cdc)}")
    if unexpected_cdc:
        findings.append(f"unexpected CDC instances present: {','.join(unexpected_cdc)}")

    return MappedStructureEvidence(
        master_clock_count=master_count,
        generated_clock_count=generated_count,
        active_consumer_count=active_consumers,
        cdc_instance_count=len(observed_cdc),
        challenge_family_ids=tuple(sorted(family_ids)),
        challenge_lane_count=lane_count,
        findings=tuple(sorted(set(findings))),
    )


__all__ = [
    "EXPECTED_CDC_INSTANCES",
    "MappedStructureEvidence",
    "inspect_full_mapped_design",
]
