"""Strict contracts for deterministic NOVA benchmark generation and calibration."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from nova_rtl.contracts.base import (
    ArtifactRef,
    EntityId,
    FiniteFloat,
    HashRef,
    NonEmptyString,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    StrictContract,
    canonical_sha256,
)
from nova_rtl.contracts.platform import ToolFingerprint

PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
DividerRatio = Annotated[int, Field(strict=True)]

DEFAULT_MASTER_DOMAINS = (
    ("ingress", "clk_ingress"),
    ("schedule", "clk_schedule"),
    ("dma", "clk_dma"),
    ("compute", "clk_compute"),
    ("control", "clk_control"),
)
FULL_DIVIDER_RATIOS = (
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    10,
    12,
    16,
    20,
    24,
    25,
    32,
    40,
    48,
    50,
    64,
    80,
    96,
    128,
)


class BenchmarkMasterDomain(StrictContract):
    """Stable identity of one independently clocked benchmark subsystem."""

    domain_id: EntityId
    master_clock_id: EntityId


class MappedCellTarget(StrictContract):
    """Inclusive mapped standard-cell target used by later calibration."""

    minimum: PositiveStrictInt
    maximum: PositiveStrictInt

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("mapped cell target minimum must not exceed maximum")
        return self


class BenchmarkConfig(StrictContract):
    """Resolved, profile-specific input to deterministic benchmark generation."""

    schema_version: Literal[1] = 1
    profile: EntityId
    template_version: EntityId
    top_module: EntityId
    workload_scale: PositiveStrictInt
    master_domains: tuple[BenchmarkMasterDomain, ...]
    generated_clocks_per_master: PositiveStrictInt
    divider_ratios: tuple[DividerRatio, ...]
    mapped_cell_target: MappedCellTarget | None

    @model_validator(mode="after")
    def topology_is_consistent(self) -> Self:
        if len(self.master_domains) != 5:
            raise ValueError("benchmark configuration must contain exactly five master domains")
        domain_ids = tuple(item.domain_id for item in self.master_domains)
        if len(domain_ids) != len(set(domain_ids)):
            raise ValueError("master domain identities must be unique")
        clock_ids = tuple(item.master_clock_id for item in self.master_domains)
        if len(clock_ids) != len(set(clock_ids)):
            raise ValueError("master clock identities must be unique")
        if len(self.divider_ratios) != len(set(self.divider_ratios)):
            raise ValueError("divider ratios must be unique")
        if any(ratio <= 1 for ratio in self.divider_ratios):
            raise ValueError("divider ratios must be greater than one")
        if self.generated_clocks_per_master != len(self.divider_ratios):
            raise ValueError("generated_clocks_per_master must equal the divider ratio count")
        return self

    @classmethod
    def default(cls) -> BenchmarkConfig:
        """Return the canonical full-profile configuration."""

        return cls(
            profile="full",
            template_version="benchmark_template_v1",
            top_module="nebula_top",
            workload_scale=4,
            master_domains=tuple(
                BenchmarkMasterDomain(domain_id=domain, master_clock_id=clock)
                for domain, clock in DEFAULT_MASTER_DOMAINS
            ),
            generated_clocks_per_master=len(FULL_DIVIDER_RATIOS),
            divider_ratios=FULL_DIVIDER_RATIOS,
            mapped_cell_target=MappedCellTarget(minimum=45_000, maximum=55_000),
        )


class GeneratedClockExpectation(StrictContract):
    """Expected generated-clock identity, divider semantics, and active consumers."""

    clock_id: EntityId
    domain_id: EntityId
    master_clock_id: EntityId
    divider_ratio: DividerRatio
    waveform: Literal["EVEN_50_PERCENT", "ODD_ALTERNATING_DUTY"]
    divider_state_fingerprint: HashRef
    active_consumers: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def consumers_are_unique(self) -> Self:
        if len(self.active_consumers) != len(set(self.active_consumers)):
            raise ValueError("active generated-clock consumers must be unique")
        return self


class BenchmarkExpectations(StrictContract):
    """Machine-checkable topology expected from a generated benchmark."""

    schema_version: Literal[1] = 1
    master_clock_ids: tuple[EntityId, ...]
    generated_clocks: tuple[GeneratedClockExpectation, ...]
    expected_master_clocks: PositiveStrictInt
    expected_generated_per_master: PositiveStrictInt
    expected_generated_total: PositiveStrictInt
    expectations_hash: HashRef

    @model_validator(mode="after")
    def identities_counts_and_hash_are_consistent(self) -> Self:
        if len(self.master_clock_ids) != len(set(self.master_clock_ids)):
            raise ValueError("expected master clock identities must be unique")
        if len(self.master_clock_ids) != self.expected_master_clocks:
            raise ValueError("expected master clock count is inconsistent")
        generated_ids = tuple(item.clock_id for item in self.generated_clocks)
        if len(generated_ids) != len(set(generated_ids)):
            raise ValueError("expected generated clock identities must be unique")
        if len(generated_ids) != self.expected_generated_total:
            raise ValueError("expected generated clock total is inconsistent")
        if (
            self.expected_master_clocks * self.expected_generated_per_master
            != self.expected_generated_total
        ):
            raise ValueError("expected per-master generated clock count is inconsistent")
        counts = {clock_id: 0 for clock_id in self.master_clock_ids}
        for generated in self.generated_clocks:
            if generated.master_clock_id not in counts:
                raise ValueError("generated clock refers to an unknown master clock")
            counts[generated.master_clock_id] += 1
        if set(counts.values()) != {self.expected_generated_per_master}:
            raise ValueError("generated clock distribution is inconsistent")
        expected_hash = canonical_sha256(self, exclude=frozenset({"expectations_hash"}))
        if self.expectations_hash != expected_hash:
            raise ValueError("expectations_hash does not match canonical expectations")
        return self


class BenchmarkFile(StrictContract):
    """Content identity for one deterministic generated file."""

    relative_path: NonEmptyString
    sha256: HashRef
    size_bytes: NonNegativeInt
    media_type: NonEmptyString


class MasterClockConstraint(StrictContract):
    """One stable master-clock SDC binding."""

    clock_id: EntityId
    domain_id: EntityId
    source_selector: NonEmptyString
    period_ns: PositiveFloat
    waveform_ns: tuple[NonNegativeFloat, NonNegativeFloat]

    @model_validator(mode="after")
    def waveform_is_within_period(self) -> Self:
        start, end = self.waveform_ns
        if end <= start or end > self.period_ns:
            raise ValueError("master clock waveform must increase and fit within its period")
        return self


class GeneratedClockConstraint(StrictContract):
    """One generated-clock SDC binding and waveform declaration."""

    clock_id: EntityId
    domain_id: EntityId
    master_clock_id: EntityId
    source_selector: NonEmptyString
    target_selector: NonEmptyString
    divide_by: PositiveStrictInt
    waveform_mode: Literal["EVEN_DIVIDE_BY", "ODD_EDGE_LIST"]
    edge_indices: tuple[PositiveStrictInt, ...]

    @model_validator(mode="after")
    def waveform_matches_divider(self) -> Self:
        if self.divide_by % 2 == 0:
            if self.waveform_mode != "EVEN_DIVIDE_BY" or self.edge_indices:
                raise ValueError("even generated clocks must use divide-by waveform semantics")
        else:
            expected = (1, self.divide_by + 2, (2 * self.divide_by) + 1)
            if self.waveform_mode != "ODD_EDGE_LIST" or self.edge_indices != expected:
                raise ValueError("odd generated clock edge list does not match divider semantics")
        return self


class ReviewedConstraintException(StrictContract):
    """Narrow timing exception accepted by benchmark policy review."""

    exception_id: EntityId
    exception_type: Literal["FALSE_PATH"]
    from_selector: NonEmptyString
    to_selector: NonEmptyString
    rationale: NonEmptyString


class BenchmarkConstraintContract(StrictContract):
    """Complete profile-specific SDC semantics and stable selector inventory."""

    schema_version: Literal[1] = 1
    top_module: EntityId
    master_clocks: tuple[MasterClockConstraint, ...]
    generated_clocks: tuple[GeneratedClockConstraint, ...]
    asynchronous_master_groups: tuple[tuple[EntityId, ...], ...]
    setup_clock_uncertainty_ns: NonNegativeFloat
    hold_clock_uncertainty_ns: NonNegativeFloat
    input_delay_ns: NonNegativeFloat
    input_delay_clock_id: EntityId
    input_delay_selectors: tuple[NonEmptyString, ...]
    output_delay_ns: NonNegativeFloat
    output_delay_clock_id: EntityId
    output_delay_selectors: tuple[NonEmptyString, ...]
    reviewed_exceptions: tuple[ReviewedConstraintException, ...]
    expected_unconstrained_endpoints: Literal[0]
    sdc_hash: HashRef
    contract_hash: HashRef

    @model_validator(mode="after")
    def topology_selectors_and_hash_are_consistent(self) -> Self:
        if len(self.master_clocks) != 5:
            raise ValueError("constraint contract must contain exactly five master clocks")
        master_ids = tuple(item.clock_id for item in self.master_clocks)
        generated_ids = tuple(item.clock_id for item in self.generated_clocks)
        if len(master_ids) != len(set(master_ids)):
            raise ValueError("constraint master clock IDs must be unique")
        if len(generated_ids) != len(set(generated_ids)):
            raise ValueError("constraint generated clock IDs must be unique")
        if tuple(sorted(self.asynchronous_master_groups)) != tuple(
            sorted((clock_id,) for clock_id in master_ids)
        ):
            raise ValueError("all five master clocks must be singleton asynchronous groups")
        if any(item.master_clock_id not in set(master_ids) for item in self.generated_clocks):
            raise ValueError("generated clock constraint has no master ancestor")
        if self.input_delay_clock_id not in set(master_ids):
            raise ValueError("input delay clock must be a declared master clock")
        if self.output_delay_clock_id not in set(master_ids):
            raise ValueError("output delay clock must be a declared master clock")
        if not self.input_delay_selectors or not self.output_delay_selectors:
            raise ValueError("constraint contract must declare input and output delay selectors")
        exception_ids = tuple(item.exception_id for item in self.reviewed_exceptions)
        if len(exception_ids) != len(set(exception_ids)):
            raise ValueError("reviewed constraint exception IDs must be unique")
        expected_hash = canonical_sha256(self, exclude=frozenset({"contract_hash"}))
        if self.contract_hash != expected_hash:
            raise ValueError("contract_hash does not match canonical constraint contract")
        return self


class PowerWorkloadPhase(StrictContract):
    """One deterministic simulation phase in the power stimulus recipe."""

    phase_id: EntityId
    start_ns: NonNegativeFloat
    end_ns: PositiveFloat
    activity_class: Literal[
        "RESET",
        "IDLE",
        "BURST",
        "ARBITRATION",
        "BACKPRESSURE",
        "CDC_TRAFFIC",
    ]

    @model_validator(mode="after")
    def interval_increases(self) -> Self:
        if self.end_ns <= self.start_ns:
            raise ValueError("power workload phase end must be greater than start")
        return self


class BenchmarkPowerWorkload(StrictContract):
    """Fixed simulation, VCD scope, and comparable measurement-window identity."""

    schema_version: Literal[1] = 1
    workload_id: EntityId
    seed: NonNegativeInt
    phases: tuple[PowerWorkloadPhase, ...]
    vcd_file: NonEmptyString
    vcd_scope: NonEmptyString
    measurement_window_ns: tuple[NonNegativeFloat, PositiveFloat]
    workload_hash: HashRef

    @model_validator(mode="after")
    def phases_window_and_hash_are_consistent(self) -> Self:
        expected_phase_ids = (
            "reset",
            "idle",
            "bursts",
            "arbitration",
            "backpressure",
            "cdc_traffic",
        )
        if tuple(item.phase_id for item in self.phases) != expected_phase_ids:
            raise ValueError("power workload phases must use the canonical order")
        for previous, current in zip(self.phases, self.phases[1:], strict=False):
            if previous.end_ns != current.start_ns:
                raise ValueError("power workload phases must be contiguous")
        start, end = self.measurement_window_ns
        if end <= start:
            raise ValueError("power measurement window must increase")
        if start < self.phases[0].start_ns or end > self.phases[-1].end_ns:
            raise ValueError("power measurement window must fit inside workload phases")
        expected_hash = canonical_sha256(self, exclude=frozenset({"workload_hash"}))
        if self.workload_hash != expected_hash:
            raise ValueError("workload_hash does not match canonical power workload")
        return self


class FormalHarnessEntry(StrictContract):
    """One property family bound to protected benchmark structures."""

    harness_id: EntityId
    property_ids: tuple[EntityId, ...]
    bound_modules: tuple[EntityId, ...]

    @model_validator(mode="after")
    def identities_are_nonempty_and_unique(self) -> Self:
        if not self.property_ids or len(self.property_ids) != len(set(self.property_ids)):
            raise ValueError("formal harness property IDs must be nonempty and unique")
        if not self.bound_modules or len(self.bound_modules) != len(set(self.bound_modules)):
            raise ValueError("formal harness module bindings must be nonempty and unique")
        return self


class BenchmarkFormalManifest(StrictContract):
    """Hashed source and binding identity for benchmark CDC protocol properties."""

    schema_version: Literal[1] = 1
    formal_model_id: EntityId
    source_files: tuple[NonEmptyString, ...]
    source_hash: HashRef
    property_ids: tuple[EntityId, ...]
    harnesses: tuple[FormalHarnessEntry, ...]
    manifest_hash: HashRef

    @model_validator(mode="after")
    def sources_properties_and_hash_are_consistent(self) -> Self:
        if not self.source_files or len(self.source_files) != len(set(self.source_files)):
            raise ValueError("formal source files must be nonempty and unique")
        if not self.property_ids or len(self.property_ids) != len(set(self.property_ids)):
            raise ValueError("formal property IDs must be nonempty and unique")
        referenced = {item for harness in self.harnesses for item in harness.property_ids}
        if referenced != set(self.property_ids):
            raise ValueError("formal harnesses must cover every declared property exactly by ID")
        expected_hash = canonical_sha256(self, exclude=frozenset({"manifest_hash"}))
        if self.manifest_hash != expected_hash:
            raise ValueError("manifest_hash does not match canonical formal manifest")
        return self


class BenchmarkValidationEvidence(StrictContract):
    """Elaboration-aware selector, reachability, and endpoint validation evidence."""

    schema_version: Literal[1] = 1
    constraint_contract_hash: HashRef
    elaborated_design_hash: HashRef
    unresolved_selectors: tuple[NonEmptyString, ...]
    generated_clocks_without_consumers: tuple[EntityId, ...]
    unexpected_unconstrained_endpoints: tuple[NonEmptyString, ...]
    challenge_logic_without_observable_path: tuple[NonEmptyString, ...]
    evidence_hash: HashRef

    @model_validator(mode="after")
    def evidence_hash_is_consistent(self) -> Self:
        expected_hash = canonical_sha256(self, exclude=frozenset({"evidence_hash"}))
        if self.evidence_hash != expected_hash:
            raise ValueError("evidence_hash does not match canonical validation evidence")
        return self


class BenchmarkSnapshot(StrictContract):
    """Immutable identity boundary for generated benchmark sources and expectations."""

    schema_version: Literal[1] = 1
    profile: EntityId
    top_module: EntityId
    config_hash: HashRef
    template_hash: HashRef
    source_hash: HashRef
    constraint_contract_hash: HashRef
    clock_inventory_hash: HashRef
    cdc_inventory_hash: HashRef
    formal_manifest_hash: HashRef
    power_workload_hash: HashRef
    expected_master_clocks: PositiveStrictInt
    expected_generated_per_master: PositiveStrictInt
    expected_generated_total: PositiveStrictInt
    generated_files: tuple[BenchmarkFile, ...]
    expectations: BenchmarkExpectations
    snapshot_hash: HashRef

    @model_validator(mode="after")
    def counts_ordering_and_hash_are_consistent(self) -> Self:
        paths = tuple(item.relative_path for item in self.generated_files)
        if paths != tuple(sorted(paths)):
            raise ValueError("generated files must use deterministic path ordering")
        if len(paths) != len(set(paths)):
            raise ValueError("generated file paths must be unique")
        expected_counts = (
            self.expectations.expected_master_clocks,
            self.expectations.expected_generated_per_master,
            self.expectations.expected_generated_total,
        )
        if expected_counts != (
            self.expected_master_clocks,
            self.expected_generated_per_master,
            self.expected_generated_total,
        ):
            raise ValueError("snapshot clock counts do not match expectations")
        expected_hash = canonical_sha256(self, exclude=frozenset({"snapshot_hash"}))
        if self.snapshot_hash != expected_hash:
            raise ValueError("snapshot_hash does not match canonical snapshot")
        return self


class BenchmarkValidation(StrictContract):
    """Deterministic validation result for one benchmark snapshot."""

    schema_version: Literal[1] = 1
    snapshot_hash: HashRef
    expectations_hash: HashRef
    status: Literal["PASS", "FAIL"]
    expectation_mismatches: tuple[NonEmptyString, ...]
    generated_without_consumers: tuple[EntityId, ...]
    artifact_identity_mismatches: tuple[NonEmptyString, ...] = ()
    clock_lineage_mismatches: tuple[NonEmptyString, ...] = ()
    cdc_inventory_mismatches: tuple[NonEmptyString, ...] = ()
    unreachable_challenge_logic: tuple[NonEmptyString, ...] = ()
    unexpected_unconstrained_endpoints: tuple[NonEmptyString, ...] = ()
    validation_hash: HashRef

    @model_validator(mode="after")
    def status_and_hash_are_consistent(self) -> Self:
        should_pass = not any(
            (
                self.expectation_mismatches,
                self.generated_without_consumers,
                self.artifact_identity_mismatches,
                self.clock_lineage_mismatches,
                self.cdc_inventory_mismatches,
                self.unreachable_challenge_logic,
                self.unexpected_unconstrained_endpoints,
            )
        )
        if (self.status == "PASS") != should_pass:
            raise ValueError("benchmark validation status is inconsistent with findings")
        expected_hash = canonical_sha256(self, exclude=frozenset({"validation_hash"}))
        if self.validation_hash != expected_hash:
            raise ValueError("validation_hash does not match canonical validation")
        return self


class CalibrationSample(StrictContract):
    """One bounded benchmark scale observation."""

    workload_scale: PositiveStrictInt
    mapped_cell_count: PositiveStrictInt
    config_hash: HashRef
    source_hash: HashRef
    snapshot_hash: HashRef
    recipe_hash: HashRef
    tool_fingerprint: ToolFingerprint
    stage_result_artifact: ArtifactRef


class CalibrationReport(StrictContract):
    """Bounded calibration evidence for the full benchmark cell-count target."""

    schema_version: Literal[1] = 1
    target: MappedCellTarget
    samples: tuple[CalibrationSample, ...]
    selected_workload_scale: PositiveStrictInt | None
    status: Literal["PASS", "TARGET_NOT_REACHED", "BUDGET_EXHAUSTED"]
    report_hash: HashRef

    @model_validator(mode="after")
    def selection_budget_and_hash_are_consistent(self) -> Self:
        if not self.samples:
            raise ValueError("calibration report must contain at least one sample")
        if len(self.samples) > 12:
            raise ValueError("calibration report exceeds the twelve-sample budget")
        scales = tuple(sample.workload_scale for sample in self.samples)
        if len(scales) != len(set(scales)):
            raise ValueError("calibration sample scales must be unique")
        matching = {
            sample.workload_scale
            for sample in self.samples
            if self.target.minimum <= sample.mapped_cell_count <= self.target.maximum
        }
        if self.status == "PASS":
            if self.selected_workload_scale not in matching:
                raise ValueError("passing calibration must select an in-range sample")
        elif self.selected_workload_scale is not None:
            raise ValueError("failed calibration must not select a workload scale")
        expected_hash = canonical_sha256(self, exclude=frozenset({"report_hash"}))
        if self.report_hash != expected_hash:
            raise ValueError("report_hash does not match canonical calibration report")
        return self


GitCommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class M2SignoffReport(StrictContract):
    """Commit-bound, replayable trustworthy full-baseline sign-off packet."""

    schema_version: Literal[1] = 1
    status: Literal["PASS"]
    commit_sha: GitCommitSha
    implementation_tree_hash: GitCommitSha
    m1_implementation_commit: GitCommitSha
    m1_signoff_invocation_hash: HashRef
    m1_packet_hash: HashRef
    run_id: EntityId
    profile: Literal["full"]
    expected_master_clocks: Literal[5]
    expected_generated_clocks: Literal[105]
    run_index_hash: HashRef
    benchmark_snapshot_hash: HashRef
    design_contract_hash: HashRef
    platform_lock_hash: HashRef
    calibration_report_hash: HashRef
    calibration_validation_hash: HashRef
    analysis_view_hashes: dict[EntityId, HashRef]
    stage_result_hashes: dict[EntityId, HashRef]
    raw_artifact_hashes: dict[EntityId, HashRef]
    tool_build_hashes: dict[EntityId, HashRef]
    constraint_binding_hash: HashRef
    clock_graph_hash: HashRef
    cdc_inventory_hash: HashRef
    formal_model_hash: HashRef
    shared_physical_checkpoint_hash: HashRef
    setup_wns_ns: FiniteFloat
    setup_tns_ns: FiniteFloat
    hold_wns_ns: FiniteFloat
    hold_tns_ns: FiniteFloat
    timing_constraint_status: Literal["PASS", "FAIL"]
    replay_digest: HashRef
    ledger_hash: HashRef
    input_set_hash: HashRef
    report_hash: HashRef

    @model_validator(mode="after")
    def identities_and_status_are_complete(self) -> Self:
        required_stages = {
            "stage_yosys",
            "stage_binding",
            "stage_clock",
            "stage_cdc",
            "stage_formal_preflight",
            "stage_simulation",
            "stage_eqy_smoke",
            "stage_sby_cdc_properties",
            "stage_opensta_asap7_setup",
            "stage_opensta_asap7_hold",
            "stage_openroad_asap7_setup",
            "stage_openroad_asap7_hold",
        }
        if not required_stages.issubset(self.stage_result_hashes):
            missing = sorted(required_stages - set(self.stage_result_hashes))
            raise ValueError(f"M2 sign-off is missing required stages: {', '.join(missing)}")
        if set(self.analysis_view_hashes) != {"asap7_setup", "asap7_hold"}:
            raise ValueError("M2 sign-off requires exactly the locked setup and hold views")
        required_tools = {"yosys", "opensta", "openroad", "eqy", "sby", "iverilog", "slang"}
        if not required_tools.issubset(self.tool_build_hashes):
            raise ValueError("M2 sign-off tool identity set is incomplete")
        for label, values in (
            ("analysis_view_hashes", self.analysis_view_hashes),
            ("stage_result_hashes", self.stage_result_hashes),
            ("raw_artifact_hashes", self.raw_artifact_hashes),
            ("tool_build_hashes", self.tool_build_hashes),
        ):
            if not values or tuple(values) != tuple(sorted(values)):
                raise ValueError(f"{label} must be nonempty and canonically ordered")
        measured_status = (
            "FAIL" if self.setup_wns_ns < 0.0 or self.hold_wns_ns < 0.0 else "PASS"
        )
        if self.timing_constraint_status != measured_status:
            raise ValueError("timing constraint status does not match measured setup/hold slack")
        expected_input_set_hash = canonical_sha256(
            {
                "commit_sha": self.commit_sha,
                "implementation_tree_hash": self.implementation_tree_hash,
                "m1_implementation_commit": self.m1_implementation_commit,
                "m1_signoff_invocation_hash": self.m1_signoff_invocation_hash,
                "m1_packet_hash": self.m1_packet_hash,
                "run_index_hash": self.run_index_hash,
                "benchmark_snapshot_hash": self.benchmark_snapshot_hash,
                "design_contract_hash": self.design_contract_hash,
                "platform_lock_hash": self.platform_lock_hash,
                "calibration_report_hash": self.calibration_report_hash,
                "calibration_validation_hash": self.calibration_validation_hash,
                "analysis_view_hashes": self.analysis_view_hashes,
                "stage_result_hashes": self.stage_result_hashes,
                "raw_artifact_hashes": self.raw_artifact_hashes,
                "tool_build_hashes": self.tool_build_hashes,
            }
        )
        if self.input_set_hash != expected_input_set_hash:
            raise ValueError("input_set_hash does not match the complete sign-off identity set")
        expected_report_hash = canonical_sha256(self, exclude=frozenset({"report_hash"}))
        if self.report_hash != expected_report_hash:
            raise ValueError("report_hash does not match canonical M2 sign-off report")
        return self


__all__ = [
    "BenchmarkConstraintContract",
    "BenchmarkConfig",
    "BenchmarkExpectations",
    "BenchmarkFile",
    "BenchmarkFormalManifest",
    "BenchmarkMasterDomain",
    "BenchmarkPowerWorkload",
    "BenchmarkSnapshot",
    "BenchmarkValidation",
    "BenchmarkValidationEvidence",
    "CalibrationReport",
    "CalibrationSample",
    "DEFAULT_MASTER_DOMAINS",
    "FULL_DIVIDER_RATIOS",
    "GeneratedClockExpectation",
    "GeneratedClockConstraint",
    "FormalHarnessEntry",
    "MappedCellTarget",
    "M2SignoffReport",
    "MasterClockConstraint",
    "PowerWorkloadPhase",
    "ReviewedConstraintException",
]
