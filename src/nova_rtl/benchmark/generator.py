"""Deterministic, profile-driven NOVA benchmark generator."""

from __future__ import annotations

import os
import shutil
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from nova_rtl.benchmark.constraints import (
    constraint_contract_bytes,
    master_periods,
    render_constraints,
)
from nova_rtl.benchmark.formal import render_formal_assets
from nova_rtl.benchmark.power import render_power_workload
from nova_rtl.contracts.analysis import (
    CdcCrossing,
    CDCInventory,
    ClockInventory,
    ClockLineageEdge,
    GeneratedClockEntry,
    MasterClockEntry,
)
from nova_rtl.contracts.base import EntityId, StrictContract, canonical_json_bytes, canonical_sha256
from nova_rtl.contracts.benchmark import (
    BenchmarkConfig,
    BenchmarkExpectations,
    BenchmarkFile,
    BenchmarkMasterDomain,
    BenchmarkSnapshot,
    GeneratedClockExpectation,
    MappedCellTarget,
)
from nova_rtl.contracts.manifest import ProjectManifest
from nova_rtl.contracts.platform import PlatformAnalysisViews

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_TEMPLATE_ROOT = _PROJECT_ROOT / "benchmark" / "rtl"
_SNAPSHOT_FILENAME = "benchmark-snapshot.json"
_LOCKED_INPUTS = (
    "config/analysis/views.yaml",
    "config/challenge/organizer_decisions.yaml",
    "config/formal/reset_assumptions.yaml",
    "config/platform/platform.lock.yaml",
    "config/policy/cdc_patterns.yaml",
    "config/policy/default.yaml",
)


class BenchmarkProfile(StrictContract):
    """One named scale selection in the checked-in generator manifest."""

    workload_scale: Annotated[int, Field(strict=True, gt=0)]
    generated_clocks_per_master: Annotated[int, Field(strict=True, gt=0)]
    mapped_cell_target: MappedCellTarget | None


class BenchmarkGeneratorManifest(StrictContract):
    """Profile-independent generator semantics and named scale selections."""

    schema_version: Literal[1] = 1
    template_version: EntityId
    top_module: EntityId
    master_domains: tuple[BenchmarkMasterDomain, ...]
    divider_ratios: tuple[Annotated[int, Field(strict=True)], ...]
    profiles: dict[EntityId, BenchmarkProfile]

    @model_validator(mode="after")
    def manifest_is_well_formed(self) -> Self:
        if len(self.master_domains) != 5:
            raise ValueError("generator manifest must contain exactly five master domains")
        if not self.profiles:
            raise ValueError("generator manifest must define at least one profile")
        if len(self.divider_ratios) != len(set(self.divider_ratios)):
            raise ValueError("generator divider ratios must be unique")
        if any(ratio <= 1 for ratio in self.divider_ratios):
            raise ValueError("generator divider ratios must be greater than one")
        for name, profile in self.profiles.items():
            if profile.generated_clocks_per_master > len(self.divider_ratios):
                raise ValueError(f"profile {name} requests more divider ratios than are defined")
        return self


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _hash_file_map(files: dict[str, bytes]) -> str:
    identities = {path: _hash_bytes(content) for path, content in sorted(files.items())}
    return canonical_sha256(identities)


def _load_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_benchmark_config(path: Path, profile: str) -> BenchmarkConfig:
    """Resolve one named profile into a strict, self-contained generator configuration."""

    manifest = BenchmarkGeneratorManifest.model_validate(_load_yaml(path))
    try:
        selected = manifest.profiles[profile]
    except KeyError as error:
        choices = ", ".join(sorted(manifest.profiles))
        raise ValueError(
            f"unknown benchmark profile {profile!r}; expected one of: {choices}"
        ) from error
    count = selected.generated_clocks_per_master
    return BenchmarkConfig(
        profile=profile,
        template_version=manifest.template_version,
        top_module=manifest.top_module,
        workload_scale=selected.workload_scale,
        master_domains=manifest.master_domains,
        generated_clocks_per_master=count,
        divider_ratios=manifest.divider_ratios[:count],
        mapped_cell_target=selected.mapped_cell_target,
    )


def _read_templates(template_root: Path) -> dict[str, bytes]:
    if not template_root.is_dir():
        raise FileNotFoundError(f"benchmark template directory does not exist: {template_root}")
    files = {
        f"rtl/{path.relative_to(template_root).as_posix()}": path.read_bytes()
        for path in sorted(template_root.rglob("*"))
        if path.is_file() and path.name != ".gitkeep"
    }
    if not files:
        raise ValueError(f"benchmark template directory contains no source files: {template_root}")
    return files


def _render_parameters(config: BenchmarkConfig) -> bytes:
    lines = [
        "`ifndef NOVA_BENCHMARK_PARAMETERS_SVH",
        "`define NOVA_BENCHMARK_PARAMETERS_SVH",
        f"`define NOVA_WORKLOAD_SCALE {config.workload_scale}",
        "`define NOVA_MASTER_CLOCK_COUNT 5",
        "`define NOVA_GENERATED_CLOCKS_PER_MASTER "
        f"{config.generated_clocks_per_master}",
    ]
    lines.extend(
        f"`define NOVA_DIVIDER_RATIO_{index:02d} {ratio}"
        for index, ratio in enumerate(config.divider_ratios)
    )
    lines.extend(("`endif", ""))
    return "\n".join(lines).encode("utf-8")


def _build_expectations(config: BenchmarkConfig) -> BenchmarkExpectations:
    generated: list[GeneratedClockExpectation] = []
    for domain in config.master_domains:
        subsystem_instance = (
            "u_schedule" if domain.domain_id == "schedule" else f"u_{domain.domain_id}"
        )
        for index, ratio in enumerate(config.divider_ratios):
            clock_id = f"gclk_{domain.domain_id}_div_{ratio:03d}"
            state = {
                "template_version": config.template_version,
                "domain_id": domain.domain_id,
                "master_clock_id": domain.master_clock_id,
                "divider_ratio": ratio,
                "waveform": ("EVEN_50_PERCENT" if ratio % 2 == 0 else "ODD_ALTERNATING_DUTY"),
            }
            generated.append(
                GeneratedClockExpectation(
                    clock_id=clock_id,
                    domain_id=domain.domain_id,
                    master_clock_id=domain.master_clock_id,
                    divider_ratio=ratio,
                    waveform=state["waveform"],
                    divider_state_fingerprint=canonical_sha256(state),
                    active_consumers=(
                        f"{subsystem_instance}.u_domain.gen_consumers[{index}].u_consumer",
                    ),
                )
            )
    payload = {
        "schema_version": 1,
        "master_clock_ids": tuple(item.master_clock_id for item in config.master_domains),
        "generated_clocks": tuple(generated),
        "expected_master_clocks": len(config.master_domains),
        "expected_generated_per_master": config.generated_clocks_per_master,
        "expected_generated_total": len(generated),
    }
    hash_payload = {
        **payload,
        "generated_clocks": tuple(item.model_dump(mode="json") for item in generated),
    }
    return BenchmarkExpectations(
        **payload,
        expectations_hash=canonical_sha256(hash_payload),
    )


def _manifest_bytes(config: BenchmarkConfig, expectations: BenchmarkExpectations) -> bytes:
    payload = {
        "schema_version": 1,
        "profile": config.profile,
        "top_module": config.top_module,
        "template_version": config.template_version,
        "workload_scale": config.workload_scale,
        "master_domains": [item.model_dump(mode="json") for item in config.master_domains],
        "divider_ratios": list(config.divider_ratios),
        "expectations_hash": expectations.expectations_hash,
    }
    return canonical_json_bytes(payload) + b"\n"


def _locked_input_files() -> dict[str, bytes]:
    return {path: (_PROJECT_ROOT / path).read_bytes() for path in _LOCKED_INPUTS}


def _project_manifest_bytes(
    config: BenchmarkConfig,
    generated: dict[str, bytes],
) -> bytes:
    """Render the strict M1 project entry point consumed by ``nova init``."""

    platform_views = PlatformAnalysisViews.model_validate(
        yaml.safe_load(generated["config/analysis/views.yaml"])
    )
    rtl_files = tuple(sorted(path for path in generated if path.endswith(".sv")))
    rtl_files = tuple(path for path in rtl_files if path.startswith("rtl/"))
    analysis_views = []
    for view in platform_views.views:
        is_setup = view.check == "SETUP"
        analysis_views.append(
            {
                "id": view.analysis_view_id,
                "mode": "FUNCTIONAL",
                "check": view.check,
                "liberty_corner": view.liberty_corner_id,
                "rc_corner": view.rc_corner_id,
                "operating_condition": (
                    "pvt_0p63v_100c" if is_setup else "pvt_0p77v_0c"
                ),
                "derate_policy": "config/analysis/views.yaml",
                "clock_uncertainty_policy": "config/analysis/views.yaml",
                "hard_limits": {
                    "setup_wns_ns" if is_setup else "hold_wns_ns": 0.0,
                },
                "sdc": "constraints/nebula.sdc",
                "required_stages": view.required_stages,
                "required": view.required,
            }
        )
    payload = {
        "schema_version": 2,
        "project": f"nebula_{config.profile}_benchmark",
        "top": config.top_module,
        "rtl": {"files": rtl_files, "include_dirs": ("rtl",), "defines": ()},
        "compilation_profiles": {
            "functional": {
                "defines": (),
                "parameters": {
                    "WORKLOAD_SCALE": config.workload_scale,
                    "GENERATED_CLOCKS_PER_MASTER": config.generated_clocks_per_master,
                },
            },
            "formal": {
                "property_files": ("formal/cdc_protocol_properties.sv",),
                "harness_manifest": "formal/harnesses.yaml",
                "property_only_define": "NOVA_FORMAL_PROPERTIES",
                "disallow_design_behavior_defines": True,
                "require_functional_logic_hash_match": True,
            },
        },
        "constraints": {
            "sdc": "constraints/nebula.sdc",
            "immutable": True,
            "expected_master_clocks": 5,
            "expected_generated_clocks_per_master": config.generated_clocks_per_master,
            "require_zero_unconstrained_endpoints": True,
            "require_constraint_binding_equivalence": True,
            "require_exception_coverage_equivalence": True,
        },
        "technology": {
            "platform_id": platform_views.platform_id,
            "platform_lock": "config/platform/platform.lock.yaml",
            "platform_lock_hash": platform_views.platform_lock_hash,
        },
        "analysis_views": analysis_views,
        "power_activity": {
            "format": "VCD",
            "source": "sim/activity/nebula_power_workload.vcd",
            "scope": "tb_nebula.dut",
            "time_window_ns": (300.0, 4300.0),
            "require_same_activity_hash": True,
            "vectorless_fallback": "ESTIMATED_ONLY_NOT_COMPARABLE",
        },
        "cdc": {
            "checker_mode": "STRUCTURAL_INVARIANT_AUDIT",
            "approved_pattern_registry": "config/policy/cdc_patterns.yaml",
            "require_zero_unapproved_crossings": True,
            "require_candidate_inventory_equivalence": True,
            "protocol_property_manifest": "formal/harnesses.yaml",
            "external_cdc_tool": None,
        },
        "formal": {
            "primary_competition_contract": "STRICT_SEQ_EQUIV",
            "require_primary_latency_preserving_candidate": True,
            "proof_scope_policy": "WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED",
            "asynchronous_master_clocks": 5,
            "master_clock_model": "INDEPENDENT_SHARED_GOLD_GATE_EVENTS",
            "generated_clock_model": "DERIVED_FROM_PROTECTED_DIVIDER_STATE",
            "sby_multiclock": True,
            "reset_assumption_manifest": "config/formal/reset_assumptions.yaml",
            "prohibit_assumption_only_equivalence": True,
        },
        "protection": {
            "modules": (
                "protected_clock_divider",
                "clock_divider_bank",
                "cdc_level_sync",
                "cdc_toggle_sync",
                "cdc_req_ack",
                "cdc_async_fifo",
                "cdc_gray_sync",
                "reset_synchronizer",
            ),
            "path_patterns": ("rtl/clocking/**", "rtl/cdc/**"),
        },
        "optimization": {
            "objective_policy": "BALANCED_PPA",
            "max_area_growth_percent": 5.0,
            "max_candidates": 40,
            "openroad_finalists": 4,
            "allowed_contracts": (
                "STRICT_SEQ_EQUIV",
                "RETIMING_EQUIV",
                "LATENCY_AWARE",
            ),
            "editable_path_patterns": ("rtl/subsystems/**", "rtl/workload/**"),
            "deterministic_seed": 20260808,
        },
        "planner": {
            "mode": "AGENT_COUNCIL",
            "runtime": "LANGGRAPH",
            "fallback_order": ("SINGLE_AGENT", "HEURISTIC"),
            "max_proposals": 3,
            "max_parallel_specialists": 3,
            "max_revision_rounds": 1,
            "deadline_seconds": 120,
            "aggregate_token_budget": 30000,
        },
        "context_isolation": {
            "mode": "ROLE_SCOPED_EVIDENCE_PACKS",
            "shared_envelope_max_tokens": 2000,
            "private_pack_max_tokens": 7000,
            "chair_pack_max_tokens": 6000,
            "allow_read_only_retrieval": True,
            "require_snapshot_hash_match": True,
            "blind_independent_round": True,
            "blind_parallel_critics": True,
            "randomize_neutral_proposal_order": True,
        },
        "recovery": {
            "enabled": True,
            "policy_version": "recovery_policy_v1",
            "diagnostic_rule_registry": "config/policy/default.yaml",
            "fingerprint_schema": "candidate_fingerprint_v1",
            "similarity_threshold": 0.85,
            "no_progress_wns_epsilon_ns": 0.01,
            "no_progress_area_epsilon_percent": 0.1,
            "repeated_failure_count": 2,
            "max_recovery_depth_per_lineage": 4,
            "allow_targeted_recovery_council": True,
            "force_fresh_sessions_after_stagnation": True,
            "prohibit_same_transform_family_after_stagnation": True,
        },
        "physical": {
            "placement_finalists": 4,
            "routed_finalists": 2,
            "repeat_final_seeds": (20260808, 20260809, 20260810),
        },
    }
    manifest = ProjectManifest.model_validate(payload)
    return yaml.safe_dump(
        manifest.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")


def _cdc_contract_bytes() -> bytes:
    crossings = (
        ("u_reset_compute", "reset_synchronizer", "RESET_ASYNC_ASSERT_LOCAL_RELEASE"),
        ("u_reset_control", "reset_synchronizer", "RESET_ASYNC_ASSERT_LOCAL_RELEASE"),
        ("u_reset_dma", "reset_synchronizer", "RESET_ASYNC_ASSERT_LOCAL_RELEASE"),
        ("u_reset_ingress", "reset_synchronizer", "RESET_ASYNC_ASSERT_LOCAL_RELEASE"),
        ("u_reset_schedule", "reset_synchronizer", "RESET_ASYNC_ASSERT_LOCAL_RELEASE"),
        ("u_ingress_to_schedule_pulse", "cdc_toggle_sync", "TOGGLE_PULSE"),
        ("u_schedule_to_dma_level", "cdc_level_sync", "STABLE_LEVEL_TWO_FLOP"),
        ("u_ingress_to_compute_fifo", "cdc_async_fifo", "ASYNC_FIFO_GRAY_POINTERS"),
        ("u_compute_to_control", "cdc_req_ack", "REQUEST_ACKNOWLEDGE"),
        ("u_dma_to_control", "cdc_req_ack", "REQUEST_ACKNOWLEDGE"),
        ("u_ingress_to_control", "cdc_req_ack", "REQUEST_ACKNOWLEDGE"),
        ("u_schedule_to_control", "cdc_req_ack", "REQUEST_ACKNOWLEDGE"),
    )
    payload = {
        "schema_version": 1,
        "approved_patterns": [
            "ASYNC_FIFO_GRAY_POINTERS",
            "REQUEST_ACKNOWLEDGE",
            "RESET_ASYNC_ASSERT_LOCAL_RELEASE",
            "STABLE_LEVEL_TWO_FLOP",
            "TOGGLE_PULSE",
        ],
        "crossings": [
            {"instance": instance, "module": module, "pattern": pattern}
            for instance, module, pattern in sorted(crossings)
        ],
    }
    return canonical_json_bytes(payload) + b"\n"


def _build_clock_inventory(
    config: BenchmarkConfig,
    expectations: BenchmarkExpectations,
) -> ClockInventory:
    periods = master_periods(config)
    master_clocks = tuple(
        MasterClockEntry(
            clock_id=domain.master_clock_id,
            domain_id=domain.domain_id,
            source_object=f"nebula_top/{domain.master_clock_id}",
            period_ns=periods[domain.master_clock_id],
            waveform_ns=(0.0, periods[domain.master_clock_id] / 2.0),
            active_consumer_count=3,
        )
        for domain in config.master_domains
    )
    expected_by_id = {item.clock_id: item for item in expectations.generated_clocks}
    generated_clocks = tuple(
        GeneratedClockEntry(
            clock_id=expected.clock_id,
            domain_id=expected.domain_id,
            master_clock_id=expected.master_clock_id,
            source_object=(
                f"nebula_top/u_{expected.domain_id}_dividers/"
                f"gen_div_{expected.divider_ratio:02d}/u_divider/generated_clock"
            ),
            multiply_by=1,
            divide_by=expected.divider_ratio,
            waveform_ns=(
                0.0,
                periods[expected.master_clock_id]
                * ((expected.divider_ratio + 1) // 2),
            ),
            active_consumer_count=len(expected.active_consumers),
        )
        for expected in (expected_by_id[item.clock_id] for item in expectations.generated_clocks)
    )
    lineage = tuple(
        ClockLineageEdge(
            parent_clock_id=item.master_clock_id,
            child_clock_id=item.clock_id,
            relationship="GENERATED_FROM",
        )
        for item in generated_clocks
    )
    payload = {
        "schema_version": 1,
        "candidate_id": f"benchmark_{config.profile}",
        "expected_master_count": 5,
        "expected_generated_clocks_per_master": config.generated_clocks_per_master,
        "master_clocks": master_clocks,
        "generated_clocks": generated_clocks,
        "lineage_edges": lineage,
        "cross_master_synchronous_relationships": (),
    }
    hash_payload = {
        **payload,
        "master_clocks": tuple(item.model_dump(mode="json") for item in master_clocks),
        "generated_clocks": tuple(item.model_dump(mode="json") for item in generated_clocks),
        "lineage_edges": tuple(item.model_dump(mode="json") for item in lineage),
    }
    return ClockInventory(
        **payload,
        clock_graph_hash=canonical_sha256(hash_payload),
    )


def _build_cdc_inventory(config: BenchmarkConfig) -> CDCInventory:
    specifications = (
        (
            "reset_ingress",
            "external_reset",
            "ingress",
            "rst_n",
            "u_reset_ingress/local_reset_n",
            "RESET",
            "reset_async_assert_local_release",
            ("reset_async_assert_local_release",),
        ),
        (
            "reset_schedule",
            "external_reset",
            "schedule",
            "rst_n",
            "u_reset_schedule/local_reset_n",
            "RESET",
            "reset_async_assert_local_release",
            ("reset_async_assert_local_release",),
        ),
        (
            "reset_dma",
            "external_reset",
            "dma",
            "rst_n",
            "u_reset_dma/local_reset_n",
            "RESET",
            "reset_async_assert_local_release",
            ("reset_async_assert_local_release",),
        ),
        (
            "reset_compute",
            "external_reset",
            "compute",
            "rst_n",
            "u_reset_compute/local_reset_n",
            "RESET",
            "reset_async_assert_local_release",
            ("reset_async_assert_local_release",),
        ),
        (
            "reset_control",
            "external_reset",
            "control",
            "rst_n",
            "u_reset_control/local_reset_n",
            "RESET",
            "reset_async_assert_local_release",
            ("reset_async_assert_local_release",),
        ),
        (
            "ingress_schedule_pulse",
            "ingress",
            "schedule",
            "u_ingress/snapshot_valid",
            "u_schedule/external_event",
            "PULSE",
            "toggle_pulse",
            ("toggle_pulse_delivery",),
        ),
        (
            "schedule_dma_level",
            "schedule",
            "dma",
            "u_schedule/active_level",
            "u_dma/external_event",
            "SINGLE_BIT_CONTROL",
            "stable_level_two_flop",
            ("stable_level_two_flop",),
        ),
        (
            "ingress_compute_fifo",
            "ingress",
            "compute",
            "u_ingress/checksum",
            "u_compute/external_data",
            "MULTI_BIT_DATA",
            "async_fifo_gray_pointers",
            ("async_fifo_gray_pointer_safety",),
        ),
        (
            "ingress_control_req_ack",
            "ingress",
            "control",
            "u_ingress/checksum",
            "u_control/ingress_latest",
            "MULTI_BIT_DATA",
            "request_acknowledge",
            ("req_ack_one_outstanding",),
        ),
        (
            "schedule_control_req_ack",
            "schedule",
            "control",
            "u_schedule/checksum",
            "u_control/schedule_latest",
            "MULTI_BIT_DATA",
            "request_acknowledge",
            ("req_ack_one_outstanding",),
        ),
        (
            "dma_control_req_ack",
            "dma",
            "control",
            "u_dma/checksum",
            "u_control/dma_latest",
            "MULTI_BIT_DATA",
            "request_acknowledge",
            ("req_ack_one_outstanding",),
        ),
        (
            "compute_control_req_ack",
            "compute",
            "control",
            "u_compute/checksum",
            "u_control/compute_latest",
            "MULTI_BIT_DATA",
            "request_acknowledge",
            ("req_ack_one_outstanding",),
        ),
    )
    crossings = tuple(
        CdcCrossing(
            crossing_id=crossing_id,
            source_domain_id=source_domain,
            destination_domain_id=destination_domain,
            source_object=source_object,
            destination_object=destination_object,
            signal_class=signal_class,
            recognized_pattern=recognized_pattern,
            structural_fingerprint=canonical_sha256(
                {
                    "template_version": config.template_version,
                    "crossing_id": crossing_id,
                    "source_domain_id": source_domain,
                    "destination_domain_id": destination_domain,
                    "recognized_pattern": recognized_pattern,
                }
            ),
            protocol_property_refs=property_refs,
            status="APPROVED",
        )
        for (
            crossing_id,
            source_domain,
            destination_domain,
            source_object,
            destination_object,
            signal_class,
            recognized_pattern,
            property_refs,
        ) in specifications
    )
    registry_hash = canonical_sha256(
        {
            "approved_patterns": tuple(
                sorted(item.recognized_pattern for item in crossings if item.recognized_pattern)
            )
        }
    )
    payload = {
        "schema_version": 1,
        "candidate_id": f"benchmark_{config.profile}",
        "crossings": crossings,
        "approved_pattern_registry_hash": registry_hash,
        "new_unapproved_count": 0,
        "changed_approved_structure_count": 0,
        "removed_approved_structure_count": 0,
        "ambiguous_count": 0,
    }
    hash_payload = {
        **payload,
        "crossings": tuple(item.model_dump(mode="json") for item in crossings),
    }
    return CDCInventory(
        **payload,
        inventory_hash=canonical_sha256(hash_payload),
    )


def _file_contract(path: str, content: bytes) -> BenchmarkFile:
    suffix = Path(path).suffix
    media_types = {
        ".json": "application/json",
        ".sdc": "application/x-sdc",
        ".sv": "text/x-systemverilog",
        ".svh": "text/x-systemverilog",
        ".yaml": "application/yaml",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return BenchmarkFile(
        relative_path=path,
        sha256=_hash_bytes(content),
        size_bytes=len(content),
        media_type=media_type,
    )


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative_path, content in sorted(files.items()):
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def generate_benchmark(
    config: BenchmarkConfig,
    output: Path,
    *,
    template_root: Path | None = None,
) -> BenchmarkSnapshot:
    """Generate and atomically publish a deterministic benchmark snapshot."""

    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"output path already exists: {output}")
    templates = _read_templates((template_root or _DEFAULT_TEMPLATE_ROOT).absolute())
    expectations = _build_expectations(config)
    sdc, constraint_contract = render_constraints(config, expectations)
    clock_inventory = _build_clock_inventory(config, expectations)
    cdc_inventory = _build_cdc_inventory(config)
    formal_assets, formal_manifest = render_formal_assets()
    power_bytes, power_workload = render_power_workload()
    generated = {
        **templates,
        **_locked_input_files(),
        "rtl/benchmark_parameters.svh": _render_parameters(config),
        "constraints/nebula.sdc": sdc,
        "expected/clock_contract.json": canonical_json_bytes(expectations) + b"\n",
        "expected/cdc_contract.json": _cdc_contract_bytes(),
        "expected/clock_inventory.json": canonical_json_bytes(clock_inventory) + b"\n",
        "expected/cdc_inventory.json": canonical_json_bytes(cdc_inventory) + b"\n",
        "expected/constraint_contract.json": constraint_contract_bytes(constraint_contract),
        **formal_assets,
        "sim/power_workload.yaml": power_bytes,
        "sim/tb_nebula.sv": (_PROJECT_ROOT / "benchmark/sim/tb_nebula.sv").read_bytes(),
        "project.json": _manifest_bytes(config, expectations),
    }
    generated["project.yaml"] = _project_manifest_bytes(config, generated)
    rtl_sources = {path: content for path, content in generated.items() if path.startswith("rtl/")}
    snapshot_payload = {
        "schema_version": 1,
        "profile": config.profile,
        "top_module": config.top_module,
        "config_hash": canonical_sha256(config),
        "template_hash": _hash_file_map(templates),
        "source_hash": _hash_file_map(rtl_sources),
        "constraint_contract_hash": constraint_contract.contract_hash,
        "clock_inventory_hash": clock_inventory.clock_graph_hash,
        "cdc_inventory_hash": cdc_inventory.inventory_hash,
        "formal_manifest_hash": formal_manifest.manifest_hash,
        "power_workload_hash": power_workload.workload_hash,
        "expected_master_clocks": len(config.master_domains),
        "expected_generated_per_master": config.generated_clocks_per_master,
        "expected_generated_total": len(expectations.generated_clocks),
        "generated_files": tuple(
            _file_contract(path, content) for path, content in sorted(generated.items())
        ),
        "expectations": expectations,
    }
    snapshot_hash_payload = {
        **snapshot_payload,
        "generated_files": tuple(
            item.model_dump(mode="json") for item in snapshot_payload["generated_files"]
        ),
        "expectations": expectations.model_dump(mode="json"),
    }
    snapshot = BenchmarkSnapshot(
        **snapshot_payload,
        snapshot_hash=canonical_sha256(snapshot_hash_payload),
    )
    files = {
        **generated,
        _SNAPSHOT_FILENAME: canonical_json_bytes(snapshot) + b"\n",
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        _write_tree(staging, files)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return snapshot


__all__ = [
    "BenchmarkGeneratorManifest",
    "BenchmarkProfile",
    "generate_benchmark",
    "load_benchmark_config",
]
