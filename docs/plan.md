# NOVA-RTL Hackathon MVP and Milestone Implementation Plan

**Status:** Signed off for implementation after canonical-contract normalization and consistency review on 2026-08-14.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, judge-ready NOVA-RTL system that diagnoses multi-clock timing failures, proposes bounded RTL transformations with GenAI, applies them deterministically, proves a latency-preserving primary result with strict equivalence, measures multi-corner PPA, and replays every decision and artifact.

**Architecture:** The implementation follows the authoritative [integrated NOVA-RTL architecture](architecture.md). One deterministic orchestrator owns state, budgets, tools, transforms, acceptance, recovery, and Pareto selection; heuristic, single-agent, and bounded-council planners only return typed advisory objects. The critical path is a strict-equivalence vertical slice before multi-agent breadth.

**Tech Stack:** Conda with Python 3.11, Pydantic v2 typed models and JSON Schema export, Typer CLI, SQLite metadata, content-addressed filesystem artifacts, a pinned slang/pyslang SystemVerilog syntax-and-source-range backend, SystemVerilog, Yosys, OpenSTA, OpenROAD, EQY, SymbiYosys, Icarus Verilog or Verilator, pytest, Streamlit for the local demo, and an optional bounded LangGraph council adapter.

## Global Constraints

- The integrated architecture document is authoritative; this plan specializes its implementation order and schemas without weakening any invariant.
- The primary competition result is a latency-preserving candidate with `STRICT_SEQ_EQUIV=PASS`.
- The baseline SDC, effective constraint-binding manifest, required analysis views, functional RTL identity, clock graph, approved CDC structures, protection policy, and power-activity identity are immutable comparison inputs.
- Five master clocks are mutually asynchronous. Until organizer clarification is recorded, the benchmark contains twenty-one generated clocks per master and one hundred and five total.
- Generated clocks derive from protected divider state; formal does not model them as unrelated free clocks.
- Every required setup, hold, and physical view is explicit and hashed. Missing required-view evidence fails closed.
- Built-in checking is described as a structural CDC invariant audit, not commercial CDC sign-off.
- Agents have read-only evidence access. They cannot write RTL or SDC, execute EDA tools, approve candidates, change budgets, or relabel tool results.
- Only registered deterministic transforms may edit RTL, and every candidate is an immutable child in the candidate DAG.
- Official metrics come only from parsed EDA artifacts under matching view, activity, platform, recipe, and tool fingerprints.
- Unknown, timed-out, malformed, or incomplete mandatory results never become implicit passes.
- Every external dependency is pinned by digest or immutable version in Milestone 0 before benchmark numbers are published.
- Local Python development uses the `nova-rtl` Conda environment with Python 3.11; commands in this plan assume that environment is active.
- Registered transforms use parsed SystemVerilog syntax/source ranges and semantic preflights; regex-only RTL rewriting is prohibited.
- The repository is initialized on the `main` branch; implementation commits follow the milestone sequence in this plan.

---

## 1. Decisions Locked by This Plan

### 1.1 Implementation strategy

The build order is:

```text
Reproducible platform and schemas
        -> immutable artifacts and replay
        -> valid multi-clock baseline
        -> strict-equivalence vertical slice
        -> deterministic transform/search spine
        -> constrained single-agent planner
        -> typed deterministic recovery
        -> bounded role-scoped council
        -> full physical evaluation and judge-ready demo
```

This ordering is a hard dependency graph. Multi-agent orchestration is not allowed to become the first working path through the system.

### 1.2 Platform selection contract

Milestone 0 selects and pins exactly one disclosed OpenROAD platform. The selection algorithm is deterministic:

1. Use the organizer-provided platform when one is supplied and redistribution/use terms permit it.
2. Otherwise use an installed open OpenROAD-flow-scripts platform that provides at least one setup Liberty view, one hold Liberty view, LEF/technology data, RC setup, and a reproducible OpenROAD reference flow.
3. Prefer a platform with distinct slow/setup and fast/hold Liberty views.
4. Reject a platform candidate if either view cannot analyze the same synthesized design or if the physical flow cannot complete a smoke design.
5. Record the selected platform ID, absolute source paths, copied immutable artifact paths, SHA-256 hashes, container digest, tool versions, operating conditions, and redistribution notes in `config/platform/platform.lock.yaml`.

The plan contains no invented corner filenames. The lock task discovers real files and fails if the minimum corner contract cannot be met.

### 1.3 Organizer ambiguity contract

`config/challenge/organizer_decisions.yaml` is created with these effective defaults:

```yaml
schema_version: 1
generated_clock_interpretation:
  effective_value: 21_PER_MASTER
  masters: 5
  total_generated_clocks: 105
  source: CONSERVATIVE_INTERNAL_DEFAULT
psm_interpretation:
  effective_value: FSM_STATE_MACHINE_OPTIMIZATION
  source: CONSERVATIVE_INTERNAL_DEFAULT
organizer_response:
  status: NOT_RECEIVED
  evidence_artifact_id: null
override_rule: CONFIG_ONLY_WITH_NEW_RUN_ID
```

If the organizer later clarifies either term, only this versioned configuration, benchmark parameters, and the resulting run ID change. Prior runs remain replayable.

---

## 2. Hackathon MVP Cut Line

### 2.1 Tier 0: non-negotiable truth spine

If schedule pressure forces cuts, the following capabilities cannot be removed:

1. `nova doctor` validates the exact toolchain, platform, corners, solver availability, and filesystem capacity.
2. A normalized `ProjectManifest` and immutable run snapshot are created before tool execution.
3. The benchmark elaborates with five asynchronous master domains, the configured generated-clock count, approved CDC patterns, and a mapped-cell calibration report.
4. Baseline Yosys, required-view OpenSTA, constraint-binding, clock-lineage, structural CDC, formal-model preflight, EQY smoke, and at least placed/CTS OpenROAD stages complete.
5. Critical paths are parsed, clustered, mapped to RTL source spans, and emitted as typed `OptimizationOpportunity` objects.
6. At least one deterministic Tier-A transform executes from a typed proposal.
7. The candidate passes source authorization, binding/clock/CDC invariants, synthesis, full required-view STA, and `STRICT_SEQ_EQUIV`.
8. The exact delivered RTL is re-proved and remeasured.
9. Baseline and candidate reports are comparable by hash and include timing, area, cell count, and labeled power evidence.
10. Every run can be replayed without an LLM or EDA rerun.

If any item above is absent, the project is not a valid submission regardless of the sophistication of its agent UI.

### 2.2 Tier 1: judge-ready competition MVP

The intended hackathon submission adds all of the following on top of Tier 0:

- Four executable, latency-preserving transform capabilities:
  - `BALANCE_BOOLEAN_TREE`;
  - `RESTRUCTURE_PRIORITY_MUX`;
  - `FACTOR_COMMON_PREDICATE`; and
  - `FSM_DECODE_RESTRUCTURE`.
- A deterministic heuristic planner and a constrained single-agent planner behind the same `Planner` interface.
- One bounded council route containing two blinded proposer roles, a Formal critic, a PPA critic, and a chair that must disposition every critique.
- Common safety envelope plus private role-specific evidence packs, ID-scoped read-only retrieval, per-role token accounting, and snapshot-hash validation.
- Deterministic failure classification, checker-aware repair directives, lineage budgets, semantic duplicate/stagnation detection, and a visible path-migration recovery case.
- Candidate DAG, strict-contract Pareto frontier, hard feasibility policy, and at least one dominated-but-correct candidate.
- A recorded full-scale run and a small live run using identical event and artifact schemas.
- A report/UI path from headline metrics to raw timing, formal, binding, CDC, and physical artifacts.
- Equal-EDA-budget comparison of heuristic versus single-agent, plus a small council comparison. Claims are limited to measured evidence.

Tier 1 is the approved hackathon MVP cut line.

### 2.3 Conditional enhancements

These capabilities are implemented only after a green Tier-1 rehearsal bundle exists:

- `TIGHTEN_OPERAND_WIDTH` and `DUPLICATE_HIGH_FANOUT_DRIVER`.
- A separately labeled `RETIMING_EQUIV` candidate.
- One `INSERT_ELASTIC_PIPELINE` demonstration with transaction-level properties.
- Targeted multi-agent recovery beyond the deterministic path-migration scenario.
- Three physical seeds instead of one baseline/final comparison seed.
- The complete specialist roster rather than the minimum routed council.
- A service API beyond the local CLI/event stream used by the demo.

These features may improve judging impact, but none may delay or contaminate the primary strict-equivalence artifact.

### 2.4 Explicitly outside the hackathon MVP

- Unrestricted LLM-generated RTL patches.
- Automated SDC editing.
- Automated modification of dividers, clock gates, reset synchronizers, CDC synchronizers, or asynchronous FIFO internals.
- Production multi-tenant authentication, cloud scheduling, PostgreSQL, or object-store deployment.
- Commercial-quality CDC sign-off claims.
- Support for multiple simultaneous PDK/platform families.
- Recursive agent spawning, unbounded debate, or agent-owned global retry state.
- Full-chip equivalence for arbitrary latency-changing transformations.
- Training or fine-tuning a foundation model.

### 2.5 MVP acceptance gates

The competition MVP is releasable only when one recorded full run satisfies every hard gate below:

| Gate | Required evidence |
|---|---|
| Benchmark identity | Five master clocks; configured generated-clock total; clock lineage report; approved CDC inventory; mapped cell count within 45K–55K |
| Platform identity | Toolchain/container digest, real Liberty/LEF/RC files and hashes, analysis-view hashes, fixed recipes and seeds |
| Constraint integrity | Identical SDC hash; zero unresolved selectors; `ConstraintBindingManifest.comparison_to_baseline=EQUIVALENT` |
| CDC integrity | Zero new/unapproved crossings; zero changed approved CDC structures; scope labeled structural invariant audit |
| Formal identity | Functional/formal behavioral delta `NONE`; shared corresponding master events; protected derived-clock logic unchanged |
| Primary correctness | Exact delivered candidate has whole-design or compositionally closed `STRICT_SEQ_EQUIV=PASS` |
| Timing | Every required setup/hold view complete; candidate improves the selected target path and does not violate hard hold/setup policy |
| Physical | Candidate completes the declared finalist stage using the same platform/seed recipe and remains feasible |
| PPA | Baseline/final timing and area are comparable; power is compared only under identical activity identity |
| AI utility | At least two distinct typed hypotheses are traceable to evidence; at least one critique changes/rejects a proposal |
| Recovery utility | One real failed candidate becomes a typed failure and leads to a different, policy-valid next mechanism or explicit abandonment |
| Auditability | Selected RTL, patch, manifests, raw reports, proofs, ledger, events, and replay bundle resolve by hash |
| Demo resilience | Recorded main run replays offline; tiny live flow has a deterministic fallback |

Competitive targets, not correctness gates, are: at least 10% improvement in the target critical-path delay or equivalent Fmax, no more than 5% area growth, and a measured reduction in wasted formal/physical jobs from early rejection and recovery routing. If measurements do not reach these targets, the report shows the honest Pareto result.

### 2.6 Feature freeze rule

Once Tier 1 has one fully green rehearsal bundle, new features enter only when all of these conditions hold:

- the existing bundle remains replayable;
- the new feature has a separate branch/candidate contract;
- it does not change the primary candidate or its hashes;
- it has a bounded implementation and evaluation budget; and
- it can be removed without altering Tier-1 artifacts.

---

## 3. Repository and File Boundaries

The implementation creates this structure. Each file has one primary responsibility.

```text
pyproject.toml
README.md
Makefile
schemas/
  canonical/                       Exported `<name>.v<schema_version>.schema.json` files
config/
  challenge/organizer_decisions.yaml
  platform/platform.lock.yaml
  policy/default.yaml
  policy/recovery_rules.yaml
  policy/cdc_patterns.yaml
  analysis/views.yaml
  formal/reset_assumptions.yaml
src/nova_rtl/
  cli.py                           Command surface only
  contracts/
    base.py                        IDs, hashes, timestamps, strict-model rules
    platform.py                    Tool fingerprints, doctor and platform-lock contracts
    manifest.py                    Project and policy contracts
    execution.py                   Tool jobs, prepared commands and raw execution results
    analysis.py                    Views, activity, metrics, paths, evidence
    optimization.py                Opportunities, proposals, candidates
    verification.py                Bindings, CDC, formal and proof results
    planning.py                    Planner and council contracts
    recovery.py                    Failures, directives, fingerprints, decisions
    reporting.py                   Report-bundle and claim-to-evidence contracts
    events.py                      Run events and replay payloads
    migrations.py                  Explicit legacy schema migrations only
    schema_export.py               Deterministic JSON Schema exporter
  artifacts/
    store.py                       Content-addressed immutable blobs
    ledger.py                      Append-only JSONL/SQLite experiment ledger
    replay.py                      Event ordering and artifact resolution
  orchestrator/
    state.py                       Run/candidate transition tables
    runner.py                      One durable global workflow owner
    scheduler.py                   Bounded tool and planner job scheduling
  analysis_views/
    comparability.py               View/activity identity checks
    aggregation.py                 Worst-required-view metric aggregation
  adapters/
    base.py                        Tool adapter protocol
    yosys.py                       Synthesis runner/parser
    opensta.py                     Required-view STA runner/parser
    openroad.py                    Physical runner/parser
    eqy.py                         Strict equivalence runner/parser
    sby.py                         Property runner/parser
    simulation.py                  Activity and functional simulation
  platform/
    doctor.py                      Dependency and platform checks
    lock.py                        Real-file fingerprinting and lock creation
  benchmark/
    generator.py                   Parameterized RTL manifest generation
    calibrate.py                   Cell-count calibration loop
    validate.py                    Clock/CDC/view/cell-count checks
  constraints/
    binding.py                     Selector resolution and manifest comparison
    clocks.py                      Master/generated clock inventory and lineage
  cdc/
    inventory.py                   Structural crossings and pattern recognition
    compare.py                     Baseline/candidate invariant comparison
  formal/
    preflight.py                   Functional/formal elaboration identity
    harness.py                     Shared clock/reset environment generation
    compose.py                     Partition coverage/composition manifest
  evidence/
    graph.py                       Normalized design evidence graph
    paths.py                       Path parser and clustering
    source_map.py                  Mapped object to RTL span mapping
    opportunities.py               Root-cause and opportunity formation
  transforms/
    registry.py                    Capability metadata and lookup
    executor.py                    Authorized isolated source edits
    boolean_tree.py                BALANCE_BOOLEAN_TREE
    priority_mux.py                RESTRUCTURE_PRIORITY_MUX
    predicate.py                   FACTOR_COMMON_PREDICATE
    fsm_decode.py                  FSM_DECODE_RESTRUCTURE
  evaluation/
    cascade.py                     Candidate gate state machine
    feasibility.py                 Hard acceptance predicate
    metrics.py                     Comparable metric aggregation
    frequency.py                   Controlled period-sweep evaluation
  search/
    dag.py                         Immutable candidate lineage
    controller.py                  Bounded search and stop policy
    pareto.py                      Contract-partitioned Pareto archive
  planner/
    interface.py                   Planner protocol
    heuristic.py                   Deterministic baseline planner
    single_agent.py                Schema-constrained agent planner
    context.py                     Safety envelope/private packs/retrieval
    council.py                     Bounded role graph and chair fan-in
  recovery/
    classifier.py                  StageResult -> FailureEvent
    compiler.py                    FailureEvent -> RepairDirective
    fingerprint.py                 Semantic candidate/failure fingerprint
    policy.py                      Budgets and monotonic escalation
    router.py                      Authoritative RecoveryDecision
  reports/
    bundle.py                      Evidence package and comparison tables
  ui/
    app.py                         Local live/replay application
tests/
  unit/                            Pure contract, parser, policy and transform tests
  golden/                          Pinned raw EDA report fixtures
  integration/tiny/               Seconds-scale strict vertical slice
  integration/small/              Two-clock CDC and planner/recovery flow
  integration/full/               Five-master full acceptance wrapper
benchmark/
  rtl/                             Generated and hand-maintained SV
  constraints/                     Immutable SDC
  formal/                          Equivalence/protocol harnesses
  sim/                             Testbench and activity workload
  expected/                        Expected clock, CDC and sizing manifests
  recovery_cases/                 Labeled failure scenarios
runs/                              Git-ignored immutable run artifacts
```

Boundary rules:

- `contracts/` imports no adapter, planner, transform, UI, or orchestration module.
- `adapters/` return typed contracts and artifact references; they do not choose recovery or acceptance.
- `planner/` can read only an authorized evidence provider and cannot import adapter runners or the transform executor.
- `transforms/` can materialize changes but cannot mark them correct or feasible.
- `ui/` reads events and report models; it never parses raw tool output.
- `runs/` is append-only during a run. Candidate repair creates a new directory and ID.

---

## 4. Canonical Schema Rules

### 4.1 Serialization and validation policy

All persisted contracts obey these rules:

- JSON is canonical storage; YAML is accepted only for human-authored configuration and normalized to canonical JSON.
- Models reject unknown fields (`extra=forbid`) except a versioned `extensions` map explicitly declared by that schema.
- `schema_version` is an integer. `ProjectManifest`, `StageResult`, and `OptimizationProposal` use version `2`; every other top-level persisted contract in the canonical registry begins at version `1`.
- IDs match `^[a-z][a-z0-9_]{2,95}$` and are unique within their entity type and run.
- Content hashes match `^sha256:[0-9a-f]{64}$`.
- Artifact URIs match `^artifact://[a-zA-Z0-9._/-]+$` and must resolve in the run artifact index.
- Timestamps are RFC 3339 UTC with a trailing `Z`.
- Enumerations serialize as uppercase stable strings.
- Numeric timing uses nanoseconds, frequency uses megahertz, area uses square micrometres, power uses microwatts, memory uses bytes, and duration uses milliseconds. Unit suffixes are part of field names.
- A metric unavailable from a completed stage is `null` and requires a same-key entry in `missing_metric_reasons`.
- Empty strings, NaN, positive/negative infinity, negative byte counts, negative durations, and duplicate IDs are invalid.
- Official comparisons require identical hashes for platform, recipe, analysis-view set, functional source inputs, effective constraint intent, and activity contract where power is compared.
- Schema migration creates a new object and records the source object hash; persisted objects are never rewritten in place.

#### Canonical contract and interface registry

The registry in Architecture Section 8.0 is authoritative. This plan uses exactly the following top-level schema names and versions:

| Version 2 | Version 1 |
|---|---|
| `ProjectManifest` | `PlatformLock`, `DesignContract`, `AnalysisViewContract`, `PowerActivityContract`, `FrequencySweepContract` |
| `StageResult` | `ToolFingerprint`, `DoctorReport`, `ToolJob`, `PreparedCommand`, `RawToolResult`, `CriticalPathRecord`, `ClockInventory`, `CDCInventory`, `EvidenceGraphSnapshot` |
| `OptimizationProposal` | `OptimizationOpportunity`, `CandidateRecord`, `ConstraintBindingManifest`, `FormalModelContract`, `ProofResult` |
|  | `PlannerRequest`, `PlannerResult`, `ContextRequest`, `RoleContextPack`, `ProviderResult`, `DiagnosisReport`, `TransformRecommendation` |
|  | `CritiqueReport`, `CritiqueDisposition`, `ProposalShortlist`, `CouncilTrace`, `CouncilRequest`, `CouncilResult` |
|  | `FailureEvent`, `RepairDirective`, `CandidateFailureFingerprint`, `RecoveryRoutePlan`, `RecoveryRequest`, `RecoveryAdvice`, `RecoveryDecision` |
|  | `ExperimentRecord`, `ParetoRecord`, `SearchRequest`, `SearchResult`, `ReportBundle`, `RunEvent` |

Reusable component schemas are `ArtifactRef`, `EvidenceRef`, `Diagnostic`, `DoctorCheck`, `StageInputHashes`, and `MetricSet`; each has exported component-schema version `1`. Embedded instances omit their own `schema_version` because the containing top-level contract fixes their interpretation. New serialized data must not use an alias. `StageResult` version 1 is accepted only by `StageResultV1ToV2` during legacy ingestion and is immediately persisted as version 2.

Canonical public names are `ArtifactStore`, `ExperimentLedger`, `RunOrchestrator`, `BoundedScheduler`, `ToolAdapter`, `Planner`, `EvidenceProvider`, `StructuredModelProvider`, `CouncilRuntime`, `TransformCapability`, `RecoveryAdvisor`, `SearchController`, and `ParetoArchive`. Their boundary types are `JobRequest`, `JobHandle`, `ToolJob`, `PreparedCommand`, `RawToolResult`, `PlannerRequest`, `PlannerResult`, `ContextRequest`, `RoleContextPack`, `ProviderResult`, `CouncilRequest`, `CouncilEventSink`, `CouncilResult`, `RecoveryRequest`, `RecoveryAdvice`, `SearchRequest`, and `SearchResult`.

`JobRequest`, `JobHandle`, and `CouncilEventSink` are canonical runtime-only boundary types, not persisted schemas. `JobRequest` wraps the ID and resource policy for a persisted work contract; `JobHandle` is an in-process asynchronous completion handle; and `CouncilEventSink` is a protocol that emits persisted `RunEvent` objects. They never appear in `SCHEMA_REGISTRY`. Every other boundary type in the preceding sentence is a versioned persisted schema listed above.

Only names in the persisted-schema, reusable-component, public-interface, and runtime-boundary registries above are canonical public contracts. Capitalized helper types used in milestone pseudocode are module-local implementation types unless a future architecture decision explicitly promotes them into this registry.

Forbidden new-code aliases are `PlanningRequest`, `RecoveryAdviser`, bare `Ledger`, `ToolRequest`, `ExecutionArtifacts`, and `build_command`. Tests include a source scan that rejects these aliases outside the single legacy-migration module.

### 4.2 Common primitives

#### `ArtifactRef`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `artifact_id` | ID string | yes | Unique within run |
| `uri` | artifact URI | yes | Resolvable in content-addressed store |
| `sha256` | hash string | yes | Hash of exact bytes |
| `media_type` | string | yes | MIME-like value such as `application/json` or `text/plain` |
| `size_bytes` | integer | yes | At least zero |
| `created_at` | timestamp | yes | UTC |
| `producer_stage_result_id` | ID or null | yes | Null for snapshotted input |
| `classification` | enum | yes | `PUBLIC`, `INTERNAL`, or `RESTRICTED_RTL` |

#### `EvidenceRef`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `evidence_id` | ID string | yes | Stable semantic evidence ID |
| `kind` | enum | yes | `PATH`, `ARC`, `CONE`, `SOURCE_SPAN`, `CLOCK`, `CDC`, `BINDING`, `PHYSICAL`, `FORMAL`, `METRIC`, or `HISTORY` |
| `artifact_id` | ID string | yes | Must resolve |
| `json_pointer` | string or null | yes | RFC 6901 pointer when evidence is a subobject |
| `snapshot_hash` | hash string | yes | Must match the authorized run/candidate snapshot |

#### `Diagnostic`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `code` | uppercase stable string | yes | Registered diagnostic code |
| `severity` | enum | yes | `INFO`, `WARNING`, `ERROR`, or `FATAL` |
| `message` | string | yes | Human-readable, non-authoritative explanation |
| `evidence_refs` | list of IDs | yes | Empty allowed only for infrastructure diagnostics |

#### `ToolFingerprint`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `schema_version` | integer | yes | `1` |
| `tool_id` | ID string | yes | Logical tool name such as `opensta`; not assumed to equal executable basename |
| `executable` | absolute path | yes | Resolved executable actually invoked |
| `version` | string | yes | Parsed tool version |
| `build_hash` | hash string | yes | Version/build-output and executable identity hash |
| `adapter_version` | string | yes | Parser/adapter implementation version |
| `container_digest` | hash or null | yes | Required for containerized runs; null for a pinned native installation |

### 4.3 `ProjectManifest`

`ProjectManifest` is the single human-authored run entry point.

| Field | Type | Required | Key validation |
|---|---|---:|---|
| `schema_version` | integer | yes | Equals architecture version `2` |
| `project` | ID string | yes | Forms run namespace |
| `top` | HDL identifier | yes | Must elaborate exactly once |
| `rtl.files` | nonempty list of paths | yes | Paths resolve under project root; expansion is sorted and snapshotted |
| `rtl.include_dirs` | list of paths | yes | Paths resolve under project root |
| `rtl.defines` | list of strings | yes | No define may conflict with formal property-only define |
| `compilation_profiles.functional` | object | yes | Source of synthesis/STA behavior |
| `compilation_profiles.formal` | object | yes | May add only properties, assumptions, covers, binds and harness wiring |
| `constraints` | object | yes | SDC path, expected clock counts, zero-unconstrained and binding policies |
| `technology` | object | yes | References one `platform.lock.yaml` hash |
| `analysis_views` | nonempty list | yes | At least one required setup and one required hold view |
| `power_activity` | object | yes | Explicit activity or `ESTIMATED_ONLY_NOT_COMPARABLE` fallback |
| `cdc` | object | yes | Approved pattern registry and invariant policy |
| `formal` | object | yes | Primary contract must be `STRICT_SEQ_EQUIV` |
| `protection` | object | yes | Protected modules and path patterns |
| `optimization` | object | yes | Candidate, area and objective budgets |
| `planner` | object | yes | Mode, fallback, proposal and token limits |
| `context_isolation` | object | yes | Private-pack and retrieval policy |
| `recovery` | object | yes | Policy version, similarity and depth budgets |
| `physical` | object | yes | Finalist counts and deterministic seeds |

Cross-field validators enforce five expected masters; the effective organizer decision for generated clocks; one `STRICT_SEQ_EQUIV` primary lane; distinct required setup/hold view IDs; immutable constraints; no protected path in an editable allowlist; and a candidate limit at least as large as the physical-finalist count.

#### `PlatformLock` and normalized `DesignContract`

`PlatformLock` contains the platform ID; container image/digest; tool fingerprints; setup/hold Liberty, LEF, RC and OpenROAD-recipe `ArtifactRef` objects; discovered operating conditions; deterministic seed policy; host-compatibility facts; and the hash of the selection policy. It is valid only when every referenced byte is present and rehashes correctly.

`DesignContract` is the machine-generated immutable form consumed downstream. It contains `design_contract_id`, `run_id`, `project_manifest_hash`, sorted functional source artifacts, top/parameters/defines, functional and formal compilation profile hashes, constraint snapshot artifact/hash, analysis-view IDs and set hash, power-activity contract ID/hash, platform-lock hash, CDC/protection/formal/policy IDs and hashes, organizer-decision hash, creation timestamp, and its own canonical hash. No downstream job expands source globs or rereads mutable project configuration after this object is sealed.

### 4.4 `AnalysisViewContract` and `PowerActivityContract`

`AnalysisViewContract` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `schema_version` | integer | yes | `1` |
| `analysis_view_id` | ID | yes | Stable within run |
| `mode` | enum | yes | `FUNCTIONAL` for MVP |
| `check` | enum | yes | `SETUP`, `HOLD`, or `POWER` |
| `required` | boolean | yes | Required views participate in hard feasibility |
| `liberty_corner.id` | ID | yes | Resolves in platform lock |
| `liberty_corner.artifact_hash` | hash | yes | Exact Liberty bytes |
| `rc_corner.id` | ID | yes | Resolves in platform lock |
| `rc_corner.artifact_hash` | hash | yes | Exact RC configuration bytes |
| `sdc_hash` | hash | yes | Must match run constraint snapshot |
| `operating_condition` | string | yes | Must exist or be intentionally selected by adapter |
| `derate_policy_hash` | hash | yes | Exact derate policy |
| `clock_uncertainty_policy_hash` | hash | yes | Exact uncertainty policy |
| `hard_limits` | metric-name to number map | yes | At least the WNS limit for the view check |
| `required_stages` | nonempty stage list | yes | MVP setup/hold include `OPENSTA_FULL` and physical finalist stages |
| `power_activity_contract_id` | ID or null | yes | Required for official power view |

`PowerActivityContract` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `power_activity_contract_id` | ID | yes | Stable within run |
| `format` | enum | yes | `VCD`, `SAIF`, or `VECTORLESS` |
| `source_artifact` | `ArtifactRef` or null | yes | Required for VCD/SAIF |
| `scope` | hierarchy string | yes | Must resolve in simulated design |
| `time_window_ns` | two-number tuple | yes | End greater than start |
| `propagation_policy` | enum | yes | `ANNOTATED`, `PROPAGATED`, or `VECTORLESS_ESTIMATE` |
| `comparability` | enum | yes | `OFFICIAL_COMPARABLE` or `ESTIMATED_ONLY_NOT_COMPARABLE` |
| `contract_hash` | hash | yes | Hash excludes only this field |

Power values can enter official baseline/candidate comparison only when both contracts are `OFFICIAL_COMPARABLE` and the complete contract hashes match.

#### `FrequencySweepContract`

Frequency sweep is a deterministic evaluation experiment, never an agent/optimizer SDC edit. The contract contains `frequency_sweep_contract_id`, baseline analysis-view hash, target master clock/domain, ordered trial periods in nanoseconds, fixed non-target clock definitions, setup/hold pass limits, deterministic constraint-overlay generator hash, search method (`BOUNDED_BINARY` for MVP), maximum trials, and result-label policy. Baseline and candidate use the same sweep contract; every trial is stored as a distinct view-keyed `StageResult`. The maximum passing frequency is labeled achieved-by-sweep only when both setup and hold policies pass and all non-target constraints remain identical. Otherwise `estimated_fmax_mhz` remains explicitly estimated.

### 4.5 Evidence schemas

#### `CriticalPathRecord`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `path_id` | ID | yes | Stable for snapshot/view |
| `candidate_id` | ID | yes | `baseline` is allowed |
| `analysis_view_id` | ID | yes | Required setup/hold view |
| `path_group` | string | yes | Parsed STA group |
| `launch_clock_id` | ID | yes | Resolves in clock inventory |
| `capture_clock_id` | ID | yes | Resolves in clock inventory |
| `startpoint` | string | yes | Canonical mapped object |
| `endpoint` | string | yes | Canonical mapped object |
| `arrival_ns` | number | yes | Finite |
| `required_ns` | number | yes | Finite |
| `slack_ns` | number | yes | Consistent with required-arrival within parser tolerance |
| `cell_delay_ns` | number | yes | At least zero |
| `net_delay_ns` | number | yes | At least zero |
| `logic_depth` | integer | yes | At least zero |
| `max_fanout` | integer | yes | At least zero |
| `object_sequence` | nonempty list | yes | Ordered cells/pins/nets |
| `source_span_refs` | list of IDs | yes | Resolvable source-map evidence |
| `raw_report_artifact_id` | ID | yes | Exact supporting report |

#### `ClockInventory`

Contains `candidate_id`, five master clock entries, generated-clock entries, lineage edges, waveform/ratio definitions, active-consumer counts, and `clock_graph_hash`. Validation requires exactly the configured master count; exactly the configured generated count per master; one master ancestor per generated clock; nonempty active consumers; and no undeclared cross-master synchronous relationship.

#### `CDCInventory`

Contains `candidate_id`, `crossings[]`, `approved_pattern_registry_hash`, `inventory_hash`, and comparison counts. Each crossing has source/destination domain, source/destination object, signal class, recognized pattern, structural fingerprint, protocol-property refs, and status. Candidate acceptance requires `new_unapproved_count=0`, `changed_approved_structure_count=0`, and no `AMBIGUOUS` crossing.

#### `EvidenceGraphSnapshot`

Contains the snapshot hash; node and edge schema versions; counts by node/edge type; artifact reference to compressed graph data; source-map artifact; path-record artifact; clock and CDC inventory IDs; and evidence-resolution index hash. The compressed graph is immutable; planners see authorized subgraphs, never the whole raw graph by default.

### 4.6 `MetricSet` and `StageResult`

`MetricSet` is shared by baseline, candidates, Pareto records, and reports.

| Field | Type | Required | Rule |
|---|---|---:|---|
| `analysis_view_id` | ID or null | yes | Non-null for view metrics |
| `setup_wns_ns`, `setup_tns_ns` | number or null | yes | Setup fields required for completed setup stage |
| `hold_wns_ns`, `hold_tns_ns` | number or null | yes | Hold fields required for completed hold stage |
| `failing_endpoints` | integer or null | yes | At least zero |
| `critical_path_delay_ns` | number or null | yes | Positive when present |
| `estimated_fmax_mhz` | number or null | yes | Labeled estimated unless from period sweep |
| `mapped_area_um2` | number or null | yes | At least zero |
| `physical_area_um2` | number or null | yes | At least zero |
| `cell_count`, `register_count`, `buffer_count` | integer or null | yes | At least zero |
| `power_total_uw` | number or null | yes | Requires the containing result/record to identify a comparable `PowerActivityContract` hash |
| `wirelength_um` | number or null | yes | At least zero |
| `congestion_overflow` | number or null | yes | At least zero |
| `runtime_ms` | integer | yes | At least zero |
| `missing_metric_reasons` | map | yes | Exactly covers unavailable expected fields |

`StageInputHashes` has named nullable hash fields for `rtl_snapshot`, `design_contract`, `constraints`, `constraint_binding`, `analysis_view`, `power_activity`, `platform_lock`, `tool_recipe`, `formal_model`, and `parent_stage_result`, plus a strict `extensions` map for registered stage-specific identities. A stage validator makes every applicable identity non-null; arbitrary string-to-hash maps are not accepted because they make cross-stage comparability ambiguous.

`StageResult` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `schema_version` | integer | yes | `2` |
| `stage_result_id`, `run_id`, `candidate_id` | IDs | yes | Resolve to run/candidate |
| `stage` | enum | yes | Registered gate or tool stage |
| `analysis_view_id` | ID or null | yes | Non-null for view-specific stages |
| `status` | enum | yes | `PASS`, `FAIL`, `INCONCLUSIVE`, `INFRASTRUCTURE_ERROR` |
| `tool_fingerprint` | `ToolFingerprint` | yes | Logical tool, real executable, version/build, adapter and container identity |
| `input_hashes` | `StageInputHashes` | yes | RTL, constraint, binding, view, activity, recipe and platform identities as applicable |
| `metrics` | `MetricSet` | yes | No predicted values |
| `diagnostics` | list of `Diagnostic` | yes | At least one for non-pass |
| `raw_artifacts` | nonempty list of `ArtifactRef` | yes | Logs and reports preserved |
| `started_at`, `ended_at` | timestamps | yes | End not earlier than start |

A process exit code of zero is insufficient for `PASS`; the adapter must also prove required report sections and metrics are present.

#### Legacy `StageResult` version-1 ingestion

Version 1 is not a second live contract and is never emitted by an adapter, planner, event, ledger or API. `StageResultV1ToV2` accepts it only with a `LegacyMigrationContext` that pins the source-object hash, artifact index, platform/tool fingerprints, stage expectation matrix, diagnostic-code registry, and legacy field-alias table.

| Version-1 shape | Required version-2 normalization | Fail-closed condition |
|---|---|---|
| Tool name/version strings | Resolve one exact `ToolFingerprint` from the pinned run/platform manifest | No match or more than one match |
| Arbitrary input-hash map and historical aliases | Map registered aliases into the named `StageInputHashes` fields; preserve registered extension hashes | Unknown alias, missing applicable identity, or conflicting hashes |
| Partial metrics map | Build a complete `MetricSet`; add null plus a registered stage-specific reason for each metric the source stage cannot produce | Missing metric that the stage was required to produce, unknown unit, NaN/infinity, or unregistered metric key |
| String or minimal-map diagnostics | Resolve a registered diagnostic code/severity and attach resolvable evidence IDs | Unknown code, severity conflict, or required evidence cannot be resolved |
| Artifact URI strings | Resolve each URI through the immutable artifact index into a complete `ArtifactRef` | URI missing, hash/size mismatch, ambiguous URI, or producer identity conflict |
| Legacy timestamps/status | Normalize UTC timestamps and validate against the canonical status enum and stage expectations | Invalid ordering, unknown status, or a legacy `PASS` lacking required reports/metrics |

The migration emits a `RunEvent` payload containing `source_schema_version=1`, the source-object hash, and migration-adapter identity. Registered `StageInputHashes.extensions` keys carry the source-object and migration-adapter hashes in the migrated result. The result is then validated exactly like a native version-2 object. Migration never guesses a metric, invents evidence, converts an unknown status to a pass, or rewrites the source object.

### 4.7 Optimization schemas

#### `OptimizationOpportunity`

Required fields are `opportunity_id`, `parent_candidate_id`, `target_domain`, `target_analysis_view_id`, `affected_analysis_view_ids`, `root_causes`, `severity`, `editability`, `source_spans`, `protected_neighbors`, `eligible_transform_families`, `proof_contracts`, and `evidence_refs`. Root causes contain a registered category and confidence in `[0,1]`. `severity` contains worst view, worst slack, affected endpoint count, and TNS share. An editable opportunity requires at least one authorized source span, one eligible family, one proof contract, and no protected node in the edit set.

#### `OptimizationProposal`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `schema_version` | integer | yes | `2` |
| `proposal_id`, `parent_candidate_id`, `opportunity_id` | IDs | yes | Must resolve in same snapshot |
| `diagnosis_refs` | nonempty ID list | yes | Validated advisory diagnoses |
| `target.hierarchy` | string | yes | Resolves in parent snapshot |
| `target.source_span_id` | ID | yes | Authorized editable span |
| `target.cone_fingerprint` | fingerprint string | yes | Equals current cone fingerprint |
| `transformation.operation` | enum | yes | Registered operation |
| `transformation.family` | enum | yes | Matches registry entry |
| `transformation.parameters` | strict object | yes | Validates against operation parameter schema |
| `preconditions` | nonempty enum list | yes | Superset of registry-required preconditions |
| `correctness.contract` | enum | yes | Matches registry contract |
| `correctness.proof_scope` | string | yes | Whole design or closed partition target |
| `correctness.reset_model` | enum | yes | Matches design contract |
| `prediction` | advisory object | yes | Direction/confidence only; not official metrics |
| `evidence_refs` | nonempty evidence list | yes | Snapshot hashes must match |
| `abort_conditions` | nonempty registered-code list | yes | Deterministically evaluable |

#### `CandidateRecord`

Contains `candidate_id`, `run_id`, `parent_candidate_id`, `lineage_depth`, `proposal_id`, `opportunity_id`, `rtl_snapshot_artifact`, `patch_artifact`, `source_hash`, `changed_spans`, `transform_fingerprint`, `required_correctness_contract`, ordered `stage_result_ids`, per-view metrics, proof result ID, binding/clock/CDC inventory IDs, classification, terminal disposition, created timestamp, and recovery-parent failure ID. A candidate has exactly one parent, cannot overwrite its parent artifacts, and cannot become `FEASIBLE_PARETO` or `SELECTED` until the hard feasibility predicate passes.

Candidate classifications are `REJECTED_SAFETY`, `REJECTED_CORRECTNESS`, `REJECTED_POLICY`, `INCONCLUSIVE`, `INFRASTRUCTURE_ERROR`, `VALID_NEGATIVE_RESULT`, `FEASIBLE_DOMINATED`, `FEASIBLE_PARETO`, and `SELECTED`. Selection classes are `PRIMARY_STRICT`, `SECONDARY_RETIMED`, and `EXPLORATORY_LATENCY_AWARE`.

### 4.8 Verification schemas

#### `ConstraintBindingManifest`

Required fields are `binding_manifest_id`, `candidate_id`, `sdc_hash`, `netlist_snapshot_hash`, `analysis_view_ids`, `resolved_commands`, endpoint `coverage`, `effective_binding_hash`, `comparison_to_baseline`, and `reviewed_mapping_refs`. Each resolved command contains the normalized command hash and sorted resolved object IDs plus their set hash. `coverage.timed_endpoints + reviewed_exception_endpoints` must equal `sequential_endpoints_total`; unresolved selectors must be zero. MVP candidates accept only `EQUIVALENT`.

#### `FormalModelContract`

Required fields are `formal_model_contract_id`, `candidate_id`, functional RTL and parameter hashes, gold/gate snapshot hashes, property manifest hash, master/generated clock models, multiclock flag, reset and environment assumption hashes, proof-scope policy, and `behavioral_elaboration_delta`. The MVP requires `INDEPENDENT_SHARED_GOLD_GATE_EVENTS`, `DERIVED_FROM_PROTECTED_DIVIDER_STATE`, `multiclock_enabled=true`, `WHOLE_DESIGN_OR_COMPOSITIONALLY_CLOSED`, and `behavioral_elaboration_delta=NONE`.

#### `ProofResult`

| Field | Type | Required | Rule |
|---|---|---:|---|
| `proof_result_id`, `run_id`, `candidate_id` | IDs | yes | Same candidate snapshot |
| `contract` | enum | yes | `STRICT_SEQ_EQUIV`, `RETIMING_EQUIV`, or `LATENCY_AWARE` |
| `outcome` | enum | yes | `PASS`, `FAIL`, `INCONCLUSIVE`, `INFRASTRUCTURE_ERROR` |
| `formal_model_contract_id` | ID | yes | Resolves and passes preflight |
| `gold_hash`, `gate_hash` | hashes | yes | Exact proved artifacts |
| `proof_scope` | enum | yes | `WHOLE_DESIGN` or `COMPOSITIONALLY_CLOSED` for primary |
| `partitions` | nonempty list | yes | Each has status, runtime, strategy and artifact refs |
| `composition_manifest_artifact_id` | ID or null | yes | Required for compositional scope |
| `assumption_hashes` | list of hashes | yes | Complete set |
| `counterexample_artifact_id` | ID or null | yes | Required when available on `FAIL` |
| `raw_artifacts` | nonempty artifact list | yes | Configs, logs, traces and reports |
| `runtime_ms` | integer | yes | At least zero |

Only `PASS` satisfies a mandatory correctness gate. A local proof without a closed composition manifest is `INCONCLUSIVE` for final acceptance.

### 4.9 Planner and council schemas

#### `PlannerResult`

Contains `planner_result_id`, `run_id`, `opportunity_id`, `planner_mode`, `status`, ordered validated `proposal_ids`, rejected-output diagnostics, context-pack hashes, optional `council_result_id`, provider/model/prompt/schema fingerprints, token counts, latency, and fallback used. Status is `PASS`, `NO_SAFE_PROPOSAL`, `PARTIAL`, `PROVIDER_ERROR`, or `SCHEMA_ERROR`. `PASS` requires at least one validated proposal. A fallback result retains the failed upstream result rather than replacing it.

#### `CouncilResult`

Contains:

- `council_result_id`, `council_request_id`, run/opportunity/snapshot IDs and policy hash;
- `route_reason_codes` and selected role IDs;
- one common safety-envelope hash;
- private role records containing role ID, private-pack hash, retrieval log, blinded-round flag, structured submission, token use, latency and status;
- normalized proposal cards with author identity removed from critic-facing content;
- Formal and PPA critiques with evidence refs, severity, objection codes, and requested disposition;
- chair dispositions of `ACCEPT`, `REVISE`, or `REJECT` for every critique;
- at most one revision record;
- final ordered proposal IDs;
- total budget consumed and deadline outcome; and
- trace-completeness percentage.

Validation requires two independently authored proposal mechanisms for the showcased council path, both mandatory critics, no private-pack leakage between proposer roles, every critique dispositioned, one revision maximum, valid evidence IDs, and aggregate tokens no greater than policy.

Council subcontracts are also exported:

- `DiagnosisReport`: role, opportunity, snapshot, ranked root causes, editable/safety assessment, evidence-backed claims, uncertainties and `NO_RTL_ACTION`/`INSUFFICIENT_EVIDENCE` option.
- `TransformRecommendation`: diagnosis ID, registered family/operation, target, strict parameters, preconditions, contract, abort conditions, evidence and expected structural direction.
- `CritiqueReport`: critic role, neutral proposal-card ID, typed objections with severity, semantic/PPA category, evidence, and requested disposition.
- `CritiqueDisposition`: objection ID, `ACCEPT`/`REVISE`/`REJECT`, reason code, resulting proposal ID or null, and chair evidence refs.
- `ProposalShortlist`: `PASS`, `NO_SAFE_PROPOSAL`, `INSUFFICIENT_EVIDENCE` or `PARTIAL`; zero to three ordered validated proposal IDs; fallback eligibility; and unresolved mandatory finding count, which must be zero before execution.
- `CouncilTrace`: selected roles, fan-out/fan-in events, prompt/model/context/retrieval hashes, token/latency use, deadline/cancellation state and trace-completeness score.

#### `RecoveryAdvice`

`RecoveryAdvice` is explicitly advisory and separate from `RecoveryDecision`. It contains `recovery_advice_id`, failure/directive IDs, `advisor_mode`, selected role, proposed action, proposed parent, proposed excluded families, rationale codes, evidence refs, uncertainty, token/latency data, and output hash. It cannot alter budgets, failure family, stage status, or protected policy.

### 4.10 Failure and recovery schemas

#### `FailureEvent`

Required fields follow Architecture Section 8.4: subject/run/entity IDs; failed stage and optional view; failure family, scope, repairability, severity and retryability; verified constraint/view/binding/protected statuses; typed metric delta; evidence and raw stage result; classifier version. Entity IDs may be null only when the entity did not yet exist. Deterministic code, not an agent, owns `failure_family`, `repairability`, and `retryable`.

The canonical failure families are `BASELINE_INPUT_ERROR`, `ANALYSIS_VIEW_OR_ACTIVITY_MISMATCH`, `INFRASTRUCTURE_TRANSIENT`, `ADAPTER_OR_PARSER_ERROR`, `CONSTRAINT_BINDING_DELTA`, `CDC_INVARIANT_DELTA`, `FORMAL_MODEL_MISMATCH`, `INVALID_PROPOSAL_SCHEMA`, `UNKNOWN_OR_INAPPLICABLE_TRANSFORM`, `PROTECTED_STRUCTURE_VIOLATION`, `RTL_PARSE_OR_ELAB_FAILURE`, `FAST_SYNTH_STRUCTURAL_FAILURE`, `FORMAL_SEMANTIC_FAILURE`, `FORMAL_INCONCLUSIVE`, `TIMING_NO_GAIN`, `TIMING_REGRESSION`, `CRITICAL_PATH_MIGRATION`, `HOLD_REGRESSION`, `AREA_POLICY_VIOLATION`, `POWER_POLICY_VIOLATION`, `PHYSICAL_CORRELATION_MISS`, `CONGESTION_OR_ROUTABILITY_RISK`, `REPEATED_NON_PROGRESS`, and `VALID_BUT_DOMINATED`.

#### `RepairDirective`

Contains directive/failure IDs; allowed scope; nonempty `observed`, `preserve`, and `prohibit` lists; recommended actions and roles; evidence references; and compiler-rule references. Every preserve/prohibit token is registered and machine-checkable. A directive never changes the failure classification.

#### `CandidateFailureFingerprint`

Contains candidate ID, target-cone fingerprint, operation family and operation, parameter/AST-diff/mapped-delta fingerprints, ancestor lineage, failure family, optional formal-counterexample fingerprint, and metric-response class. Similarity is computed from these normalized features; raw proposal text is not an input.

#### `RecoveryDecision`

Contains decision/failure IDs, authoritative action, next parent or null, reasoning-invocation flag, selected roles, excluded families, remaining family/lineage budgets, reason codes, policy version, optional advice ID, and decision hash. Actions are `RETRY_INFRASTRUCTURE`, `REPAIR_SCHEMA`, `CORRECT_EXECUTOR_OUTPUT`, `LOCAL_PARAMETER_REVISION`, `NARROW_TRANSFORM_SCOPE`, `SWITCH_OPERATION_SAME_FAMILY`, `SWITCH_TRANSFORM_FAMILY`, `OPPORTUNITY_REANALYSIS`, `TARGET_NEW_PATH_CLUSTER`, `BRANCH_FROM_ALTERNATE_PARENT`, `REPARTITION_FORMAL_PROOF`, `PHYSICAL_ONLY_RECOMMENDATION`, `REJECT_CANDIDATE`, `ABANDON_LINEAGE`, or `STOP_RUN_OR_REQUEST_HUMAN`. Budgets can only stay equal or decrease along a lineage. Protected edits route to `REJECT_CANDIDATE` or `STOP_RUN_OR_REQUEST_HUMAN`; infrastructure failures cannot create an RTL repair proposal.

### 4.11 `ParetoRecord` and `RunEvent`

`ParetoRecord` contains candidate ID, correctness/selection class, feasibility predicate version, complete required-view metric IDs, metric vector, dominance set, objective-policy rank, binding/clock/CDC/formal hashes, physical stage, and record timestamp. Dominance is calculated only within the same correctness-contract class and comparable metric identities.

`RunEvent` contains `event_id`, `run_id`, monotonically increasing `sequence`, event type, entity type/ID, timestamp, prior/new state, policy hash, payload schema name/version, payload artifact, duration/resource use, and status/error code. Sequence is the replay authority; wall-clock timestamps do not reorder events. Replay performs no model or EDA calls.

`ExperimentRecord` is the append-only search unit. It contains the planner/council IDs, opportunity and cone fingerprints, proposal/operation/parameters, parent and candidate IDs, source/patch/transform fingerprints, ordered stage results, comparison identities, proof/counterexample summary, before/after metrics, failure/directive/fingerprint/recovery IDs, descendant outcome, role/model/prompt/context hashes, tokens/latency/EDA cost, terminal disposition and human review record. Search indexes contain record IDs and derived keys only and can be rebuilt from these records.

`RecoveryRoutePlan` is an internal persisted control object containing failure/directive IDs, allowed action set, eligible roles, excluded families, next-parent choices, remaining budgets and policy hash. A `RecoveryAdvisor` cannot see or return any action outside this set; `RecoveryDecision` records the final validated subset.

### 4.12 Schema compatibility matrix

| Producer | Contract | Consumer |
|---|---|---|
| Manifest loader | `ProjectManifest` | Doctor, orchestrator, all adapters |
| Platform locker | Platform lock + `ArtifactRef` | Analysis views and adapters |
| Yosys/OpenSTA/OpenROAD | `StageResult`, paths, metrics | Evidence builder, evaluator, reports |
| Constraint/clock/CDC audits | Binding and inventory contracts | Safety gate, formal, UI |
| Evidence builder | `EvidenceGraphSnapshot`, `OptimizationOpportunity` | Router/planners |
| Planner modes | `PlannerResult`, `OptimizationProposal` | Validator/executor |
| Council runtime | `CouncilResult` | Planner normalization and audit UI |
| Transform executor | `CandidateRecord` | Evaluation cascade |
| EQY/SBY | `ProofResult` | Feasibility, recovery, reports |
| Evaluation cascade | `StageResult` | Failure classifier and candidate state |
| Failure compiler | `FailureEvent`, `RepairDirective` | Recovery router/`RecoveryAdvisor` |
| Recovery router | `RecoveryDecision` | Search controller |
| Search controller | `ParetoRecord`, ledger records | Reports/UI |
| Every component | `RunEvent` | Replay/UI/audit |

### 4.13 Boundary request and result schemas

Every contract in this subsection has `schema_version=1`, rejects unknown fields, uses canonical IDs/hashes from Section 4.1, and is exported through `SCHEMA_REGISTRY`. A request records identities and permissions, never mutable object bodies; a result records terminal status plus artifact/evidence references, never an untracked blob.

#### Platform and tool-execution boundaries

| Schema | Required canonical fields |
|---|---|
| `DoctorReport` | `status`, nonempty ordered `checks: Sequence[DoctorCheck]`, `platform_lock_hash`, `generated_at`, and process-facing `exit_code`; `PASS` requires every check to pass and exit code zero |
| `ToolJob` | `tool_job_id`, preallocated `stage_result_id`, `run_id`, `candidate_id`, `stage`, optional `analysis_view_id`, `design_contract_hash`, `input_artifact_refs`, `input_hashes: StageInputHashes`, `resource_limits`, `deadline`, `artifact_namespace`, and `requested_at` |
| `PreparedCommand` | `tool_job_id`, `stage_result_id`, `tool_fingerprint`, nonempty `argv`, absolute isolated `working_directory`, sanitized `environment`, `resource_limits`, `deadline`, staged input artifact refs, generated recipe/script artifact refs, and `preparation_hash`; shell command strings are invalid |
| `RawToolResult` | `tool_job_id`, `stage_result_id`, `tool_fingerprint`, prepared-command artifact/hash, `exit_code`, optional `signal`, `timed_out`, `started_at`, `ended_at`, `resource_usage`, and nonempty raw `ArtifactRef` list containing stdout, stderr, invoked argv/recipe and produced reports |

`DoctorCheck` contains `name`, `status`, optional `resolved_path`, optional `tool_fingerprint`, optional non-tool `artifact_hash`, and `message`. Exactly one of `tool_fingerprint` or `artifact_hash` is present for a passing executable or file check. `RawToolResult` is not an analysis verdict: only `ToolAdapter.parse` followed by `ToolAdapter.validate` may produce the canonical version-2 `StageResult`.

#### Planner, context, provider, and council boundaries

| Schema | Required canonical fields |
|---|---|
| `PlannerRequest` | `planner_request_id`, `run_id`, `parent_candidate_id`, `opportunity_id`, evidence/design/policy/transform-registry snapshot hashes, authorized evidence IDs, planner mode, proposal limit, token/latency budget, deadline, deterministic seed, and required output schema name/version |
| `ContextRequest` | `context_request_id`, `planner_request_id`, role, common-envelope hash, private evidence IDs, retrieval allowlist, snapshot hash, token budget, and redaction policy hash |
| `RoleContextPack` | `role_context_pack_id`, `context_request_id`, role, common-envelope artifact/hash, private-pack artifact/hash, retrieval grants, rendered-message artifact/hash, estimated tokens, and creation timestamp |
| `ProviderResult` | `provider_result_id`, request/context IDs, terminal status, provider/model identity, provider configuration hash, prompt hash, requested schema name/version, structured-output artifact/hash, token usage, latency, optional registered error code, and completion timestamp |
| `CouncilRequest` | `council_request_id`, `planner_request_id`, council-route ID/hash, ordered blinded proposer roles, critic roles, chair role, context policy hash, fan-out/fan-in limits, aggregate token/latency budget, deadline, and event-stream ID |

`ProviderResult.status` is one of `PASS`, `INVALID_OUTPUT`, `PROVIDER_ERROR`, `TIMEOUT`, or `BUDGET_EXHAUSTED`; only `PASS` may reference a normalized model output. `CouncilRequest` cannot enlarge the evidence, proposal, token, latency, or transform permissions in its parent `PlannerRequest`. `PlannerResult` and `CouncilResult` remain the canonical normalized outputs described in Section 4.9; provider-native payloads never cross those boundaries directly.

#### Recovery, search, and reporting boundaries

| Schema | Required canonical fields |
|---|---|
| `RecoveryRequest` | `recovery_request_id`, failure/directive/route-plan IDs and hashes, candidate lineage IDs, candidate-failure fingerprint IDs, allowed actions, excluded transform families, eligible roles, authorized evidence IDs, remaining family/lineage/token/latency budgets, policy hash, and deadline |
| `SearchRequest` | `search_request_id`, `run_id`, design/policy/transform-registry hashes, ordered opportunity IDs, planner mode, required correctness class, candidate/formal/physical/token/latency budgets, deterministic seed, stop-policy hash, and creation timestamp |
| `SearchResult` | `search_result_id`, `search_request_id`, terminal status, ordered candidate IDs, feasible and Pareto candidate IDs, selected candidate ID or null, stop reason code, consumed budgets, planner/council/recovery IDs, event range, artifact refs, and completion timestamp |
| `ReportBundle` | `report_bundle_id`, `run_id`, selected candidate ID, claim-to-evidence index, baseline/final comparison identities, timing/PPA/formal/binding/clock/CDC artifact refs, experiment-ledger range/hash, replay-manifest artifact/hash, UI snapshot refs, bundle hash, and creation timestamp |

`RecoveryRequest.allowed_actions`, excluded families, eligible roles, and remaining budgets must equal or narrow its referenced `RecoveryRoutePlan`. `SearchResult` is terminal only with `COMPLETED`, `BUDGET_EXHAUSTED`, `NO_FEASIBLE_CANDIDATE`, `STOPPED_BY_POLICY`, or `INFRASTRUCTURE_BLOCKED`; it never converts an inconclusive or failed candidate into success. Every headline claim in `ReportBundle.claim_to_evidence_index` resolves to one or more immutable artifacts and the exact comparison identities used to compute it.

---

## 5. Milestone Summary and Critical Path

| Milestone | Outcome | Estimated focused effort | Hard dependency | MVP class |
|---|---|---:|---|---|
| M0 | Reproducible EDA/platform baseline contract | 1–2 engineer-days | None | Tier 0 |
| M1 | Typed contracts, artifacts, ledger and replay spine | 2 days | M0 decisions | Tier 0 |
| M2 | Valid benchmark and baseline evidence | 3–4 days | M0–M1 | Tier 0 |
| M3 | Critical-path evidence and opportunity formation | 2 days | M2 | Tier 0 |
| M4 | One strict-equivalence end-to-end optimized candidate | 3 days | M1–M3 | Tier 0 |
| M5 | Four transforms, bounded search and strict Pareto archive | 3–4 days | M4 | Tier 1 |
| M6 | Constrained single-agent planning | 2 days | M3–M5 interfaces | Tier 1 |
| M7 | Deterministic failure-aware recovery | 2–3 days | M4–M5 | Tier 1 |
| M8 | Minimum bounded role-scoped council | 2–3 days | M6 plus green M4 | Tier 1 |
| M9 | Full evaluation, reports, UI and rehearsal | 3–4 days | M5–M8 | Tier 1 |

The earliest credible submission path is M0 -> M1 -> M2 -> M3 -> M4 -> M5 -> M9. M6–M8 add the GenAI and multi-agent differentiation required for the intended competition MVP, but they are integrated only through already-tested interfaces.

---

## 6. M0 — Reproducible Toolchain and Platform Contract

### Task 1: Create the repository, package, and toolchain doctor

**Files:**

- Create: `pyproject.toml`
- Create: `environment.yml`
- Create: `requirements.txt`
- Create: `Makefile`
- Create: `.gitignore`
- Create: `src/nova_rtl/__init__.py`
- Create: `src/nova_rtl/cli.py`
- Create: `src/nova_rtl/contracts/__init__.py`
- Create: `src/nova_rtl/contracts/base.py`
- Create: `src/nova_rtl/contracts/platform.py`
- Create: `src/nova_rtl/platform/doctor.py`
- Test: `tests/unit/platform/test_doctor.py`

**Interfaces:**

- Consumes: process environment and an optional platform-lock path.
- Produces: `DoctorReport run_doctor(required_tools: Sequence[str], platform_lock: Path | None) -> DoctorReport`.
- CLI: `nova doctor --platform-lock config/platform/platform.lock.yaml --json`.

- [ ] **Step 1: Confirm repository and package metadata**

Run:

```bash
conda env create --file environment.yml
conda activate nova-rtl
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
```

`environment.yml` pins Python 3.11 and bootstraps pip; `pyproject.toml` is the authoritative Python package/dependency contract; and `requirements.txt` is the convenience editable-development installer. `pyproject.toml` must expose the `nova` console script as `nova_rtl.cli:main` and define pytest paths. `.gitignore` must exclude local environments, `runs/`, caches, generated benchmark build directories, solver scratch data, and unredacted provider traces.

- [ ] **Step 2: Write the failing doctor test**

```python
def test_doctor_reports_every_missing_dependency(tmp_path):
    report = run_doctor(
        required_tools=("yosys", "opensta", "openroad", "eqy", "sby"),
        platform_lock=tmp_path / "missing.lock.yaml",
        which=lambda _: None,
    )
    assert report.status == "FAIL"
    assert [item.name for item in report.checks] == [
        "yosys", "opensta", "openroad", "eqy", "sby", "platform_lock"
    ]
    assert report.exit_code == 2
```

- [ ] **Step 3: Run the test and confirm the red state**

Run: `python -m pytest tests/unit/platform/test_doctor.py::test_doctor_reports_every_missing_dependency -v`

Expected: failure because `run_doctor` and `DoctorReport` do not exist.

- [ ] **Step 4: Implement strict doctor models and checks**

```python
class DoctorCheck(StrictContract):
    name: str
    status: Literal["PASS", "FAIL"]
    resolved_path: str | None
    tool_fingerprint: ToolFingerprint | None
    artifact_hash: HashRef | None
    message: str

class DoctorReport(StrictContract):
    schema_version: Literal[1]
    status: Literal["PASS", "FAIL"]
    checks: Sequence[DoctorCheck]
    platform_lock_hash: HashRef | None
    generated_at: AwareDatetime
    exit_code: int

def run_doctor(
    required_tools: Sequence[str],
    platform_lock: Path | None,
    which: Callable[[str], str | None] = shutil.which,
) -> DoctorReport:
    checks = tuple(probe_executable(name, which) for name in required_tools)
    if platform_lock is not None:
        checks += (verify_platform_lock(platform_lock),)
    status = "PASS" if all(item.status == "PASS" for item in checks) else "FAIL"
    return DoctorReport(
        schema_version=1,
        status=status,
        checks=checks,
        platform_lock_hash=hash_if_present(platform_lock),
        generated_at=utc_now(),
        exit_code=0 if status == "PASS" else 2,
    )
```

These models live in `contracts/platform.py`; `platform/doctor.py` imports rather than redefines them. The implementation sorts neither away nor suppresses failures, invokes each available tool with its read-only version flag, records stdout/stderr hashes, and returns exit code `2` if any required check fails.

- [ ] **Step 5: Verify unit behavior and CLI exit codes**

Run:

```bash
python -m pytest tests/unit/platform/test_doctor.py -v
nova doctor --platform-lock config/platform/platform.lock.yaml --json
```

Expected now: unit tests pass; the CLI may return `2` until Task 2 pins the actual toolchain, while its JSON lists every missing item rather than crashing.

- [ ] **Step 6: Commit the repository spine**

```bash
git add pyproject.toml environment.yml requirements.txt Makefile .gitignore src/nova_rtl tests/unit/platform
git commit -m "build: add NOVA-RTL package and toolchain doctor"
```

### Task 2: Discover, validate, and lock the actual OpenROAD platform

**Files:**

- Modify: `src/nova_rtl/contracts/platform.py`
- Create: `src/nova_rtl/platform/lock.py`
- Create: `config/platform/platform-selection-policy.yaml`
- Create during execution: `config/platform/platform.lock.yaml`
- Create: `config/challenge/organizer_decisions.yaml`
- Create during execution: `config/analysis/views.yaml`
- Create: `config/policy/default.yaml`
- Create: `config/policy/cdc_patterns.yaml`
- Create: `config/formal/reset_assumptions.yaml`
- Test: `tests/unit/platform/test_lock.py`
- Test: `tests/integration/tiny/test_platform_smoke.py`

**Interfaces:**

- Consumes: candidate platform root, tool paths, selection policy, organizer decisions.
- Produces: `PlatformLock create_platform_lock(request: PlatformLockRequest) -> PlatformLock`.
- CLI: `nova platform lock --root <real-platform-root> --policy config/platform/platform-selection-policy.yaml`.

- [ ] **Step 1: Write platform-lock validation tests**

```python
def test_platform_lock_rejects_missing_hold_corner(fake_platform):
    fake_platform.remove("lib/hold.lib")
    with pytest.raises(PlatformContractError, match="required HOLD liberty"):
        create_platform_lock(fake_platform.request())

def test_platform_lock_hashes_every_consumed_file(fake_platform):
    lock = create_platform_lock(fake_platform.request())
    assert lock.setup_view.liberty.sha256.startswith("sha256:")
    assert lock.hold_view.liberty.sha256.startswith("sha256:")
    assert lock.lef_files
    assert lock.container_digest.startswith("sha256:")
```

- [ ] **Step 2: Run the tests and confirm missing models fail collection**

Run: `python -m pytest tests/unit/platform/test_lock.py -v`

Expected: failure because the platform lock request/model and creator are absent.

- [ ] **Step 3: Implement file discovery and immutable hashing**

```python
class PlatformLockRequest(BaseModel):
    platform_id: str
    platform_root: Path
    setup_liberty: Path
    hold_liberty: Path
    lef_files: Sequence[Path]
    setup_rc_config: Path
    hold_rc_config: Path
    openroad_flow_config: Path
    container_image: str
    container_digest: str

def create_platform_lock(request: PlatformLockRequest) -> PlatformLock:
    """Resolve real files, copy/snapshot metadata, hash bytes, reject aliases or missing files."""
    inputs = collect_required_platform_inputs(request)
    validate_all_regular_files(inputs)
    discovered_conditions = inspect_liberty_operating_conditions(request.setup_liberty, request.hold_liberty)
    return PlatformLock.from_request(request, inputs, discovered_conditions)
```

The lock also records tool versions, operating conditions discovered from Liberty, OpenROAD recipe hash, host CPU/OS metadata, deterministic seed policy, and license/redistribution notes. Symlinks are resolved before hashing and the original plus resolved paths are recorded. The command materializes `config/analysis/views.yaml` from the real setup/hold files and hashes, while default policy, CDC registry and reset assumptions are independently versioned inputs.

- [ ] **Step 4: Run the real smoke flow**

Run:

```bash
nova platform lock --root /absolute/path/to/selected/platform --policy config/platform/platform-selection-policy.yaml
python -m pytest tests/integration/tiny/test_platform_smoke.py -v --platform-lock config/platform/platform.lock.yaml
nova doctor --platform-lock config/platform/platform.lock.yaml --json
```

Expected: a two-flop smoke design synthesizes, receives setup and hold analysis under the two locked views, completes the chosen OpenROAD smoke stage, and `nova doctor` returns `0`. A missing real platform is a visible M0 execution blocker, not a reason to create fabricated corner paths.

- [ ] **Step 5: Record organizer defaults and lock hashes**

Run:

```bash
python -m nova_rtl.platform.lock verify config/platform/platform.lock.yaml
sha256sum config/platform/platform.lock.yaml config/challenge/organizer_decisions.yaml
```

Expected: verification exits `0`, and both hashes are copied into `runs/<run_id>/inputs/toolchain.lock.json` when a run begins.

- [ ] **Step 6: Commit the real platform contract**

```bash
git add src/nova_rtl/platform config/platform config/challenge config/analysis config/policy config/formal tests/unit/platform tests/integration/tiny
git commit -m "build: pin reproducible EDA platform and corner contract"
```

**M0 exit gate:** `nova doctor` returns `0`; the tiny two-view physical smoke test passes; real file hashes exist for both Liberty views, LEF, RC configuration and flow recipe; and organizer defaults are versioned. No timing number may be published before this gate.

**M0 cut rule:** Do not begin agent integration while M0 is red. Tool installation and platform selection are critical-path work.

---

## 7. M1 — Typed Contracts, Immutable Artifacts, Ledger, and Replay

### Task 3: Implement strict schema models and deterministic export

**Files:**

- Modify: `src/nova_rtl/contracts/base.py`
- Modify: `src/nova_rtl/contracts/platform.py`
- Create: `src/nova_rtl/contracts/manifest.py`
- Create: `src/nova_rtl/contracts/execution.py`
- Create: `src/nova_rtl/contracts/analysis.py`
- Create: `src/nova_rtl/contracts/optimization.py`
- Create: `src/nova_rtl/contracts/verification.py`
- Create: `src/nova_rtl/contracts/planning.py`
- Create: `src/nova_rtl/contracts/recovery.py`
- Create: `src/nova_rtl/contracts/reporting.py`
- Create: `src/nova_rtl/contracts/events.py`
- Create: `src/nova_rtl/contracts/migrations.py`
- Create: `src/nova_rtl/contracts/schema_export.py`
- Test: `tests/unit/contracts/test_contracts.py`
- Test: `tests/unit/contracts/test_schema_export.py`
- Test: `tests/unit/contracts/test_canonical_names.py`
- Test: `tests/unit/contracts/test_stage_result_migration.py`

**Interfaces:**

- Consumes: normalized Python dictionaries from config, parsers, planners and policies.
- Produces: the canonical models in Section 4 and `export_all_schemas(output_dir: Path) -> Sequence[Path]`.

- [ ] **Step 1: Write strict primitive and cross-field tests**

```python
def test_models_reject_unknown_fields(valid_stage_result):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        StageResult.model_validate({**valid_stage_result, "agent_score": 0.99})

def test_setup_stage_requires_view_and_missing_reason(valid_stage_result):
    data = deepcopy(valid_stage_result)
    data["analysis_view_id"] = None
    data["metrics"]["setup_wns_ns"] = None
    data["metrics"]["missing_metric_reasons"] = {}
    with pytest.raises(ValidationError):
        StageResult.model_validate(data)

def test_primary_manifest_requires_strict_contract(valid_manifest):
    valid_manifest["formal"]["primary_competition_contract"] = "LATENCY_AWARE"
    with pytest.raises(ValidationError, match="STRICT_SEQ_EQUIV"):
        ProjectManifest.model_validate(valid_manifest)

def test_stage_result_v1_migrates_once_to_canonical_v2(
    legacy_stage_result,
    legacy_migration_context,
):
    migrated = StageResultV1ToV2.migrate(
        legacy_stage_result,
        context=legacy_migration_context,
    )
    assert migrated.schema_version == 2
    assert isinstance(migrated.tool_fingerprint, ToolFingerprint)
    assert all(isinstance(item, ArtifactRef) for item in migrated.raw_artifacts)
```

- [ ] **Step 2: Run the schema tests and confirm the red state**

Run: `python -m pytest tests/unit/contracts/test_contracts.py -v`

Expected: collection fails because canonical models are absent.

- [ ] **Step 3: Implement common strict base classes and every Section 4 schema**

```python
class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

HashRef = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$")]

class StageResult(StrictContract):
    schema_version: Literal[2]
    stage_result_id: EntityId
    run_id: EntityId
    candidate_id: EntityId
    stage: Stage
    analysis_view_id: EntityId | None
    status: StageStatus
    tool_fingerprint: ToolFingerprint
    input_hashes: StageInputHashes
    metrics: MetricSet
    diagnostics: Sequence[Diagnostic]
    raw_artifacts: Sequence[ArtifactRef]
    started_at: AwareDatetime
    ended_at: AwareDatetime
```

Implement validators exactly as specified in Section 4, including status/evidence requirements, view-specific metrics, proof scope, binding coverage arithmetic, council critique dispositions, monotonic recovery budgets, and primary strict-contract enforcement. `StageResultV1ToV2` is the only compatibility path: it resolves legacy URI strings through the artifact index, expands legacy diagnostics and missing metrics deterministically, records the source-object hash, and rejects any legacy field it cannot translate without inference.

- [ ] **Step 4: Export stable JSON Schemas and detect drift**

```python
@dataclass(frozen=True)
class SchemaRegistration:
    model: type[StrictContract]
    version: int

SCHEMA_REGISTRY: dict[str, SchemaRegistration] = {
    "artifact-ref": SchemaRegistration(ArtifactRef, version=1),
    "evidence-ref": SchemaRegistration(EvidenceRef, version=1),
    "diagnostic": SchemaRegistration(Diagnostic, version=1),
    "doctor-check": SchemaRegistration(DoctorCheck, version=1),
    "stage-input-hashes": SchemaRegistration(StageInputHashes, version=1),
    "project-manifest": SchemaRegistration(ProjectManifest, version=2),
    "platform-lock": SchemaRegistration(PlatformLock, version=1),
    "design-contract": SchemaRegistration(DesignContract, version=1),
    "analysis-view-contract": SchemaRegistration(AnalysisViewContract, version=1),
    "power-activity-contract": SchemaRegistration(PowerActivityContract, version=1),
    "frequency-sweep-contract": SchemaRegistration(FrequencySweepContract, version=1),
    "tool-fingerprint": SchemaRegistration(ToolFingerprint, version=1),
    "doctor-report": SchemaRegistration(DoctorReport, version=1),
    "tool-job": SchemaRegistration(ToolJob, version=1),
    "prepared-command": SchemaRegistration(PreparedCommand, version=1),
    "raw-tool-result": SchemaRegistration(RawToolResult, version=1),
    "critical-path-record": SchemaRegistration(CriticalPathRecord, version=1),
    "clock-inventory": SchemaRegistration(ClockInventory, version=1),
    "cdc-inventory": SchemaRegistration(CDCInventory, version=1),
    "evidence-graph-snapshot": SchemaRegistration(EvidenceGraphSnapshot, version=1),
    "metric-set": SchemaRegistration(MetricSet, version=1),
    "stage-result": SchemaRegistration(StageResult, version=2),
    "optimization-opportunity": SchemaRegistration(OptimizationOpportunity, version=1),
    "optimization-proposal": SchemaRegistration(OptimizationProposal, version=2),
    "candidate-record": SchemaRegistration(CandidateRecord, version=1),
    "constraint-binding-manifest": SchemaRegistration(ConstraintBindingManifest, version=1),
    "formal-model-contract": SchemaRegistration(FormalModelContract, version=1),
    "proof-result": SchemaRegistration(ProofResult, version=1),
    "planner-request": SchemaRegistration(PlannerRequest, version=1),
    "planner-result": SchemaRegistration(PlannerResult, version=1),
    "context-request": SchemaRegistration(ContextRequest, version=1),
    "role-context-pack": SchemaRegistration(RoleContextPack, version=1),
    "provider-result": SchemaRegistration(ProviderResult, version=1),
    "diagnosis-report": SchemaRegistration(DiagnosisReport, version=1),
    "transform-recommendation": SchemaRegistration(TransformRecommendation, version=1),
    "critique-report": SchemaRegistration(CritiqueReport, version=1),
    "critique-disposition": SchemaRegistration(CritiqueDisposition, version=1),
    "proposal-shortlist": SchemaRegistration(ProposalShortlist, version=1),
    "council-trace": SchemaRegistration(CouncilTrace, version=1),
    "council-request": SchemaRegistration(CouncilRequest, version=1),
    "council-result": SchemaRegistration(CouncilResult, version=1),
    "failure-event": SchemaRegistration(FailureEvent, version=1),
    "repair-directive": SchemaRegistration(RepairDirective, version=1),
    "candidate-failure-fingerprint": SchemaRegistration(CandidateFailureFingerprint, version=1),
    "recovery-route-plan": SchemaRegistration(RecoveryRoutePlan, version=1),
    "recovery-request": SchemaRegistration(RecoveryRequest, version=1),
    "recovery-advice": SchemaRegistration(RecoveryAdvice, version=1),
    "recovery-decision": SchemaRegistration(RecoveryDecision, version=1),
    "experiment-record": SchemaRegistration(ExperimentRecord, version=1),
    "pareto-record": SchemaRegistration(ParetoRecord, version=1),
    "search-request": SchemaRegistration(SearchRequest, version=1),
    "search-result": SchemaRegistration(SearchResult, version=1),
    "report-bundle": SchemaRegistration(ReportBundle, version=1),
    "run-event": SchemaRegistration(RunEvent, version=1),
}
```

Run: `python -m nova_rtl.contracts.schema_export schemas/canonical`

Expected: one deterministically formatted `<registry-key>.v<schema_version>.schema.json` file per registry key. A second run produces no byte change. Mixed contract versions therefore cannot be mistaken for one directory-wide version.

Run: `python -m pytest tests/unit/contracts/test_canonical_names.py tests/unit/contracts/test_stage_result_migration.py -v`

Expected: public source contains no forbidden aliases outside `contracts/migrations.py`; the legacy fixture becomes a valid version-2 `StageResult`; a malformed legacy object fails rather than receiving fabricated values.

- [ ] **Step 5: Run contract, export, and serialization tests**

Run: `python -m pytest tests/unit/contracts -v`

Expected: all contract fixtures round-trip through canonical JSON; invalid enums, hashes, references and cross-field combinations fail.

- [ ] **Step 6: Commit schemas and generated contracts**

```bash
git add src/nova_rtl/contracts schemas/canonical tests/unit/contracts
git commit -m "feat: add canonical NOVA-RTL contracts and schemas"
```

### Task 4: Build immutable artifacts, append-only ledger, and replay

**Files:**

- Create: `src/nova_rtl/artifacts/store.py`
- Create: `src/nova_rtl/artifacts/ledger.py`
- Create: `src/nova_rtl/artifacts/replay.py`
- Test: `tests/unit/artifacts/test_store.py`
- Test: `tests/unit/artifacts/test_ledger.py`
- Test: `tests/unit/artifacts/test_replay.py`

**Interfaces:**

- Produces: `ArtifactStore.put_bytes`, `ArtifactStore.open_verified`, `ExperimentLedger.append_event`, `ExperimentLedger.append_record`, `ExperimentLedger.iter_events`, `ExperimentLedger.iter_records`, and `replay_run`.
- Consumes: canonical contract JSON bytes, `RunEvent` objects, and `ExperimentRecord` objects.

- [ ] **Step 1: Write immutability and replay-order tests**

```python
def test_put_is_content_addressed_and_verified(store):
    first = store.put_bytes(b"same", media_type="text/plain", classification="INTERNAL")
    second = store.put_bytes(b"same", media_type="text/plain", classification="INTERNAL")
    assert first.sha256 == second.sha256
    assert first.uri == second.uri
    assert store.open_verified(first).read() == b"same"

def test_replay_uses_sequence_not_timestamp(ledger):
    ledger.append_event(event(sequence=1, timestamp="2026-08-14T01:00:00Z"))
    ledger.append_event(event(sequence=2, timestamp="2026-08-14T00:00:00Z"))
    assert [e.sequence for e in replay_run(ledger)] == [1, 2]
```

- [ ] **Step 2: Confirm tests fail before storage code exists**

Run: `python -m pytest tests/unit/artifacts -v`

Expected: import/collection failure.

- [ ] **Step 3: Implement atomic content-addressed writes**

```python
class ArtifactStore:
    def put_bytes(self, data: bytes, *, media_type: str, classification: Classification) -> ArtifactRef:
        raise NotImplementedError("Concrete filesystem store supplies atomic content-addressed writes")
    def put_json(self, value: StrictContract, *, classification: Classification) -> ArtifactRef:
        raise NotImplementedError("Concrete store serializes canonical model JSON before put_bytes")
    def open_verified(self, ref: ArtifactRef) -> BinaryIO:
        raise NotImplementedError("Concrete store verifies SHA-256 before returning a reader")

class ExperimentLedger:
    def append_event(self, event: RunEvent) -> None:
        raise NotImplementedError("SQLite implementation enforces append-only unique sequence")
    def append_record(self, record: ExperimentRecord) -> None:
        raise NotImplementedError("SQLite implementation enforces immutable experiment IDs")
    def iter_events(self, run_id: EntityId) -> Iterator[RunEvent]:
        raise NotImplementedError("SQLite implementation returns sequence-ordered validated events")
    def iter_records(self, run_id: EntityId) -> Iterator[ExperimentRecord]:
        raise NotImplementedError("SQLite implementation returns immutable experiment records")
```

Writes use a temporary file, `fsync`, hash verification, atomic rename, and read-only final permissions. `ExperimentLedger.append_event` rejects duplicate event IDs and duplicate/non-monotonic sequence numbers; `append_record` rejects duplicate experiment IDs. Repair appends a compensating event or successor record rather than mutating history.

- [ ] **Step 4: Add corruption, missing-artifact, and no-call replay tests**

Tests must corrupt one stored byte and assert `ArtifactIntegrityError`; reference a missing payload and assert replay fails closed; and inject model/tool spies to prove replay makes zero external calls.

- [ ] **Step 5: Run artifact tests and a deterministic replay digest**

Run:

```bash
python -m pytest tests/unit/artifacts -v
python -m nova_rtl.artifacts.replay tests/fixtures/runs/minimal --digest
```

Expected: tests pass and repeated replay prints the same SHA-256 digest.

- [ ] **Step 6: Commit the provenance spine**

```bash
git add src/nova_rtl/artifacts tests/unit/artifacts
git commit -m "feat: add immutable artifacts ledger and deterministic replay"
```

### Task 4B: Implement the durable orchestrator and analysis-view authority

**Files:**

- Create: `src/nova_rtl/orchestrator/state.py`
- Create: `src/nova_rtl/orchestrator/runner.py`
- Create: `src/nova_rtl/orchestrator/scheduler.py`
- Create: `src/nova_rtl/analysis_views/comparability.py`
- Create: `src/nova_rtl/analysis_views/aggregation.py`
- Test: `tests/unit/orchestrator/test_state.py`
- Test: `tests/unit/orchestrator/test_resume.py`
- Test: `tests/unit/analysis_views/test_comparability.py`
- Test: `tests/unit/analysis_views/test_aggregation.py`

**Interfaces:**

- `RunOrchestrator.transition(run_id, expected_state, next_state, payload) -> RunStateRecord` is the only global state writer.
- `BoundedScheduler.submit(job: JobRequest) -> JobHandle` enforces per-kind CPU/memory/concurrency/deadline budgets.
- `assert_comparable(baseline, candidate, metric_family) -> ComparabilityResult` fails closed on identity mismatch.
- `aggregate_required_views(results, contracts) -> AggregatedMetrics` selects the worst complete required setup and hold views.

- [ ] **Step 1: Write illegal-transition, resume, budget and view-identity tests**

```python
def test_run_state_machine_rejects_skipping_baseline(orchestrator):
    with pytest.raises(IllegalTransitionError):
        orchestrator.transition("run_001", "INGESTING", "SEARCHING", payload_ref())

def test_resume_reuses_only_exact_cache_identity(orchestrator, cached_stage):
    assert orchestrator.can_reuse(cached_stage, cached_stage.input_hashes)
    changed = cached_stage.input_hashes.model_copy(update={"analysis_view": OTHER_HASH})
    assert not orchestrator.can_reuse(cached_stage, changed)

def test_worst_view_requires_complete_setup_and_hold(view_results, view_contracts):
    view_results.pop("FUNC_HOLD_FAST")
    with pytest.raises(IncompleteRequiredViewError):
        aggregate_required_views(view_results, view_contracts)
```

- [ ] **Step 2: Run focused tests and observe missing-module failures**

Run: `python -m pytest tests/unit/orchestrator tests/unit/analysis_views -v`

Expected: imports fail because the state/scheduler/comparability modules do not exist.

- [ ] **Step 3: Implement compare-and-set run and candidate transitions**

Run states follow Architecture Section 9.2. State writes use a transaction, require the caller's expected prior state, append a `RunEvent`, and store a payload artifact before commit. Candidate states use a separate transition table and create a new candidate ID for every recovery child. Planner/council code receives no state-store write handle.

- [ ] **Step 4: Implement bounded scheduling and cancellation**

The scheduler has separate queues for synthesis/STA, formal, OpenROAD and planner requests. The run policy fixes concurrency, CPU, memory, wall time, candidate count, token count and retry depth. Cancellation terminates workers, preserves partial logs, emits a terminal event and cannot silently reset consumed budget on resume.

- [ ] **Step 5: Implement analysis-view comparability and worst-view aggregation**

Timing requires identical platform, recipe, SDC, binding intent and complete analysis-view hashes. Power additionally requires identical activity contract. Aggregation refuses missing required stages/metrics and selects minimum WNS plus corresponding view ID separately for setup and hold.

- [ ] **Step 6: Run state, restart and cross-view regression tests**

Run:

```bash
python -m pytest tests/unit/orchestrator tests/unit/analysis_views -v
python -m nova_rtl.orchestrator.runner tests/fixtures/runs/interrupted --resume --dry-run
```

Expected: legal transitions pass; illegal/duplicate transitions fail; the dry-run lists exact reusable and rerun stages; a missing or mismatched view cannot be aggregated.

- [ ] **Step 7: Commit the global control boundary**

```bash
git add src/nova_rtl/orchestrator src/nova_rtl/analysis_views tests/unit/orchestrator tests/unit/analysis_views
git commit -m "feat: add durable orchestration and analysis-view authority"
```

**M1 exit gate:** Every schema exports deterministically, invalid contracts fail closed, artifacts detect corruption, the ledger is append-only, replay resolves a fixture without model/tool calls, run transitions are durable, and required-view identity/aggregation fails closed.

**M1 cut rule:** Do not let individual adapters invent dictionaries or log-only outputs. They must depend on these contracts.

---

## 8. M2 — Benchmark and Trustworthy Baseline

### Task 5: Generate and calibrate the multi-clock benchmark

**Files:**

- Create: `src/nova_rtl/benchmark/generator.py`
- Create: `src/nova_rtl/benchmark/calibrate.py`
- Create: `src/nova_rtl/benchmark/validate.py`
- Create: `benchmark/generator/benchmark.yaml`
- Create: `benchmark/rtl/nebula_top.sv`
- Create: `benchmark/rtl/clocking/clock_divider_bank.sv`
- Create: `benchmark/rtl/cdc/sync_2ff.sv`
- Create: `benchmark/rtl/cdc/toggle_sync.sv`
- Create: `benchmark/rtl/cdc/async_fifo.sv`
- Create: `benchmark/constraints/nebula.sdc`
- Create: `benchmark/formal/cdc_protocol_properties.sv`
- Create: `benchmark/formal/harnesses.yaml`
- Create: `benchmark/sim/tb_nebula.sv`
- Create: `benchmark/sim/power_workload.yaml`
- Create: `benchmark/expected/clock_contract.json`
- Create: `benchmark/expected/cdc_contract.json`
- Test: `tests/unit/benchmark/test_generator.py`
- Test: `tests/integration/small/test_benchmark_elaboration.py`

**Interfaces:**

- Produces: `generate_benchmark(config: BenchmarkConfig, output: Path) -> BenchmarkSnapshot` and `validate_benchmark(snapshot, expectations) -> BenchmarkValidation`.
- Configuration controls domain replicas and generated clocks without changing source templates.

- [ ] **Step 1: Write deterministic clock-count and CDC tests**

```python
def test_default_benchmark_has_required_clock_topology(tmp_path):
    snapshot = generate_benchmark(BenchmarkConfig.default(), tmp_path)
    assert snapshot.expected_master_clocks == 5
    assert snapshot.expected_generated_per_master == 21
    assert snapshot.expected_generated_total == 105
    assert snapshot.source_hash == generate_benchmark(BenchmarkConfig.default(), tmp_path / "again").source_hash

def test_every_generated_clock_has_active_consumers(snapshot):
    result = validate_benchmark(snapshot, snapshot.expectations)
    assert result.generated_without_consumers == ()
```

- [ ] **Step 2: Run tests and confirm the red state**

Run: `python -m pytest tests/unit/benchmark/test_generator.py -v`

Expected: failure because generator contracts and RTL templates are absent.

- [ ] **Step 3: Implement five domains, divider lineages, active workloads, and CDC registry use**

The top-level modules are `u_ingress`, `u_schedule`, `u_dma`, `u_compute`, and `u_control`. Ratios are exactly:

```text
2, 3, 4, 5, 6, 7, 8, 10, 12, 16, 20, 24, 25, 32, 40, 48, 50, 64, 80, 96, 128
```

Each divided clock drives state that contributes to an observable checksum/status path so synthesis cannot remove it. Cross-master transfers use only registered two-flop level, toggle, request/acknowledge, asynchronous FIFO, Gray-pointer, and reset-synchronizer structures.

Even and odd ratios have explicit functional waveform definitions and matching `create_generated_clock` source/edge semantics. Every generated clock records one master lineage, divider ratio, waveform, divider-state fingerprint and active-consumer set in the expected clock contract.

The simulation workload drives deterministic reset, idle, burst, arbitration, backpressure and CDC traffic phases. It emits a VCD or SAIF with a fixed top scope and measurement window declared by `PowerActivityContract`. Formal CDC properties cover handshake stability, FIFO pointer safety, Gray transitions, pulse/toggle delivery assumptions and local reset release for the registered patterns.

- [ ] **Step 4: Add small and full parameter profiles**

```yaml
profiles:
  tiny:
    workload_scale: 1
    generated_clocks_per_master: 2
  full:
    workload_scale: 64
    generated_clocks_per_master: 21
    mapped_cell_target: [45000, 55000]
```

The tiny profile exists only for fast tests. Official reports use the full profile and effective organizer clock decision.

- [ ] **Step 5: Calibrate mapped cell count with bounded search**

`calibrate.py` runs Yosys with the locked Liberty, adjusts only `workload_scale`, stores every scale/count pair, and stops after 12 samples or when count lies in `[45000, 55000]`. Failure to enter the range emits a calibration report and blocks the full baseline.

Run:

```bash
nova benchmark generate --profile full --output benchmark/build/full
nova benchmark calibrate --profile full --platform-lock config/platform/platform.lock.yaml
python -m pytest tests/integration/small/test_benchmark_elaboration.py -v
```

Expected: deterministic RTL hashes for equal configuration; clean elaboration; full clock/CDC expectation artifacts; calibrated mapped cell count in range.

- [ ] **Step 6: Commit benchmark generator and validated snapshot inputs**

```bash
git add src/nova_rtl/benchmark benchmark/generator benchmark/rtl benchmark/constraints benchmark/formal benchmark/sim benchmark/expected tests/unit/benchmark tests/integration/small
git commit -m "feat: add configurable five-domain timing benchmark"
```

### Task 6: Implement baseline tool adapters and safety preflights

**Files:**

- Create: `src/nova_rtl/adapters/base.py`
- Create: `src/nova_rtl/adapters/yosys.py`
- Create: `src/nova_rtl/adapters/opensta.py`
- Create: `src/nova_rtl/adapters/openroad.py`
- Create: `src/nova_rtl/adapters/eqy.py`
- Create: `src/nova_rtl/adapters/sby.py`
- Create: `src/nova_rtl/adapters/simulation.py`
- Create: `src/nova_rtl/constraints/binding.py`
- Create: `src/nova_rtl/constraints/clocks.py`
- Create: `src/nova_rtl/cdc/inventory.py`
- Create: `src/nova_rtl/formal/preflight.py`
- Create: `src/nova_rtl/formal/harness.py`
- Test: `tests/golden/test_adapter_parsers.py`
- Test: `tests/integration/small/test_baseline_flow.py`

**Interfaces:**

- Consumes: immutable run snapshot, `AnalysisViewContract`, platform lock and tool recipe.
- Produces: `StageResult`, `ConstraintBindingManifest`, `ClockInventory`, `CDCInventory`, `FormalModelContract`, and raw artifacts.

- [ ] **Step 1: Create golden parser fixtures and failing assertions**

Golden fixtures include a passing report, negative setup slack, negative hold slack, absent required section, malformed number, unresolved SDC selector, changed wildcard binding, EQY pass/fail/unknown, VCD scope/activity-window mismatch, and OpenROAD crash.

```python
@pytest.mark.parametrize("fixture,status", [
    ("opensta/setup_violation.rpt", "FAIL"),
    ("opensta/missing_summary.rpt", "INFRASTRUCTURE_ERROR"),
    ("eqy/pass.log", "PASS"),
    ("eqy/unknown.log", "INCONCLUSIVE"),
])
def test_adapter_status_is_semantic_not_exit_code(fixture, status):
    assert parse_fixture(fixture).status == status
```

- [ ] **Step 2: Run parser tests and confirm the red state**

Run: `python -m pytest tests/golden/test_adapter_parsers.py -v`

Expected: import failure because adapters are absent.

- [ ] **Step 3: Implement a common adapter execution contract**

```python
class ToolAdapter(Protocol):
    def fingerprint(self) -> ToolFingerprint:
        raise NotImplementedError
    def prepare(self, job: ToolJob) -> PreparedCommand:
        raise NotImplementedError
    def run(self, command: PreparedCommand) -> RawToolResult:
        raise NotImplementedError
    def parse(self, raw: RawToolResult) -> StageResult:
        raise NotImplementedError
    def validate(self, result: StageResult) -> Sequence[Diagnostic]:
        raise NotImplementedError
```

The orchestrator, not the adapter protocol, owns persistence:

```python
def execute_tool_job(
    adapter: ToolAdapter,
    job: ToolJob,
    artifact_store: ArtifactStore,
) -> StageResult:
    """Run argv without a shell, capture bytes, hash inputs/outputs, parse, and persist."""
    command = adapter.prepare(job)
    raw = adapter.run(command)
    result = adapter.parse(raw)
    diagnostics = tuple(adapter.validate(result))
    return finalize_validated_stage_result(result, diagnostics, artifact_store)
```

`PreparedCommand` carries the `tool_job_id` and preallocated `stage_result_id`, an argv tuple, isolated working directory, sanitized environment allowlist, resource limits, deadline, staged input/recipe artifacts, tool fingerprint, and preparation hash; shell strings are forbidden. `RawToolResult` carries the same job/result identities, the prepared-command artifact/hash, exit and signal/timeout facts, resource use, and `ArtifactRef` objects for stdout, stderr, argv/recipes and tool outputs. `validate` returns semantic diagnostics after parsing. `finalize_validated_stage_result` fails the stage closed when required reports/metrics are absent and persists the canonical version-2 `StageResult`.

OpenSTA runs once per required `AnalysisViewContract`. OpenROAD uses native multi-corner analysis when the pinned flow proves it available; otherwise it runs deterministic per-view analysis over the same physical database/checkpoint and seed. Either implementation emits one view-keyed `StageResult` per required view and refuses worst-view aggregation until the complete set exists.

- [ ] **Step 4: Implement binding, clock, CDC, and formal-model preflights**

Binding normalization emits resolved sorted object sets for each SDC command. Clock inventory checks five independent masters and generated lineage. CDC inventory recognizes only the approved registry. Formal preflight compares functional portions of synthesis and formal elaborations and rejects a behavior-affecting formal define. The formal harness supplies independent master-clock event streams shared between corresponding gold/gate instances, derives generated clocks through the unchanged protected divider logic, enables multiclock semantics, and hashes all reset/fairness assumptions.

Required negative tests:

- unchanged SDC with an empty wildcard is `FORBIDDEN_DELTA`;
- new combinational cross-domain edge is rejected;
- generated clock with wrong ancestor is rejected;
- formal-only divider simplification returns `INFRASTRUCTURE_ERROR` before EQY;
- setup/hold results with wrong view hash are incomparable.

- [ ] **Step 5: Run the small baseline end to end**

Run:

```bash
nova init benchmark/build/tiny/project.yaml
nova analyze runs/latest --stages yosys,opensta,binding,clock,cdc,formal-smoke,openroad
python -m pytest tests/integration/small/test_baseline_flow.py -v --run runs/latest
```

Expected: all required tiny-profile views complete; every selector resolves; expected clock/CDC inventories match; formal preflight is `NONE`; smoke proof passes; OpenROAD artifacts resolve.

- [ ] **Step 6: Commit adapters and baseline preflights**

```bash
git add src/nova_rtl/adapters src/nova_rtl/constraints src/nova_rtl/cdc src/nova_rtl/formal tests/golden tests/integration/small
git commit -m "feat: add baseline EDA adapters and safety preflights"
```

**M2 exit gate:** The tiny flow is green; the full benchmark maps to 45K–55K cells; all configured clocks survive with valid lineage; CDC inventory matches; required setup/hold views complete; all SDC selectors resolve; and raw reports are preserved.

**M2 cut rule:** If full physical runtime is too high, keep full synthesis/STA/formal evidence and one fixed physical baseline run; never replace the full benchmark with the tiny profile in headline results.

---

## 9. M3 — Evidence Graph, Critical Paths, and Opportunities

### Task 7: Build source-mapped path clusters and typed opportunities

**Files:**

- Create: `src/nova_rtl/evidence/graph.py`
- Create: `src/nova_rtl/evidence/paths.py`
- Create: `src/nova_rtl/evidence/source_map.py`
- Create: `src/nova_rtl/evidence/opportunities.py`
- Test: `tests/unit/evidence/test_paths.py`
- Test: `tests/unit/evidence/test_opportunities.py`
- Test: `tests/integration/small/test_evidence_flow.py`

**Interfaces:**

- Produces: `build_evidence_graph(inputs) -> EvidenceGraphSnapshot`, `cluster_paths(paths) -> Sequence[PathCluster]`, and `form_opportunities(cluster, graph, policy) -> Sequence[OptimizationOpportunity]`.
- Consumes: parsed timing paths, Yosys structural graph, source maps, clock/CDC inventories and protected policy.

- [ ] **Step 1: Write path-clustering and source-resolution tests**

```python
def test_paths_cluster_by_shared_cone_not_report_order(path_fixture):
    clusters = cluster_paths(tuple(reversed(path_fixture.paths)))
    assert digest(clusters) == digest(cluster_paths(path_fixture.paths))
    assert clusters[0].root_causes[0].category == "DEEP_PRIORITY_CHAIN"

def test_protected_cone_is_not_editable(graph_with_cdc_neighbor):
    opportunities = form_opportunities(graph_with_cdc_neighbor.cluster, graph_with_cdc_neighbor, policy())
    assert opportunities[0].editability == "PROTECTED_OR_UNSAFE"
    assert opportunities[0].eligible_transform_families == ()
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run: `python -m pytest tests/unit/evidence -v`

Expected: import failure because graph/path/opportunity code is absent.

- [ ] **Step 3: Implement normalized graph and path clustering**

Graph nodes cover RTL spans, operations, registers, cells, nets, pins, clocks, domains, CDC boundaries and physical regions. Edges cover contains, maps-to, data-dependency, timing-arc, launch/capture, clock-lineage, crossing and proximity. Stable IDs derive from snapshot hash plus normalized semantic identity.

Path similarity combines shared mapped cone, endpoints, source spans, root-cause features, clock domain and analysis view. Clustering is deterministic under input order.

- [ ] **Step 4: Implement root-cause and opportunity rules**

The root-cause vocabulary is exactly `DEEP_PRIORITY_CHAIN`, `UNBALANCED_BOOLEAN_TREE`, `WIDE_COMPARATOR`, `SERIAL_ARITHMETIC`, `HIGH_FANOUT_CONTROL`, `MUX_AFTER_ARITHMETIC`, `REPEATED_DECODE`, `FSM_DECODE_DEPTH`, `RESOURCE_ARBITRATION`, `PLACEMENT_OR_WIRE_DOMINATED`, `CLOCK_OR_CONSTRAINT_ISSUE`, `CDC_ADJACENT_UNSAFE_TO_EDIT`, and `UNCLASSIFIED`. `DEEP_PRIORITY_CHAIN`, `UNBALANCED_BOOLEAN_TREE`, `REPEATED_DECODE`, and `FSM_DECODE_DEPTH` map to the four executable competition-MVP transforms; the other categories produce safe recommendations or conditional enhancements until a matching registered capability exists.

Opportunity priority is deterministic from worst slack, TNS share, affected endpoints, editability, physical confidence and proof cost. An agent does not set the priority score.

- [ ] **Step 5: Run evidence integration and verify reference integrity**

Run:

```bash
nova analyze runs/latest --stages evidence,opportunities
python -m pytest tests/unit/evidence tests/integration/small/test_evidence_flow.py -v
```

Expected: every `evidence_ref` resolves to the same snapshot; protected spans are non-editable; at least one seeded tiny benchmark path becomes an executable priority-mux opportunity.

- [ ] **Step 6: Commit evidence construction**

```bash
git add src/nova_rtl/evidence tests/unit/evidence tests/integration/small
git commit -m "feat: add source-mapped timing evidence and opportunities"
```

**M3 exit gate:** A raw OpenSTA path can be traced to mapped objects, source spans, clock/CDC protection context, a root-cause category and a schema-valid opportunity. Reversing report order does not change IDs or ranking.

**M3 cut rule:** Start with the worst setup cluster in one domain, but preserve view/domain fields so expansion does not require a schema change.

---

## 10. M4 — Strict-Equivalence Vertical Slice

### Task 8: Execute one priority-mux optimization through every mandatory gate

**Files:**

- Create: `src/nova_rtl/transforms/registry.py`
- Create: `src/nova_rtl/transforms/executor.py`
- Create: `src/nova_rtl/transforms/syntax.py`
- Create: `src/nova_rtl/transforms/priority_mux.py`
- Create: `src/nova_rtl/evaluation/cascade.py`
- Create: `src/nova_rtl/evaluation/feasibility.py`
- Create: `src/nova_rtl/search/dag.py`
- Create: `src/nova_rtl/formal/compose.py`
- Test: `tests/unit/transforms/test_priority_mux.py`
- Test: `tests/unit/transforms/test_syntax_backend.py`
- Test: `tests/unit/evaluation/test_feasibility.py`
- Test: `tests/integration/tiny/test_strict_vertical_slice.py`

**Interfaces:**

- Produces: `TransformRegistry.resolve`, `TransformExecutor.materialize`, `EvaluationCascade.evaluate`, `is_feasible`, and immutable `CandidateRecord` updates.
- Consumes: one validated `OptimizationProposal`, parent snapshot, platform/run contracts and authorized span.

- [ ] **Step 1: Write transform authorization and semantic tests**

```python
def test_priority_mux_rewrite_is_deterministic(priority_fixture, proposal):
    first = materialize(priority_fixture, proposal)
    second = materialize(priority_fixture, proposal)
    assert first.source_hash == second.source_hash
    assert first.patch_artifact.sha256 == second.patch_artifact.sha256

def test_executor_rejects_change_outside_authorized_span(priority_fixture, malicious_transform):
    with pytest.raises(UnauthorizedDiffError):
        TransformExecutor(registry_with(malicious_transform)).materialize(priority_fixture.proposal)

def test_inconclusive_formal_is_never_feasible(candidate):
    candidate.proof.outcome = "INCONCLUSIVE"
    assert is_feasible(candidate, policy()) is False
```

- [ ] **Step 2: Run tests and confirm the red state**

Run: `python -m pytest tests/unit/transforms/test_priority_mux.py tests/unit/evaluation/test_feasibility.py -v`

Expected: import failure because registry, executor and feasibility predicate are absent.

- [ ] **Step 3: Implement capability metadata and deterministic materialization**

```python
class TransformCapability(Protocol):
    operation: Operation
    family: TransformFamily
    correctness_contract: CorrectnessContract
    parameter_model: type[BaseModel]

    def match(self, snapshot: RtlSnapshot, target: TargetRef) -> MatchResult:
        raise NotImplementedError
    def preflight(self, match: MatchResult, policy: Policy) -> GuardResult:
        raise NotImplementedError
    def rewrite(self, match: MatchResult, parameters: BaseModel) -> SourceEdit:
        raise NotImplementedError
    def fingerprint(self, match: MatchResult, parameters: BaseModel) -> str:
        raise NotImplementedError
```

The syntax backend uses pinned slang/pyslang parsing and exact source ranges. It rewrites only registered AST node kinds and emits a span replacement plus parse fingerprint; it never performs regex-only matching or reformats unrelated source. `RESTRUCTURE_PRIORITY_MUX` recognizes a bounded priority chain and produces balanced predecode while preserving branch priority and signed/width casts. It rejects incomplete assignments, side-effecting expressions, multi-clock cones, protected neighbors, ambiguous X semantics and source spans not owned by the proposal.

- [ ] **Step 4: Implement ordered gates 0 through 7**

The cascade persists `GATE_STARTED` and a terminal gate event for every attempted stage. It stops on first hard non-pass, classifies no failure yet beyond a simple terminal record, and never skips binding/clock/CDC checks. Formal runs before final acceptance; the exact delivered snapshot is re-proved after routed-final materialization.

The hard predicate is the expression in Architecture Section 20.2 plus: primary selection class must be `PRIMARY_STRICT`, proof contract must be `STRICT_SEQ_EQUIV`, and all official baseline/candidate metric identities must match.

- [ ] **Step 5: Verify the red/green formal mutation behavior**

Run:

```bash
python -m pytest tests/integration/tiny/test_strict_vertical_slice.py::test_bad_priority_rewrite_fails_eqy -v
python -m pytest tests/integration/tiny/test_strict_vertical_slice.py::test_registered_priority_rewrite_passes_all_gates -v
```

Expected: the intentionally wrong branch-priority mutation produces an EQY counterexample; the registered rewrite passes strict equivalence and all tiny-flow gates. Reverting the correct rewrite to the faulty implementation must make the first regression test fail before restoring it.

- [ ] **Step 6: Produce and inspect the first complete candidate bundle**

Run:

```bash
nova optimize runs/latest --planner heuristic --max-candidates 1 --operations RESTRUCTURE_PRIORITY_MUX
nova candidate inspect cand_001 --json
nova verify cand_001
```

Expected: immutable parent/child artifacts, authorized patch, equivalent binding/clock/CDC hashes, `STRICT_SEQ_EQUIV=PASS`, per-view STA, physical-stage result and replay events.

- [ ] **Step 7: Commit the strict vertical slice**

```bash
git add src/nova_rtl/transforms src/nova_rtl/evaluation src/nova_rtl/search/dag.py src/nova_rtl/formal/compose.py tests/unit/transforms tests/unit/evaluation tests/integration/tiny
git commit -m "feat: add strict-equivalence RTL optimization vertical slice"
```

**M4 exit gate:** One typed heuristic proposal becomes a deterministic patch, an immutable candidate, a passing strict proof, comparable multi-view timing/area evidence, and a replayable candidate bundle. A seeded incorrect rewrite produces a real counterexample.

**M4 cut rule:** Freeze multi-agent work until this milestone is green. This is the primary architecture-risk retirement point.

---

## 11. M5 — Deterministic Transform and Search Spine

### Task 9: Add the remaining competition-MVP Tier-A transforms

**Files:**

- Create: `src/nova_rtl/transforms/boolean_tree.py`
- Create: `src/nova_rtl/transforms/predicate.py`
- Create: `src/nova_rtl/transforms/fsm_decode.py`
- Modify: `src/nova_rtl/transforms/registry.py`
- Test: `tests/unit/transforms/test_boolean_tree.py`
- Test: `tests/unit/transforms/test_predicate.py`
- Test: `tests/unit/transforms/test_fsm_decode.py`
- Test: `tests/integration/tiny/test_transform_formal_matrix.py`

**Interfaces:**

- Each module implements the same `TransformCapability` protocol from Task 8.
- The registry produces `CapabilityDescriptor get_descriptor(operation: Operation)` and rejects unknown operations before source modification.

- [ ] **Step 1: Write positive, near-miss, idempotence, protected-node, width/X, reset and enable tests**

```python
@pytest.mark.parametrize("operation", [
    "BALANCE_BOOLEAN_TREE",
    "FACTOR_COMMON_PREDICATE",
    "FSM_DECODE_RESTRUCTURE",
])
def test_registered_transform_is_deterministic_and_idempotent(operation, fixture_for):
    first = execute(operation, fixture_for(operation))
    second = execute(operation, first.snapshot)
    assert first.authorized_diff_only
    assert second.changed_spans == ()

def test_boolean_balance_rejects_short_circuit_x_sensitive_case(x_sensitive_fixture):
    assert preflight("BALANCE_BOOLEAN_TREE", x_sensitive_fixture).status == "REJECT"

def test_fsm_decode_preserves_reset_and_state_register(fsm_fixture):
    result = execute("FSM_DECODE_RESTRUCTURE", fsm_fixture)
    assert result.modified_state_registers == ()
    assert result.correctness_contract == "STRICT_SEQ_EQUIV"
```

- [ ] **Step 2: Confirm the new transform tests fail before implementation**

Run: `python -m pytest tests/unit/transforms/test_boolean_tree.py tests/unit/transforms/test_predicate.py tests/unit/transforms/test_fsm_decode.py -v`

Expected: registry lookup or imports fail for all three operations.

- [ ] **Step 3: Implement exact rewrite constraints**

- `BALANCE_BOOLEAN_TREE` accepts associative bitwise operations only when operand widths, signedness, four-state semantics and parenthesis boundaries are provably preserved.
- `FACTOR_COMMON_PREDICATE` accepts pure repeated predicates, preserves evaluation width/casts, and aborts when factoring raises fanout over policy.
- `FSM_DECODE_RESTRUCTURE` edits combinational next-state/output decode only; it does not recode the state register in the MVP and preserves default/X/reset semantics.

Each capability emits predicted structural effect fields that are checked after synthesis: depth reduction, duplicate-expression reduction, or decode-level reduction. Failure to produce the predicted structural direction triggers an abort diagnostic.

- [ ] **Step 4: Run formal mutation matrices**

For each transform, include at least one correct case and two incorrect mutations. The incorrect suite covers lost priority, signed-width extension, incomplete assignment/latch behavior, reset-output change, X-sensitive equality, and duplicated predicate with altered enable.

Run: `python -m pytest tests/integration/tiny/test_transform_formal_matrix.py -v`

Expected: every registered transformation case passes EQY; every mutation case produces `FAIL` rather than `PASS`.

- [ ] **Step 5: Run synthesis and changed-span authorization for all transforms**

Run: `python -m pytest tests/unit/transforms tests/integration/tiny/test_transform_formal_matrix.py -v`

Expected: deterministic output, no protected edits, no unauthorized files, clean synthesis and declared strict contract for all four operations.

- [ ] **Step 6: Commit the transform set**

```bash
git add src/nova_rtl/transforms tests/unit/transforms tests/integration/tiny/test_transform_formal_matrix.py
git commit -m "feat: add strict boolean predicate and FSM transformations"
```

### Task 10: Add bounded search, experiment ledger records, and contract-partitioned Pareto ranking

**Files:**

- Create: `src/nova_rtl/search/controller.py`
- Create: `src/nova_rtl/search/pareto.py`
- Create: `src/nova_rtl/evaluation/metrics.py`
- Modify: `src/nova_rtl/artifacts/ledger.py`
- Modify: `src/nova_rtl/cli.py`
- Test: `tests/unit/search/test_controller.py`
- Test: `tests/unit/search/test_pareto.py`
- Test: `tests/integration/small/test_deterministic_search.py`

**Interfaces:**

- Produces: `await SearchController.run(request) -> SearchResult`, `ParetoArchive.add(record) -> ParetoUpdate`, and `compare_metrics(baseline, candidate) -> MetricDelta`.
- Consumes: ranked opportunities, `Planner`, registry, evaluator, hard policy, candidate/EDA budgets and ledger.

- [ ] **Step 1: Write budget, comparability and dominance tests**

```python
@pytest.mark.asyncio
async def test_search_never_exceeds_executed_candidate_budget(controller):
    result = await controller.run(request(max_candidates=3, generated_proposals=20))
    assert result.executed_candidates == 3

def test_latency_aware_candidate_cannot_dominate_strict_candidate(archive):
    archive.add(strict_record(wns=-0.05))
    archive.add(latency_aware_record(wns=0.20))
    assert archive.frontier("STRICT_SEQ_EQUIV")[0].selection_class == "PRIMARY_STRICT"

def test_mismatched_view_hash_is_not_comparable():
    with pytest.raises(IncomparableMetricsError):
        compare_metrics(metric_set(view_hash="sha256:" + "1" * 64), metric_set(view_hash="sha256:" + "2" * 64))
```

- [ ] **Step 2: Confirm search tests fail before implementation**

Run: `python -m pytest tests/unit/search -v`

Expected: imports fail for controller/Pareto modules.

- [ ] **Step 3: Implement deterministic heuristic planner and bounded best-first search**

```python
class Planner(Protocol):
    async def propose(self, request: PlannerRequest) -> PlannerResult:
        raise NotImplementedError

class SearchController:
    async def run(self, request: SearchRequest) -> SearchResult:
        """Rank, propose, deduplicate, materialize, evaluate, ledger, Pareto, stop."""
        state = SearchState.from_request(request)
        while state.has_budget and state.has_open_opportunities:
            state = await execute_one_search_iteration(state)
        return state.to_result()
```

Heuristic mapping is registered root cause -> ordered transform operations. Candidate priority considers opportunity severity, transform confidence, estimated proof/EDA cost and current frontier gaps. All tie-breaks use stable IDs and the run seed.

Default funnel budgets are forty generated proposals, twenty materialized/elaborated candidates, twelve fast-synthesis survivors, eight formal-plus-full-STA survivors, four placement/CTS finalists, two routed finalists and one to three Pareto selections. Each count is a ceiling; the controller may stop earlier on closure, infeasibility, exhaustion or stagnation.

- [ ] **Step 4: Implement exact comparability and Pareto rules**

The Pareto vector includes worst required setup/hold WNS/TNS, failing endpoints, mapped/physical area, comparable power, source change size and evaluation runtime. Missing official metrics make a candidate infeasible, not artificially dominant. Pareto records are partitioned by correctness contract.

- [ ] **Step 5: Run deterministic small search twice**

Run:

```bash
nova optimize runs/latest --planner heuristic --max-candidates 8 --seed 20260808
nova replay runs/latest --digest
python -m pytest tests/integration/small/test_deterministic_search.py -v
```

Expected: with cache cleared or held constant, the same inputs and seed produce identical opportunity/proposal/candidate ordering and final frontier identities. Timing runtimes may differ but do not alter deterministic selection tie-breaks.

- [ ] **Step 6: Commit deterministic search**

```bash
git add src/nova_rtl/search src/nova_rtl/evaluation/metrics.py src/nova_rtl/artifacts/ledger.py src/nova_rtl/cli.py tests/unit/search tests/integration/small/test_deterministic_search.py
git commit -m "feat: add bounded deterministic search and Pareto archive"
```

**M5 exit gate:** All four transforms pass their unit/formal matrix; a bounded heuristic search creates alternatives; no budget can be exceeded; the strict frontier is stable and separated from supplementary contracts; and a correct but non-improving candidate is retained as a valid negative result.

**M5 cut rule:** Four reliable transforms are preferable to a large shallow catalog. Additional operations remain conditional until formal mutation coverage exists.

---

## 12. M6 — Constrained Single-Agent Planner

### Task 11: Add role-scoped context, schema-constrained provider abstraction, and fallback

**Files:**

- Create: `src/nova_rtl/planner/interface.py`
- Create: `src/nova_rtl/planner/heuristic.py`
- Create: `src/nova_rtl/planner/context.py`
- Create: `src/nova_rtl/planner/single_agent.py`
- Create: `config/policy/planner.yaml`
- Test: `tests/unit/planner/test_context.py`
- Test: `tests/unit/planner/test_single_agent.py`
- Test: `tests/integration/small/test_planner_equivalence.py`

**Interfaces:**

- `build_context(request: ContextRequest) -> RoleContextPack`.
- `EvidenceProvider.fetch(role_id, evidence_ids) -> Sequence[EvidenceObject]` is read-only and ID-scoped.
- `await SingleAgentPlanner.propose(request: PlannerRequest) -> PlannerResult` returns the same result boundary as `HeuristicPlanner`.

- [ ] **Step 1: Write isolation, stale-hash, schema-repair and fallback tests**

```python
def test_private_context_contains_only_authorized_evidence(context_builder, request):
    pack = context_builder.build(request.for_role("logic_restructuring"))
    assert set(pack.evidence_ids) <= set(request.authorized_evidence_ids)
    assert "raw_repository_path" not in pack.model_dump_json()

def test_stale_snapshot_retrieval_is_denied(provider):
    with pytest.raises(ContextIntegrityError):
        provider.fetch("logic_restructuring", ("path_0042",), expected_snapshot_hash=HASH_B)

@pytest.mark.asyncio
async def test_provider_failure_uses_declared_heuristic_fallback(planner):
    result = await planner.with_provider(always_fails()).propose(planner_request())
    assert result.fallback_used == "HEURISTIC"
    assert result.status == "PASS"
```

- [ ] **Step 2: Run planner tests and confirm the red state**

Run: `python -m pytest tests/unit/planner -v`

Expected: imports fail for context/provider/planner code.

- [ ] **Step 3: Implement common safety envelope and private evidence pack**

The shared envelope includes immutable hashes, protected structures, allowed transform registry, correctness contracts, target opportunity ID, required views, token/deadline limits and fail-closed instructions. The private pack includes only path-local evidence for the single role. Retrieval accepts stable evidence IDs only, logs request/response IDs and tokens, and rejects snapshot mismatch or unauthorized kinds.

- [ ] **Step 4: Implement provider-neutral structured output and one repair attempt**

```python
class StructuredModelProvider(Protocol):
    async def generate(self, *, messages: Sequence[Message], schema: dict, deadline_s: int) -> ProviderResult:
        raise NotImplementedError

class SingleAgentPlanner:
    async def propose(self, request: PlannerRequest) -> PlannerResult:
        # build pack -> call provider -> validate -> one schema-only repair -> fallback/reject
        pack = self.context_builder.build(request.context_request)
        raw = await self.provider.generate(messages=pack.messages, schema=request.output_schema, deadline_s=request.deadline_s)
        return self.validator.validate_repair_or_fallback(raw, request, pack)
```

The schema-repair prompt receives validation errors and the original structured output, but no additional RTL. It may repair shape/enums only. A semantic invalidity such as a protected target is rejected and falls back rather than being prompt-repaired.

- [ ] **Step 5: Prove planner-mode independence of execution**

Run:

```bash
python -m pytest tests/unit/planner tests/integration/small/test_planner_equivalence.py -v
nova optimize runs/latest --planner single_agent --max-candidates 4
```

Expected: heuristic and single-agent proposals with the same normalized transform/target/parameters produce the same candidate source hash; provider outage leaves a visible failed planner result and a successful heuristic fallback result.

- [ ] **Step 6: Commit the single-agent planner**

```bash
git add src/nova_rtl/planner config/policy/planner.yaml tests/unit/planner tests/integration/small/test_planner_equivalence.py
git commit -m "feat: add constrained evidence-scoped single-agent planner"
```

**M6 exit gate:** The model can choose only registered experiments; invalid/stale/private evidence access is denied; structured proposals validate; provider outage falls back safely; and evaluation remains planner-independent.

**M6 cut rule:** If provider integration is unstable, use a recorded validated provider response plus heuristic live fallback. Do not grant the model tool or write access.

---

## 13. M7 — Deterministic Failure-Aware Recovery

### Task 12: Classify failures, compile directives, detect stagnation, and route bounded recovery

**Files:**

- Create: `src/nova_rtl/recovery/classifier.py`
- Create: `src/nova_rtl/recovery/compiler.py`
- Create: `src/nova_rtl/recovery/fingerprint.py`
- Create: `src/nova_rtl/recovery/policy.py`
- Create: `src/nova_rtl/recovery/router.py`
- Create: `config/policy/recovery_rules.yaml`
- Test: `tests/unit/recovery/test_classifier.py`
- Test: `tests/unit/recovery/test_compiler.py`
- Test: `tests/unit/recovery/test_stagnation.py`
- Test: `tests/unit/recovery/test_router.py`
- Test: `tests/integration/small/test_path_migration_recovery.py`

**Interfaces:**

- `classify(stage_result, candidate, baseline, policy) -> FailureEvent`.
- `compile_directive(failure, evidence, rules) -> RepairDirective`.
- `fingerprint(candidate, failure, evidence) -> CandidateFailureFingerprint`.
- `route_recovery(failure, directive, history, budgets, advice=None) -> RecoveryDecision`.

- [ ] **Step 1: Write table-driven classification and safety routing tests**

```python
@pytest.mark.parametrize("fixture,family,action", [
    ("binding_delta", "CONSTRAINT_BINDING_DELTA", "REJECT_CANDIDATE"),
    ("cdc_delta", "CDC_INVARIANT_DELTA", "REJECT_CANDIDATE"),
    ("adapter_timeout", "INFRASTRUCTURE_TRANSIENT", "RETRY_INFRASTRUCTURE"),
    ("path_migration", "CRITICAL_PATH_MIGRATION", "OPPORTUNITY_REANALYSIS"),
    ("area_growth", "AREA_POLICY_VIOLATION", "LOCAL_PARAMETER_REVISION"),
])
def test_failure_family_owns_allowed_action(fixture, family, action):
    event = classify(load_case(fixture))
    decision = route_recovery(event, compile_case(event), history(), budgets())
    assert event.failure_family == family
    assert decision.action == action

def test_agent_advice_cannot_increase_budget(case):
    advice = case.advice.model_copy(update={"requested_lineage_budget": 99})
    with pytest.raises(ValidationError):
        route_recovery(case.failure, case.directive, case.history, budgets(lineage=2), advice)
```

- [ ] **Step 2: Run recovery tests and confirm the red state**

Run: `python -m pytest tests/unit/recovery -v`

Expected: imports fail for recovery modules.

- [ ] **Step 3: Implement normalized classifier and checker-aware rules**

Rules inspect typed stage status/diagnostics/metric deltas rather than raw log substring alone. Every rule has `rule_id`, priority, preconditions, failure family, repairability, severity, retryability, allowed next actions and required evidence kinds. Multiple matches resolve by safety-first priority; unresolved ambiguity routes to `HUMAN_REVIEW`.

- [ ] **Step 4: Implement fingerprints and monotonic escalation**

Candidate similarity is a weighted deterministic combination of cone, family, operation, parameter, AST-diff, mapped-delta, counterexample, lineage and metric-response features. Starting policy uses similarity threshold `0.85`, WNS progress epsilon `0.01 ns`, area progress epsilon `0.10%`, repeated count `2`, and maximum recovery depth `4`; every value is versioned and later calibrated. Two qualifying failures with no WNS/area progress trigger `REPEATED_NON_PROGRESS`. Escalation sequence is local revision -> family switch -> opportunity reanalysis -> parent branch -> abandon; it cannot move backward for the same lineage/failure family.

- [ ] **Step 5: Exercise the path-migration recovery showcase**

Run:

```bash
python -m pytest tests/integration/small/test_path_migration_recovery.py -v
nova optimize runs/latest --planner heuristic --recovery deterministic --max-candidates 6
nova failure inspect fail_path_migration --json
```

Expected: the first correct candidate improves its target but exposes a sibling worst path; the failure becomes `CRITICAL_PATH_MIGRATION`; the directive preserves the successful local mechanism but prohibits identical repetition; the router reanalyzes from the declared parent; the child uses a distinct target or family. If no safe alternative exists, the lineage is explicitly abandoned.

- [ ] **Step 6: Verify infrastructure and protected failures cause no RTL reasoning/edit**

Inject an OpenROAD transient failure and a protected CDC edit. Assert that the transient uses one `RETRY_INFRASTRUCTURE` action with identical RTL hash, while the protected edit uses `REJECT_CANDIDATE` or `STOP_RUN_OR_REQUEST_HUMAN` and creates no child snapshot.

- [ ] **Step 7: Commit deterministic recovery**

```bash
git add src/nova_rtl/recovery config/policy/recovery_rules.yaml tests/unit/recovery tests/integration/small/test_path_migration_recovery.py
git commit -m "feat: add typed bounded failure-aware recovery"
```

**M7 exit gate:** Every non-pass is classified once, every retry has a directive and budget, infrastructure/protected failures cannot mutate RTL, two similar no-progress attempts force escalation, and the showcased path-migration lineage takes a meaningfully different next step.

**M7 cut rule:** Deterministic recovery is Tier 1. Multi-agent recovery advice remains conditional; the reliable value is typed checker feedback and bounded routing.

---

## 14. M8 — Minimum Bounded Role-Scoped Council

### Task 13: Add blinded proposers, independent critics, chair fan-in, and trace accounting

**Files:**

- Create: `src/nova_rtl/planner/council.py`
- Create: `config/policy/council.yaml`
- Create: `config/prompts/timing_forensics.md`
- Create: `config/prompts/logic_or_domain_specialist.md`
- Create: `config/prompts/formal_critic.md`
- Create: `config/prompts/ppa_critic.md`
- Create: `config/prompts/chair.md`
- Test: `tests/unit/planner/test_council_routing.py`
- Test: `tests/unit/planner/test_council_isolation.py`
- Test: `tests/unit/planner/test_council_fanin.py`
- Test: `tests/integration/small/test_council_flow.py`

**Interfaces:**

- `route_roles(opportunity, policy) -> CouncilRoute`.
- `await CouncilPlanner.propose(request) -> PlannerResult` with a linked `CouncilResult`.
- The durable orchestrator calls the council as one bounded planner request; the council cannot schedule EDA or recovery.

- [ ] **Step 1: Write routing, blinding, critic and budget tests**

```python
def test_proposers_do_not_receive_each_others_private_packs(council_fixture):
    result = council_fixture.run_until_critics()
    assert result.role("timing_forensics").visible_private_pack_ids == ("pack_timing",)
    assert result.role("logic_specialist").visible_private_pack_ids == ("pack_logic",)
    assert result.critic_cards_have_author_identity is False

def test_chair_must_disposition_every_critic_objection(council_fixture):
    result = council_fixture.run_with_undispositioned_objection()
    assert result.status == "SCHEMA_ERROR"

def test_council_never_exceeds_role_or_token_budget(council_fixture):
    result = council_fixture.run()
    assert len(result.selected_roles) <= 5
    assert result.total_tokens <= result.token_budget
```

- [ ] **Step 2: Run council tests and confirm the red state**

Run: `python -m pytest tests/unit/planner/test_council_*.py -v`

Expected: imports fail for council runtime.

- [ ] **Step 3: Implement deterministic role routing and independent first round**

The minimum route uses no more than five total reasoning roles, three strategist roles, three final proposals, one revision, 120 seconds and 30,000 aggregate tokens. It is:

```text
Timing Forensics proposer -----\
                               > neutral proposal cards
Routed domain proposer --------/          |
                                          +--> Formal critic --\
                                          +--> PPA critic ------> Chair -> validated shortlist
```

The domain proposer is logic, arithmetic or FSM based on deterministic opportunity category. Independent proposer calls share only the safety envelope and receive distinct private packs. Proposal cards are normalized, semantically deduplicated and neutrally ordered before critics.

- [ ] **Step 4: Implement strict fan-in and one revision maximum**

Mandatory critic failure produces `PARTIAL` and falls back to single-agent/heuristic according to policy. The chair sees normalized proposals, critic artifacts, validated evidence summaries, and budgets—not every raw private pack. Every objection receives `ACCEPT`, `REVISE`, or `REJECT` with a reason and evidence. A revision agent may run once and only for a specific critique.

- [ ] **Step 5: Add trace completeness and token accounting**

Record prompt hash, provider/model fingerprint, envelope/private-pack hashes, retrieval IDs, status, tokens, latency, output hash, blinding flags, normalized order, critiques, dispositions, fallback and deadline. Trace completeness is calculated from required fields; moving evidence to retrieval still counts its tokens.

- [ ] **Step 6: Run the council integration scenario and compare equal EDA budgets**

Run:

```bash
python -m pytest tests/unit/planner/test_council_*.py tests/integration/small/test_council_flow.py -v
nova optimize runs/latest --planner council --max-candidates 4 --council-deadline 120
nova council inspect deliberation_001 --json
```

Expected: two distinct hypotheses appear before cross-sharing; at least one critic objection receives a visible disposition; final proposals pass the unchanged executor boundary; no more than four candidates execute; provider/role failure invokes declared fallback without changing acceptance policy.

- [ ] **Step 7: Commit bounded council support**

```bash
git add src/nova_rtl/planner/council.py config/policy/council.yaml config/prompts tests/unit/planner tests/integration/small/test_council_flow.py
git commit -m "feat: add bounded blinded multi-agent proposal council"
```

**M8 exit gate:** The council is bounded, role-routed, private by default, independently critical, schema-valid, fully traced and interchangeable with other planners. It demonstrates a material critique without owning execution or acceptance.

**M8 cut rule:** If council quality or reliability misses adoption gates, keep it as an inspectable judge-demo mode and use single-agent or heuristic mode for the primary candidate. Do not lower proof or safety requirements to make council output succeed.

---

## 15. M9 — Full Evaluation, Reports, UI, and Submission Rehearsal

### Task 14: Build the evidence report and live/replay UI

**Files:**

- Create: `src/nova_rtl/reports/bundle.py`
- Create: `src/nova_rtl/ui/app.py`
- Create: `src/nova_rtl/ui/view_models.py`
- Create: `tests/unit/reports/test_bundle.py`
- Create: `tests/unit/ui/test_view_models.py`
- Create: `tests/integration/full/test_replay_bundle.py`

**Interfaces:**

- `build_report_bundle(run_id, store, ledger) -> ReportBundle`.
- `build_view_model(run_id, sequence=None) -> DashboardModel`.
- CLI: `nova report <run_id>`, `nova replay <run_id>`, and `nova demo <run_id>`.

- [ ] **Step 1: Write claim-to-artifact and replay-equivalence tests**

```python
def test_every_headline_metric_resolves_to_raw_artifact(full_run):
    bundle = build_report_bundle(full_run.run_id, full_run.store, full_run.ledger)
    for claim in bundle.headline_claims:
        assert claim.evidence_refs
        assert all(full_run.store.exists(ref) for ref in claim.evidence_refs)

def test_live_and_replay_view_models_are_identical(recorded_run):
    assert build_view_model(recorded_run.id) == build_view_model_from_replay(recorded_run.bundle)

def test_pipeline_result_cannot_be_labeled_primary(bundle_fixture):
    with pytest.raises(ReportIntegrityError):
        bundle_fixture.label_as_primary(contract="LATENCY_AWARE")
```

- [ ] **Step 2: Run report/UI model tests and confirm the red state**

Run: `python -m pytest tests/unit/reports tests/unit/ui -v`

Expected: imports fail for report and view-model code.

- [ ] **Step 3: Implement report sections with hash-linked evidence**

The bundle contains:

1. executive result and disclosed claim boundaries;
2. platform/tool/recipe/seed manifest;
3. benchmark clock, CDC and cell-count validation;
4. baseline required-view timing and physical evidence;
5. critical-path cluster and source mapping;
6. planner/council hypotheses, critiques and dispositions;
7. candidate DAG and gate matrix;
8. failure/directive/recovery lineage;
9. primary strict proof and composition coverage;
10. binding/clock/CDC invariant comparison;
11. per-view timing, frequency, area and comparable power tables;
12. strict Pareto frontier and separately labeled secondary lanes;
13. ablation and resource usage;
14. exact selected RTL/patch hashes; and
15. replay instructions and artifact index.

- [ ] **Step 4: Implement the seven evaluator views**

`Design Health`, `Critical-Path Explorer`, `AI Design Review`, `Candidate Tournament`, `Recovery Intelligence`, `Formal Proof`, and `Results` are projections of typed contracts. Advisory AI content is purple, policy/transforms green, proof/safety red, measured EDA blue, historical evidence gray, and recovery amber. No view invents metrics or parses raw reports.

- [ ] **Step 5: Verify offline replay and broken-link rejection**

Run:

```bash
nova report runs/full_reference
nova replay runs/full_reference --offline --verify-all-artifacts
python -m pytest tests/unit/reports tests/unit/ui tests/integration/full/test_replay_bundle.py -v
```

Expected: offline replay makes zero model/tool/network calls, every evidence link resolves, and deleting/corrupting one referenced artifact makes verification fail with its exact ID.

- [ ] **Step 6: Commit reporting and demo views**

```bash
git add src/nova_rtl/reports src/nova_rtl/ui tests/unit/reports tests/unit/ui tests/integration/full/test_replay_bundle.py
git commit -m "feat: add auditable reports and live-replay demo"
```

### Task 15: Run full acceptance, ablations, failure injection, and rehearsal

**Files:**

- Create: `tests/integration/full/test_full_acceptance.py`
- Create: `tests/integration/full/test_failure_injection.py`
- Create: `src/nova_rtl/evaluation/frequency.py`
- Create: `tests/integration/full/test_frequency_sweep.py`
- Create: `config/evaluation/ablation.yaml`
- Create: `config/evaluation/frequency_sweep.yaml`
- Create during execution: `runs/full_reference/`
- Create during execution: `submission/nova_rtl_evidence_bundle.tar.zst`
- Create during execution: `submission/formal_equivalence_report.pdf`
- Create during execution: `submission/timing_ppa_report.pdf`
- Create during execution: `submission/demo_runbook.md`

**Interfaces:**

- Consumes: all prior milestones and locked full benchmark/platform.
- Produces: final acceptance result, equal-budget experiment table, replay bundle, reports and demo runbook.

- [ ] **Step 1: Encode the MVP acceptance gates as executable tests**

```python
def test_full_reference_meets_competition_mvp(run):
    assert run.benchmark.master_count == 5
    assert run.benchmark.generated_count == run.organizer_decision.total_generated_clocks
    assert 45_000 <= run.baseline.cell_count <= 55_000
    assert run.binding.comparison_to_baseline == "EQUIVALENT"
    assert run.cdc.new_unapproved_count == 0
    assert run.cdc.changed_approved_structure_count == 0
    assert run.primary.proof.contract == "STRICT_SEQ_EQUIV"
    assert run.primary.proof.outcome == "PASS"
    assert run.primary.required_views_complete
    assert run.primary.delivered_snapshot_hash == run.primary.proof.gate_hash
    assert run.replay.verify_all_artifacts()
```

- [ ] **Step 2: Run the full baseline and bounded optimizer**

Run:

```bash
nova benchmark generate --profile full --output benchmark/build/full
nova init benchmark/build/full/project.yaml
nova analyze runs/full_reference
nova optimize runs/full_reference --planner council --max-candidates 40 --openroad-finalists 4
nova verify runs/full_reference/selected_primary
nova physical runs/full_reference/selected_primary --stage routed
```

Expected: every tool result is persisted even on failure; the selected primary exists only if all hard gates pass. If no candidate is feasible, the run is an honest failed rehearsal and blocks release while preserving diagnosis.

- [ ] **Step 3: Run required negative and recovery scenarios**

Run: `python -m pytest tests/integration/full/test_failure_injection.py -v`

Cases must demonstrate formal counterexample, hold regression, changed SDC binding, new CDC crossing, formal compilation delta, area-policy failure, path migration, physical non-correlation, repeated non-progress and transient adapter failure. Each case asserts the expected family and safe recovery disposition.

- [ ] **Step 4: Run equal-budget planner/recovery ablations**

The minimum matrix is:

```text
H  = heuristic planner, deterministic recovery
S  = single-agent planner, deterministic recovery
MC = minimum council, deterministic recovery
F0 = fixed generic retry baseline
F4 = typed classifier + directive + stagnation + bounded escalation
```

Each variant receives identical opportunities, transforms, maximum executed candidates, formal slots, STA/OpenROAD slots, analysis views, seeds and wall-clock ceiling. Record schema validity, distinct mechanisms, formal pass yield, feasible yield, best strict PPA, time, tokens, semantic duplicates and EDA jobs avoided.

Council becomes the recommended default only if it improves feasible-candidate yield by at least 15% relative to single-agent under equal EDA budget, matches or improves best strict PPA, reduces semantic duplicates/repeated failures, produces at least 98% schema-valid shortlists after one schema repair, preserves all safety gates, stays within 120-second median latency, remains below 2x single-agent cost unless disclosed PPA justifies it, and reaches greater than 99% trace completeness. Deterministic recovery becomes default only if it reduces same-mechanism retries by at least 30%, improves recovered-feasible yield, keeps at least 95% of infrastructure failures from triggering RTL reasoning, routes no protected failure to unsafe repair, resolves at least 99% of directive evidence refs, remains bounded, and never weakens constraints or proof.

- [ ] **Step 5: Run a controlled baseline/candidate frequency sweep**

Create one `FrequencySweepContract` for the selected target master domain. The deterministic evaluator varies only that master period through a hashed overlay, keeps all non-target clocks/exceptions/views fixed, and requires both setup and hold policy at each passing point.

Run:

```bash
nova evaluate-frequency runs/full_reference --candidate baseline --contract config/evaluation/frequency_sweep.yaml
nova evaluate-frequency runs/full_reference --candidate selected_primary --contract config/evaluation/frequency_sweep.yaml
python -m pytest tests/integration/full/test_frequency_sweep.py -v
```

Expected: both sweeps share a contract hash, every trial has raw STA evidence, and the report distinguishes swept Fmax from any algebraic estimate.

- [ ] **Step 6: Seal the exact delivered candidate**

Copy no mutable working directory into the report. Re-hash the selected source snapshot, patch, SDC, binding manifest, analysis views, activity contract, formal model, platform, recipes and raw final reports. Re-run strict proof and required final measurement on that exact snapshot. Store a `FINALIST_SELECTED` event only after hashes match.

- [ ] **Step 7: Build and independently verify the submission bundle**

Run:

```bash
nova report runs/full_reference --output submission
nova replay runs/full_reference --offline --verify-all-artifacts
python -m pytest tests/integration/full/test_full_acceptance.py tests/integration/full/test_frequency_sweep.py tests/integration/full/test_replay_bundle.py -v
sha256sum submission/nova_rtl_evidence_bundle.tar.zst submission/formal_equivalence_report.pdf submission/timing_ppa_report.pdf
```

Expected: all acceptance tests pass, replay verification exits `0`, and report/bundle hashes are recorded in `submission/demo_runbook.md`.

- [ ] **Step 8: Rehearse the ten-minute judge sequence and degraded modes**

The runbook must time these actions:

1. benchmark and baseline health — 60 seconds;
2. source-mapped failing path — 60 seconds;
3. blinded proposals and critic disposition — 90 seconds;
4. deterministic transformation/gates — 75 seconds;
5. real failed candidate and recovery — 90 seconds;
6. strict proof plus binding/CDC invariants — 90 seconds;
7. multi-corner/physical PPA and Pareto result — 90 seconds;
8. artifact drill-down and closing utility statement — 45 seconds.

Rehearse with provider unavailable, network unavailable and one live EDA timeout. The recorded main run must still complete the story; a tiny live run is optional authenticity evidence.

- [ ] **Step 9: Commit evaluation configuration and tests; tag the sealed rehearsal**

```bash
git add src/nova_rtl/evaluation/frequency.py tests/integration/full config/evaluation submission/demo_runbook.md
git commit -m "test: seal full NOVA-RTL hackathon acceptance flow"
git tag -a nova-rtl-hackathon-rehearsal-v1 -m "Verified judge-ready rehearsal bundle"
```

**M9 exit gate:** All executable MVP acceptance tests pass on a full recorded run, the delivered primary snapshot re-proves, official PPA identities match, every claim resolves to raw evidence, ablations use equal EDA budgets, offline replay succeeds, and the demo has been rehearsed under degraded conditions.

**M9 cut rule:** Protect the sealed bundle. Later conditional features use new run IDs and cannot replace the verified primary unless they independently pass every M9 gate.

---

## 16. Post-MVP Architecture Completion Track

M0–M9 are sufficient for the signed hackathon competition MVP. The following milestones complete the reserved architectural lanes after the sealed primary submission exists. They use new run IDs and cannot weaken or relabel the M9 primary artifact.

### Task 16 / M10: Implement separately labeled retiming and elastic-pipeline lanes

**Files:**

- Create: `src/nova_rtl/transforms/retiming.py`
- Create: `src/nova_rtl/transforms/elastic_pipeline.py`
- Create: `src/nova_rtl/formal/retiming.py`
- Create: `src/nova_rtl/formal/transactions.py`
- Create: `benchmark/formal/transaction_regions.yaml`
- Test: `tests/unit/transforms/test_retiming.py`
- Test: `tests/unit/transforms/test_elastic_pipeline.py`
- Test: `tests/integration/small/test_supplementary_contracts.py`

**Interfaces:** Registered capabilities emit `RETIMING_EQUIV` or `LATENCY_AWARE`; each `ProofResult` and `ParetoRecord` remains in its own contract class.

- [ ] Write failing tests showing that retiming across asynchronous reset/unmodelled enable is rejected, pipeline insertion outside a declared elastic region is rejected, and neither result can enter the primary strict frontier.
- [ ] Run `python -m pytest tests/unit/transforms/test_retiming.py tests/unit/transforms/test_elastic_pipeline.py -v` and observe missing-capability failures.
- [ ] Implement forward/backward retiming with explicit register/reset/enable correspondence and deterministic state-mapping artifacts.
- [ ] Implement pipeline insertion only for manifest-declared valid/ready regions with explicit fixed/bounded latency, ordering, no-loss, no-duplication, backpressure and reset-flush properties.
- [ ] Run `python -m pytest tests/integration/small/test_supplementary_contracts.py -v`; expected: valid retiming/property cases pass, injected reset/ordering/loss mutations fail, and reports label both lanes supplementary.
- [ ] Stage and commit the supplementary proof lanes:

```bash
git add src/nova_rtl/transforms/retiming.py src/nova_rtl/transforms/elastic_pipeline.py src/nova_rtl/formal/retiming.py src/nova_rtl/formal/transactions.py benchmark/formal/transaction_regions.yaml tests/unit/transforms/test_retiming.py tests/unit/transforms/test_elastic_pipeline.py tests/integration/small/test_supplementary_contracts.py
git commit -m "feat: add supplementary retiming and elastic pipeline lanes"
```

**M10 exit gate:** At least one retimed and one latency-aware fixture pass their correct proof contract, mutation tests fail as expected, and neither can dominate or replace `PRIMARY_STRICT`.

### Task 17 / M11: Expand the council and add selective advisory recovery

**Files:**

- Modify: `src/nova_rtl/planner/council.py`
- Create: `src/nova_rtl/recovery/advice.py`
- Create: `config/prompts/arithmetic_specialist.md`
- Create: `config/prompts/fsm_specialist.md`
- Create: `config/prompts/retiming_specialist.md`
- Create: `config/prompts/elastic_pipeline_specialist.md`
- Create: `config/prompts/fanout_resource_specialist.md`
- Create: `config/prompts/recovery_advisor.md`
- Test: `tests/unit/recovery/test_advice_authority.py`
- Test: `tests/integration/small/test_targeted_recovery_council.py`

**Interfaces:** `await RecoveryAdvisor.advise(request: RecoveryRequest) -> RecoveryAdvice`; the existing deterministic router remains the only producer of `RecoveryDecision`.

- [ ] Write failing tests proving that advice cannot change failure family, stage status, protected policy or remaining budgets, and that only failure-routed roles receive private recovery evidence.
- [ ] Run `python -m pytest tests/unit/recovery/test_advice_authority.py -v` and observe missing advisor failures.
- [ ] Add the full specialist registry with deterministic category routing; do not invoke every role for every opportunity.
- [ ] Add fresh-session recovery packs containing the `RepairDirective`, failed-mechanism exclusion, selected raw evidence IDs and bounded retrieval rights.
- [ ] Validate advice against the router allowlist, remaining budgets and monotonic escalation before emitting the authoritative decision.
- [ ] Run `python -m pytest tests/integration/small/test_targeted_recovery_council.py -v`; expected: advisory recovery improves or abandons the scenario without repeating an excluded family or expanding authority.
- [ ] Stage and commit the routed recovery council:

```bash
git add src/nova_rtl/planner/council.py src/nova_rtl/recovery/advice.py config/prompts/arithmetic_specialist.md config/prompts/fsm_specialist.md config/prompts/retiming_specialist.md config/prompts/elastic_pipeline_specialist.md config/prompts/fanout_resource_specialist.md config/prompts/recovery_advisor.md tests/unit/recovery/test_advice_authority.py tests/integration/small/test_targeted_recovery_council.py
git commit -m "feat: add routed specialist and recovery advisory councils"
```

**M11 exit gate:** Equal-budget experiments show whether the extended council and targeted `RecoveryAdvisor` meet architecture adoption gates. Features that miss a gate remain experimental and inspectable.

### Task 18 / M12: Add the production-shaped service, CI integration, and security hardening

**Files:**

- Create: `src/nova_rtl/service/api.py`
- Create: `src/nova_rtl/service/auth.py`
- Create: `src/nova_rtl/service/events.py`
- Create: `deploy/container/Dockerfile`
- Create: `deploy/container/compose.yaml`
- Create: `.github/workflows/tiny-regression.yaml`
- Create: `tests/unit/service/test_api_authority.py`
- Create: `tests/security/test_isolation.py`
- Create: `tests/security/test_prompt_injection.py`

**Interfaces:** The API exposes the endpoints in Architecture Section 24.2 and delegates to the durable orchestrator. Browser/API inputs never become commands or authoritative metrics.

- [ ] Write failing authorization tests showing that API clients cannot submit shell commands, metrics, acceptance status, budget increases, arbitrary artifact paths or SDC mutations.
- [ ] Run `python -m pytest tests/unit/service tests/security -v` and observe missing service/security failures.
- [ ] Implement authenticated run/query/cancel endpoints and server-sent event replay using validated request contracts and opaque entity IDs.
- [ ] Containerize planner and offline EDA authorities separately; EDA worker images run without network and with CPU/memory/time quotas.
- [ ] Add comment/prompt-injection fixtures, secret-redaction checks, retrieval allowlist tests, path traversal rejection, artifact hash verification and trace-export controls.
- [ ] Run `python -m pytest tests/unit tests/security tests/integration/tiny -v`; expected: zero failures and no planner/API path can import or invoke the source-writing/tool-runner authority directly.
- [ ] Stage and commit service hardening:

```bash
git add src/nova_rtl/service deploy/container/Dockerfile deploy/container/compose.yaml .github/workflows/tiny-regression.yaml tests/unit/service/test_api_authority.py tests/security/test_isolation.py tests/security/test_prompt_injection.py
git commit -m "feat: add governed service deployment and CI hardening"
```

**M12 exit gate:** The service preserves the same contracts and replay semantics, EDA workers are isolated/offline, authority-boundary tests pass, and CI runs the tiny strict slice on every change.

---

## 17. Milestone Evidence Register

Each milestone produces a compact sign-off packet. These are required outputs, not informal notes.

| Milestone | Sign-off artifacts |
|---|---|
| M0 | `DoctorReport`, platform lock, two-view smoke reports, organizer-decision record |
| M1 | Exported schemas, schema test report, artifact corruption test, replay digest |
| M2 | Benchmark snapshot, calibration curve, baseline `StageResult` set, clock/CDC/binding/formal manifests |
| M3 | Evidence graph hash, path clusters, source map, ranked opportunity set |
| M4 | Proposal, exact patch, candidate lineage, strict proof, all gate results, before/after metrics |
| M5 | Transform formal matrix, bounded-search trace, strict Pareto archive, valid negative result |
| M6 | Context/retrieval trace, provider fingerprint, schema-repair/fallback evidence, planner comparison |
| M7 | Failure labels, directives, fingerprints, budgets, decisions, recovery descendant outcome |
| M8 | Private-pack hashes, blinded submissions, critiques/dispositions, token trace, fallback result |
| M9 | Full acceptance results, reports, sealed replay bundle, ablations, runbook and artifact hashes |

A milestone is green only when its packet exists and its stated verification command has exited successfully in the same implementation checkpoint.

---

## 18. Objective and Deliverable Traceability

| Hackathon requirement | Architecture authority | Implementation milestone | Final evidence |
|---|---|---|---|
| Analyze RTL against timing constraints | Sections 7, 10, 16 | M0–M2 | Manifest, binding report, required-view `StageResult`s |
| Identify critical paths/violations | Sections 10–11 | M3 | Path records, evidence graph, opportunity ranking |
| GenAI recommendations | Sections 12–14 | M6 and M8 | Typed proposals, private packs, critiques, chair dispositions |
| Logic restructuring | Section 15.2 | M4–M5 | Priority, boolean and predicate candidate bundles |
| FSM/PSM optimization | Sections 3.3 and 15.2 | M5 | `FSM_DECODE_RESTRUCTURE` proof and timing result |
| Retiming/pipelining | Sections 15.3–15.4 and 17.4–17.5 | Conditional enhancement after M9 | Separately labeled proposal/result contracts; never primary |
| Timing, frequency and PPA | Sections 16, 20, 26 | M5 and M9 | Comparable multi-view tables, physical reports, Pareto archive |
| Formal equivalence | Section 17 | M4 and M9 | Primary EQY strict proof and exact-snapshot re-proof |
| Five async masters/21 generated each | Section 22 | M2 and M9 | Clock inventory/lineage and organizer decision |
| CDC and dividers | Sections 10.7, 17.2, 22 | M2, M4, M9 | Protected structures, inventory diff, formal clock model |
| Approximately 50K cells | Section 22.5 | M2 | Bounded calibration history and mapped cell report |
| Optimized RTL | Sections 15–16 | M4–M5 | Immutable candidate snapshot and exact patch |
| Formal verification report | Sections 17 and 25 | M9 | Hash-linked formal report |
| Interactive demo | Sections 24–25 | M9 | Seven-view live/replay UI and rehearsed runbook |
| Efficient practical utility | Sections 23–24, 28 | M1, M5, M7, M9 | Cache/replay, bounded search, avoided jobs, CLI artifacts |

No challenge deliverable is owned solely by an agent subsystem. Each terminates in deterministic or EDA evidence.

---

## 19. Implementation Risk Register and Stop Conditions

| Risk | Earliest detector | Stop/mitigation action |
|---|---|---|
| No usable multi-corner platform | M0 smoke | Select the next policy-compliant platform; publish no benchmark metric |
| Full benchmark exceeds runtime/memory | M2 calibration | Reduce active replication while remaining 45K–55K; cap parallelism; retain full profile for recorded run |
| Generated clocks optimized away | M2 validation | Make consumers architecturally observable; do not add false clocks |
| OpenSTA/OpenROAD parser drift | Golden tests | Mark adapter infrastructure failure; preserve raw report; update versioned parser fixture |
| EQY cannot close full design | M4 | Partition with discharged boundaries and composition manifest; never call a local cone proof final |
| Transform yields no mapped depth change | M4/M5 guard | Record valid negative result and switch mechanism; do not claim source prettification as timing optimization |
| Hold regresses while setup improves | Required-view gate | Reject candidate or select another Pareto candidate |
| Constraint selector silently changes | Binding audit | Reject candidate before expensive proof/physical work |
| CDC structure changes | CDC invariant audit | Reject candidate; protected structure cannot be locally repaired |
| LLM returns unsupported edit | Proposal validator | One schema-shape repair only, then fallback |
| Council adds cost without yield | M8 equal-budget test | Keep single-agent/heuristic as default; present council as experimental evidence |
| Recovery repeats same mechanism | M7 stagnation | Exclude family, reanalyze/branch, or abandon under monotonic budget |
| Physical result contradicts STA | M9 | Label physical non-correlation; retain as negative evidence; do not publish pre-layout win as final |
| Live demo dependency fails | M9 degraded rehearsal | Switch to verified replay with identical schema and disclose mode |

Run-level stop conditions are: missing mandatory platform/tool; invalid baseline; incomplete required view; unconstrained endpoint without reviewed exception; broken formal-model identity; no valid generated-clock lineage; unresolved binding selector; ambiguous/new CDC crossing; corrupted immutable artifact; or exhausted global budget. These conditions produce typed results and never trigger unrestricted repair.

---

## 20. Definition of Done

### 20.1 Architecture implementation sign-off

The implementation is architecture-conformant when:

- one deterministic orchestrator owns global state and retries;
- all interfaces use the canonical schemas in this plan;
- planner output is advisory and executor input is validated;
- only the deterministic executor writes candidate RTL;
- acceptance is defined by formal, constraint/CDC and measured EDA gates;
- required-view and activity comparability are enforced by hashes;
- strict equivalence owns the primary result;
- failures produce immutable typed recovery evidence;
- the council is bounded and private by role; and
- replay reconstructs the run without recomputation.

### 20.2 Hackathon release sign-off

Release sign-off requires:

- every Tier-0 and Tier-1 item in Section 2 is implemented or explicitly evidenced by the M9 bundle;
- all M0–M9 exit gates are green;
- the exact delivered primary candidate passes final strict proof and required physical/timing gates;
- no open severity-1 correctness, constraint, CDC, provenance or replay defect exists;
- all headline numerical claims have resolvable raw artifacts and matching comparison identities;
- all supplementary retiming/pipeline results, if present, are visibly separated from the primary claim;
- the demo runbook completes in ten minutes using offline replay; and
- the submission bundle hash is recorded after the final verification run.

### 20.3 Conditional features do not block sign-off

The architecture and competition MVP can be signed off without a latency-changing executor, full specialist roster, production API, multi-platform support or multi-agent recovery council. Their contracts and boundaries are already reserved, so adding them after the primary system is stable does not require architectural redesign.

---

## 21. Plan Execution Protocol

Implement tasks in numerical order. At each task:

1. create the specified failing test;
2. run the exact focused command and observe the expected failure;
3. implement only the scoped interface;
4. run focused tests to green;
5. run the milestone regression set;
6. inspect generated artifacts and hashes;
7. commit the independently testable change; and
8. update the milestone evidence register with command, exit status, timestamp and artifact IDs.

Before crossing a milestone boundary, run:

```bash
python -m pytest tests/unit -q
python -m nova_rtl.contracts.schema_export schemas/canonical --check
git status --short
```

Expected: unit suite has zero failures; schema export has no drift; version-control status contains only intentional run-independent changes. Tool-dependent integration commands for that milestone must also have fresh exit-zero evidence.

Do not batch M0–M4 into one implementation change. Each is an independent review gate and can reveal a foundational mistake that would make later agent work wasteful.

---

## 22. Plan Sign-Off Record

**Plan status:** Signed off for implementation after canonical-contract normalization and architecture/plan consistency review on 2026-08-14.

**Architecture status:** Signed off for implementation. `architecture.md` and its authoritative design-spec copy are byte-identical.

**Scope of this sign-off:** This is document, architecture, and implementation-plan sign-off. It is not M9 submission release sign-off; release still requires the measured tool, proof, PPA, replay, and demo gates in Section 20.

**Known execution-time decisions:** The actual platform/corner paths and organizer clarifications are intentionally resolved and hashed in M0. They are configuration facts, not unresolved architecture semantics, because this plan defines conservative defaults, selection rules, failure behavior and artifact outputs.

**Primary implementation checkpoint:** Do not begin M6–M8 until M4 has fresh green strict-equivalence evidence.

**Primary release checkpoint:** Do not label the submission judge-ready until M9 accepts the exact delivered snapshot and offline replay bundle.

This plan is the execution authority for the hackathon MVP. If it conflicts with the integrated architecture on safety or correctness, the architecture governs; if it adds implementation detail within those invariants, this plan governs.
