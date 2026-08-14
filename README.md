# NOVA-RTL

NOVA-RTL is an evidence-grounded framework for Generative-AI-assisted RTL timing
optimization. It combines bounded AI planning, deterministic RTL transformations,
formal equivalence, multi-corner timing analysis, physical evaluation, and auditable
failure-aware recovery.

> **Status:** M0 repository scaffold. The CLI and bootstrap dependency doctor are
> available; the optimization, EDA, formal, and multi-agent flows remain under
> implementation according to the signed-off plan.

## Documentation

- [Architecture](docs/architecture.md) — authoritative system design and safety contracts
- [Implementation plan](docs/plan.md) — milestone-by-milestone build and acceptance plan
- [Hackathon synopsis](docs/synopsis.md) — concise project overview

## Prerequisites

- Conda or Miniconda
- Git and Make
- Yosys, OpenSTA, OpenROAD, EQY, and SymbiYosys for EDA stages

The Python environment does not install the EDA toolchain. M0 will pin those tools,
the OpenROAD platform, and timing corners separately.

## Setup

```bash
conda env create --file environment.yml
conda activate nova-rtl
python -m pip install --requirement requirements.txt
```

If the environment already exists:

```bash
conda env update --file environment.yml --prune
conda activate nova-rtl
python -m pip install --requirement requirements.txt
```

## Run

```bash
nova --help
nova version
nova doctor
nova doctor --json
```

`nova doctor` checks every required executable. It exits with status `2` when a
dependency is missing; that is expected until the M0 toolchain is installed and pinned.

Developer checks:

```bash
make test
make lint
make check
```

## Repository Structure

```text
docs/                  Signed-off architecture, plan, and synopsis
src/nova_rtl/          Python package and nova CLI
tests/                 Unit and integration tests
config/                Challenge, platform, policy, analysis, and formal inputs
benchmark/             RTL benchmark, constraints, harnesses, and expected manifests
schemas/canonical/     Exported versioned JSON Schemas
.github/workflows/     Continuous-integration checks
```

Generated runs and EDA artifacts are intentionally excluded from Git. Reproducible,
reviewed inputs and canonical schemas will be committed as their milestones land.
