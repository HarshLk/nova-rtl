from __future__ import annotations

from copy import deepcopy

from nova_rtl.benchmark.calibration_validation import inspect_full_mapped_design
from nova_rtl.contracts.benchmark import BenchmarkConfig


def _mapped_design() -> dict[str, object]:
    domains = ("ingress", "schedule", "dma", "compute", "control")
    subsystem_types = {
        "ingress": "ingress_subsystem",
        "schedule": "scheduler_subsystem",
        "dma": "dma_subsystem",
        "compute": "compute_subsystem",
        "control": "control_subsystem",
    }
    modules: dict[str, object] = {}
    top_cells: dict[str, object] = {}
    for family, domain in enumerate(domains):
        domain_type = f"domain_family_{family}"
        modules[domain_type] = {
            "parameter_default_values": {
                "OPPORTUNITY_FAMILY": f"{family:032b}",
                "WORKLOAD_SCALE": f"{4:032b}",
                "GENERATED_CLOCKS": f"{21:032b}",
            },
            "ports": {"checksum": {"direction": "output", "bits": list(range(32))}},
            "cells": {
                **{f"gen_consumers[{index}].u_consumer": {} for index in range(21)},
                **{f"gen_lanes[{index}].u_lane": {} for index in range(4)},
            },
        }
        subsystem = subsystem_types[domain]
        modules[subsystem] = {
            "ports": {"checksum": {"direction": "output", "bits": list(range(32))}},
            "cells": {
                "u_domain": {
                    "type": domain_type,
                    "connections": {"checksum": list(range(32))},
                }
            },
        }
        top_cells[f"u_{domain}"] = {"type": subsystem}
        top_cells[f"u_{domain}_dividers"] = {"type": "full_clock_divider_bank"}
    cdc_instances = (
        "u_reset_compute",
        "u_reset_control",
        "u_reset_dma",
        "u_reset_ingress",
        "u_reset_schedule",
        "u_ingress_to_schedule_pulse",
        "u_schedule_to_dma_level",
        "u_ingress_to_compute_fifo",
        "u_compute_to_control",
        "u_dma_to_control",
        "u_ingress_to_control",
        "u_schedule_to_control",
    )
    top_cells.update({name: {"type": "cdc_approved"} for name in cdc_instances})
    modules["full_clock_divider_bank"] = {
        "cells": {f"gen_div_{index}.u_divider": {} for index in range(21)}
    }
    modules["nebula_top"] = {
        "ports": {
            **{f"clk_{domain}": {"direction": "input", "bits": [family + 1]}
               for family, domain in enumerate(domains)},
            "benchmark_checksum": {"direction": "output", "bits": list(range(100, 132))},
        },
        "netnames": {
            **{f"{domain}_generated_clocks": {"bits": list(range(21))}
               for domain in domains},
            **{f"{domain}_checksum": {"bits": list(range(32))} for domain in domains},
        },
        "cells": top_cells,
    }
    return {"modules": modules}


def test_full_mapped_structure_retains_required_topology_and_reachable_families() -> None:
    evidence = inspect_full_mapped_design(_mapped_design(), BenchmarkConfig.default())

    assert evidence.findings == ()
    assert evidence.master_clock_count == 5
    assert evidence.generated_clock_count == 105
    assert evidence.active_consumer_count == 105
    assert evidence.cdc_instance_count == 12
    assert evidence.challenge_family_ids == (
        "family_0",
        "family_1",
        "family_2",
        "family_3",
        "family_4",
    )
    assert evidence.challenge_lane_count == 20


def test_full_mapped_structure_fails_closed_when_a_generated_clock_is_lost() -> None:
    mapped = deepcopy(_mapped_design())
    mapped["modules"]["nebula_top"]["netnames"]["compute_generated_clocks"]["bits"].pop()

    evidence = inspect_full_mapped_design(mapped, BenchmarkConfig.default())

    assert "generated clock bus width mismatch: compute=20, expected=21" in evidence.findings
