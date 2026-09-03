"""Deterministic benchmark SDC rendering and semantic binding validation."""

from __future__ import annotations

import json
from pathlib import Path

from nova_rtl.contracts.base import canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.benchmark import (
    BenchmarkConfig,
    BenchmarkConstraintContract,
    BenchmarkExpectations,
    BenchmarkValidationEvidence,
    GeneratedClockConstraint,
    MasterClockConstraint,
    ReviewedConstraintException,
)

_MASTER_PERIODS_NS = {
    "clk_ingress": 10.0,
    "clk_schedule": 14.0,
    "clk_dma": 18.0,
    "clk_compute": 22.0,
    "clk_control": 26.0,
}
_FULL_CHALLENGE_PERIOD_NS = 5.0


def master_periods(config: BenchmarkConfig) -> dict[str, float]:
    """Return profile-specific master periods; full intentionally seeds setup pressure."""

    if config.profile == "full":
        return {clock_id: _FULL_CHALLENGE_PERIOD_NS for clock_id in _MASTER_PERIODS_NS}
    return dict(_MASTER_PERIODS_NS)


def _hash_bytes(content: bytes) -> str:
    from hashlib import sha256

    return f"sha256:{sha256(content).hexdigest()}"


def _subsystem_instance(domain_id: str) -> str:
    return "u_schedule" if domain_id == "schedule" else f"u_{domain_id}"


def _divider_bank_instance(domain_id: str) -> str:
    return f"u_{domain_id}_dividers"


def _clock_constraints(
    config: BenchmarkConfig,
) -> tuple[tuple[MasterClockConstraint, ...], tuple[GeneratedClockConstraint, ...]]:
    periods = master_periods(config)
    masters = tuple(
        MasterClockConstraint(
            clock_id=domain.master_clock_id,
            domain_id=domain.domain_id,
            source_selector=f"[get_ports {domain.master_clock_id}]",
            period_ns=periods[domain.master_clock_id],
            waveform_ns=(0.0, periods[domain.master_clock_id] / 2.0),
        )
        for domain in config.master_domains
    )
    generated: list[GeneratedClockConstraint] = []
    for domain in config.master_domains:
        for index, ratio in enumerate(config.divider_ratios):
            clock_id = f"gclk_{domain.domain_id}_div_{ratio:03d}"
            edge_indices = () if ratio % 2 == 0 else (1, ratio + 2, (2 * ratio) + 1)
            generated.append(
                GeneratedClockConstraint(
                    clock_id=clock_id,
                    domain_id=domain.domain_id,
                    master_clock_id=domain.master_clock_id,
                    source_selector=f"[get_ports {domain.master_clock_id}]",
                    target_selector=(
                        f"[get_nets {domain.domain_id}_generated_clocks[{index}]]"
                    ),
                    divide_by=ratio,
                    waveform_mode=("EVEN_DIVIDE_BY" if ratio % 2 == 0 else "ODD_EDGE_LIST"),
                    edge_indices=edge_indices,
                )
            )
    return masters, tuple(generated)


def _render_sdc_text(
    masters: tuple[MasterClockConstraint, ...],
    generated: tuple[GeneratedClockConstraint, ...],
) -> str:
    lines = [
        "# NOVA benchmark constraints; generated deterministically from benchmark configuration.",
        "# Stable selectors are mirrored in expected/constraint_contract.json.",
        "",
    ]
    for clock in masters:
        lines.append(
            f"create_clock -name {clock.clock_id} -period {clock.period_ns:g} "
            f"-waveform {{{clock.waveform_ns[0]:g} {clock.waveform_ns[1]:g}}} "
            f"{clock.source_selector}"
        )
    lines.extend(("", "# The five master domains are mutually asynchronous."))
    groups = " ".join(
        f"-group [get_clocks {clock.clock_id}]" for clock in masters
    )
    lines.append(f"set_clock_groups -asynchronous {groups}")
    lines.extend(("", "# Generated clocks preserve divider waveform semantics."))
    for clock in generated:
        if clock.waveform_mode == "EVEN_DIVIDE_BY":
            waveform = f"-divide_by {clock.divide_by}"
        else:
            waveform = "-edges {" + " ".join(str(edge) for edge in clock.edge_indices) + "}"
        lines.append(
            f"create_generated_clock -name {clock.clock_id} "
            f"-source {clock.source_selector} {waveform} {clock.target_selector}"
        )
    master_names = " ".join(clock.clock_id for clock in masters)
    generated_names = " ".join(clock.clock_id for clock in generated)
    lines.extend(
        (
            "",
            "# Reviewed clock and I/O margins.",
            f"set_clock_uncertainty -setup 0.10 "
            f"[get_clocks {{{master_names} {generated_names}}}]",
            f"set_clock_uncertainty -hold 0.01 "
            f"[get_clocks {{{master_names} {generated_names}}}]",
            "set_input_transition 0.05 [get_ports rst_n]",
            "set_input_delay -clock clk_ingress 0.20 [get_ports workload_enable]",
            "set_input_delay -clock clk_ingress 0.20 [get_ports {workload_phase[*]}]",
            "set_input_delay -clock clk_ingress 0.20 [get_ports {workload_data[*]}]",
            "set_output_delay -clock clk_control 0.50 [get_ports benchmark_active]",
            "set_output_delay -clock clk_control 0.50 [get_ports {benchmark_checksum[*]}]",
            "set_load 0.02 [get_ports {benchmark_active benchmark_checksum[*]}]",
            "",
            "# Reviewed exception: asynchronous reset assertion only; "
            "local release is synchronized.",
            "set_false_path -from [get_ports rst_n] -to [all_registers]",
        )
    )
    lines.extend(
        (
            "",
            "# Reviewed generated-to-master CDC and divider-feedback exceptions.",
        )
    )
    for exception in _reviewed_exceptions(masters, generated)[1:]:
        lines.append(
            f"set_false_path -from {exception.from_selector} -to {exception.to_selector}"
        )
    lines.append("")
    return "\n".join(lines)


def _reviewed_exceptions(
    masters: tuple[MasterClockConstraint, ...],
    generated: tuple[GeneratedClockConstraint, ...],
) -> tuple[ReviewedConstraintException, ...]:
    reset = ReviewedConstraintException(
        exception_id="async_reset_assertion",
        exception_type="FALSE_PATH",
        from_selector="[get_ports rst_n]",
        to_selector="[all_registers]",
        rationale=(
            "Only asynchronous reset assertion is exempt; reset release is synchronized "
            "inside every master domain."
        ),
    )
    crossings = tuple(
        ReviewedConstraintException(
            exception_id=f"generated_to_master_{master.domain_id}",
            exception_type="FALSE_PATH",
            from_selector=(
                "[get_clocks {"
                + " ".join(
                    clock.clock_id
                    for clock in generated
                    if clock.master_clock_id == master.clock_id
                )
                + "}]"
            ),
            to_selector=f"[get_clocks {master.clock_id}]",
            rationale=(
                "Generated-domain return traffic enters the master domain only through "
                "approved synchronizers; this direction also contains protected divider "
                "feedback whose generated-clock observation point is not a data launch."
            ),
        )
        for master in masters
    )
    return (reset, *crossings)


def render_constraints(
    config: BenchmarkConfig,
    expectations: BenchmarkExpectations,
) -> tuple[bytes, BenchmarkConstraintContract]:
    """Render the profile-specific SDC and its strict semantic contract."""

    masters, generated = _clock_constraints(config)
    expected_ids = tuple(item.clock_id for item in expectations.generated_clocks)
    if tuple(item.clock_id for item in generated) != expected_ids:
        raise ValueError("constraint clock identities differ from benchmark expectations")
    sdc = _render_sdc_text(masters, generated).encode("utf-8")
    payload = {
        "schema_version": 1,
        "top_module": config.top_module,
        "master_clocks": masters,
        "generated_clocks": generated,
        "asynchronous_master_groups": tuple((item.clock_id,) for item in masters),
        "setup_clock_uncertainty_ns": 0.1,
        "hold_clock_uncertainty_ns": 0.01,
        "input_delay_ns": 0.2,
        "input_delay_clock_id": "clk_ingress",
        "input_delay_selectors": (
            "[get_ports workload_enable]",
            "[get_ports {workload_phase[*]}]",
            "[get_ports {workload_data[*]}]",
        ),
        "output_delay_ns": 0.5,
        "output_delay_clock_id": "clk_control",
        "output_delay_selectors": (
            "[get_ports benchmark_active]",
            "[get_ports {benchmark_checksum[*]}]",
        ),
        "reviewed_exceptions": _reviewed_exceptions(masters, generated),
        "expected_unconstrained_endpoints": 0,
        "sdc_hash": _hash_bytes(sdc),
    }
    hash_payload = {
        **payload,
        "master_clocks": tuple(item.model_dump(mode="json") for item in masters),
        "generated_clocks": tuple(item.model_dump(mode="json") for item in generated),
        "reviewed_exceptions": tuple(
            item.model_dump(mode="json") for item in payload["reviewed_exceptions"]
        ),
    }
    return sdc, BenchmarkConstraintContract(
        **payload,
        contract_hash=canonical_sha256(hash_payload),
    )


def constraint_contract_bytes(contract: BenchmarkConstraintContract) -> bytes:
    return canonical_json_bytes(contract) + b"\n"


def load_constraint_contract(path: Path) -> BenchmarkConstraintContract:
    """Load and validate one generated constraint contract."""

    return BenchmarkConstraintContract.model_validate_json(path.read_text(encoding="utf-8"))


def _normalized(value: object) -> str:
    return str(value).replace("\\", "")


def _selector_resolves(
    selector: str,
    *,
    top: dict[str, object],
    modules: dict[str, dict[str, object]],
    clock_ids: frozenset[str],
) -> bool:
    if selector.startswith("[get_ports "):
        port_name = selector.removeprefix("[get_ports ").removesuffix("]")
        port_name = port_name.removeprefix("{").removesuffix("}")
        port_name = port_name.removesuffix("[*]")
        return port_name in top.get("ports", {})
    if selector == "[all_registers]":
        return any(
            _normalized(cell.get("type", "")).startswith("$adff")
            or _normalized(cell.get("type", "")).startswith("$dff")
            for module in modules.values()
            for cell in module.get("cells", {}).values()
        )
    if selector.startswith("[get_nets "):
        net_name = selector.removeprefix("[get_nets ").removesuffix("]")
        base_name = net_name.split("[", maxsplit=1)[0]
        return base_name in top.get("netnames", {})
    if selector.startswith("[get_clocks "):
        clock_names = selector.removeprefix("[get_clocks ").removesuffix("]")
        clock_names = clock_names.removeprefix("{").removesuffix("}").split()
        return bool(clock_names) and set(clock_names) <= clock_ids
    if not selector.startswith("[get_pins "):
        return False
    hierarchy = selector.removeprefix("[get_pins ").removesuffix("]")
    bank_name, generated_divider, pin_name = hierarchy.split("/", maxsplit=2)
    bank_cell = top.get("cells", {}).get(bank_name)
    if bank_cell is None:
        return False
    bank_type = _normalized(bank_cell.get("type", ""))
    try:
        bank_module = next(
            module for name, module in modules.items() if _normalized(name) == bank_type
        )
    except StopIteration:
        return False
    wanted_cell = generated_divider
    divider_cell = next(
        (
            cell
            for name, cell in bank_module.get("cells", {}).items()
            if _normalized(name).replace("/", ".") == wanted_cell
        ),
        None,
    )
    return divider_cell is not None and pin_name in divider_cell.get("connections", {})


def resolve_constraint_bindings(
    contract_path: Path,
    elaborated_json_path: Path,
) -> BenchmarkValidationEvidence:
    """Resolve stable SDC selectors and observable benchmark structure in Yosys JSON."""

    contract = load_constraint_contract(contract_path)
    design = json.loads(elaborated_json_path.read_text(encoding="utf-8"))
    modules: dict[str, dict[str, object]] = design["modules"]
    top: dict[str, object] = modules[contract.top_module]
    selectors = {
        item.source_selector for item in contract.master_clocks
    } | {
        selector
        for item in contract.generated_clocks
        for selector in (item.source_selector, item.target_selector)
    } | {
        selector
        for item in contract.reviewed_exceptions
        for selector in (item.from_selector, item.to_selector)
    } | set(contract.input_delay_selectors) | set(contract.output_delay_selectors)
    unresolved = tuple(
        selector
        for selector in sorted(selectors)
        if not _selector_resolves(
            selector,
            top=top,
            modules=modules,
            clock_ids=frozenset(
                item.clock_id
                for item in (*contract.master_clocks, *contract.generated_clocks)
            ),
        )
    )

    expected_per_domain = len(contract.generated_clocks) // 5
    consumer_modules = {
        name: module
        for name, module in modules.items()
        if "benchmark_domain" in _normalized(name)
    }
    consumer_count_is_present = any(
        sum(
            "generated_clock_consumer" in _normalized(cell.get("type", ""))
            for cell in module.get("cells", {}).values()
        )
        == expected_per_domain
        for module in consumer_modules.values()
    )
    without_consumers = () if consumer_count_is_present else tuple(
        item.clock_id for item in contract.generated_clocks
    )

    top_ports = top.get("ports", {})
    expected_ports = {
        *(item.clock_id for item in contract.master_clocks),
        "rst_n",
        "benchmark_active",
        "benchmark_checksum",
        "workload_enable",
        "workload_phase",
        "workload_data",
    }
    unconstrained = tuple(sorted(set(top_ports) - expected_ports))
    checksum_port = top_ports.get("benchmark_checksum", {})
    challenge_cells = sum(
        "timing_opportunity_lane" in _normalized(cell.get("type", ""))
        for module in modules.values()
        for cell in module.get("cells", {}).values()
    )
    challenge_findings = (
        ()
        if checksum_port.get("direction") == "output" and challenge_cells > 0
        else ("timing_opportunity_lanes_to_benchmark_checksum",)
    )
    payload = {
        "schema_version": 1,
        "constraint_contract_hash": contract.contract_hash,
        "elaborated_design_hash": canonical_sha256(design),
        "unresolved_selectors": unresolved,
        "generated_clocks_without_consumers": without_consumers,
        "unexpected_unconstrained_endpoints": unconstrained,
        "challenge_logic_without_observable_path": challenge_findings,
    }
    return BenchmarkValidationEvidence(
        **payload,
        evidence_hash=canonical_sha256(payload),
    )


__all__ = [
    "constraint_contract_bytes",
    "load_constraint_contract",
    "render_constraints",
    "resolve_constraint_bindings",
]
