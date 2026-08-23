"""Behavioral tests for the common adapter execution boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from nova_rtl.adapters.base import (
    AdapterInvocation,
    ToolFingerprintMismatchError,
    execute_tool_job,
    schedule_tool_job,
)
from nova_rtl.adapters.eqy import EQYAdapter
from nova_rtl.adapters.openroad import OpenROADAdapter
from nova_rtl.adapters.opensta import OpenSTAAdapter
from nova_rtl.adapters.sby import SymbiYosysAdapter
from nova_rtl.adapters.simulation import SimulationAdapter
from nova_rtl.adapters.yosys import YosysAdapter
from nova_rtl.artifacts.store import ArtifactStore
from nova_rtl.contracts.base import StageInputHashes
from nova_rtl.contracts.execution import ResourceLimits, ToolJob
from nova_rtl.contracts.platform import PROBE_VERSION_ARGUMENTS, ToolFingerprint
from nova_rtl.orchestrator.scheduler import (
    BoundedScheduler,
    JobKindLimits,
    SchedulerPolicy,
)


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _write_fake_tool(path: Path, *, sleep_seconds: float = 0.0) -> ToolFingerprint:
    body = f"""#!/usr/bin/python3
import os
import pathlib
import sys
import time

if '-V' in sys.argv:
    print('fake-iverilog 1.0')
    raise SystemExit(0)

time.sleep({sleep_seconds})
print('NOVA_OBSERVABLE_ACTIVITY_PASS checksum=12345678')
print('SECRET_PRESENT=' + str('FORBIDDEN_SECRET' in os.environ))
print('ARGV=' + repr(sys.argv[1:]))
print('raw stderr', file=sys.stderr)
pathlib.Path('simulation.out').write_text('waveform identity\\n', encoding='utf-8')
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return ToolFingerprint(
        tool_id="iverilog",
        executable=str(path.resolve()),
        version="fake-iverilog 1.0",
        version_args=("-V",),
        executable_sha256=_hash_bytes(path.read_bytes()),
        build_hash=_hash_bytes(path.read_bytes() + b"fake-iverilog 1.0\n"),
        adapter_version="simulation-adapter-v1",
        container_digest=None,
    )


def _job(
    store: ArtifactStore,
    *,
    wall_time_ms: int = 2_000,
    stage: str = "SIMULATION",
    analysis_view_id: str | None = None,
) -> ToolJob:
    now = datetime.now(UTC)
    source = store.put_bytes(
        b"module tiny; endmodule\n",
        media_type="text/x-systemverilog",
        classification="RESTRICTED_RTL",
    )
    hashes = StageInputHashes(
        rtl_snapshot=_hash_bytes(b"rtl"),
        design_contract=_hash_bytes(b"design"),
        constraints=_hash_bytes(b"constraints") if analysis_view_id else None,
        constraint_binding=_hash_bytes(b"binding") if analysis_view_id else None,
        analysis_view=_hash_bytes(b"view") if analysis_view_id else None,
        power_activity=None,
        platform_lock=_hash_bytes(b"platform"),
        tool_recipe=_hash_bytes(b"recipe"),
        formal_model=(
            _hash_bytes(b"formal")
            if stage in {"EQY_SMOKE", "FORMAL_EQUIVALENCE", "FORMAL_MODEL_PREFLIGHT"}
            else None
        ),
        parent_stage_result=None,
        extensions={},
    )
    return ToolJob(
        tool_job_id="tool_job_sim_001",
        stage_result_id="stage_sim_001",
        run_id="run_phase5",
        candidate_id="baseline",
        stage=stage,
        analysis_view_id=analysis_view_id,
        design_contract_hash=hashes.design_contract,
        input_artifact_refs=(source,),
        input_hashes=hashes,
        resource_limits=ResourceLimits(
            cpu_cores=1,
            memory_bytes=512 * 1024 * 1024,
            wall_time_ms=wall_time_ms,
            max_output_bytes=1024 * 1024,
        ),
        deadline=now + timedelta(seconds=30),
        artifact_namespace="artifact://runs/run_phase5/stages/stage_sim_001/",
        requested_at=now,
    )


def _adapter(
    tmp_path: Path,
    store: ArtifactStore,
    fingerprint: ToolFingerprint,
    *,
    argv_tail: tuple[str, ...] = ("--run",),
) -> SimulationAdapter:
    return SimulationAdapter(
        tool_fingerprint=fingerprint,
        artifact_store=store,
        workspace_root=tmp_path / "workspaces",
        invocation=AdapterInvocation(
            argv_tail=argv_tail,
            expected_output_paths=("simulation.out",),
            environment={"NOVA_MODE": "test"},
        ),
    )


def test_common_executor_is_shell_free_isolated_and_preserves_raw_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    fingerprint = _write_fake_tool(tmp_path / "fake_iverilog")
    injected = f"; touch {tmp_path / 'shell_was_used'}"
    adapter = _adapter(tmp_path, store, fingerprint, argv_tail=(injected,))
    monkeypatch.setenv("FORBIDDEN_SECRET", "must-not-leak")

    result = execute_tool_job(adapter, _job(store), store)

    assert result.status == "PASS"
    assert result.stage == "SIMULATION"
    assert not (tmp_path / "shell_was_used").exists()
    assert adapter.last_prepared_command is not None
    assert adapter.last_prepared_command.argv == (fingerprint.executable, injected)
    assert adapter.last_prepared_command.working_directory == (
        tmp_path / "workspaces/run_phase5/stage_sim_001"
    ).resolve()
    assert set(adapter.last_prepared_command.environment) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "NOVA_MODE",
        "PATH",
        "TMPDIR",
    }
    artifacts = {item.artifact_id: item for item in result.raw_artifacts}
    assert {
        "stage_sim_001_argv",
        "stage_sim_001_prepared_command",
        "stage_sim_001_simulation_out",
        "stage_sim_001_stderr",
        "stage_sim_001_stdout",
    } <= set(artifacts)
    stdout = store.open_verified(artifacts["stage_sim_001_stdout"]).read().decode()
    stderr = store.open_verified(artifacts["stage_sim_001_stderr"]).read().decode()
    assert "SECRET_PRESENT=False" in stdout
    assert injected in stdout
    assert stderr == "raw stderr\n"
    assert store.open_verified(artifacts["stage_sim_001_simulation_out"]).read() == (
        b"waveform identity\n"
    )


def test_fingerprint_mismatch_stops_before_tool_execution(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    executable = tmp_path / "fake_iverilog"
    fingerprint = _write_fake_tool(executable)
    executable.write_text(executable.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    marker = tmp_path / "must_not_exist"
    adapter = _adapter(tmp_path, store, fingerprint, argv_tail=(str(marker),))

    with pytest.raises(ToolFingerprintMismatchError, match="SHA-256"):
        execute_tool_job(adapter, _job(store), store)

    assert not marker.exists()
    assert not (tmp_path / "workspaces").exists()


def test_deadline_timeout_fails_closed_and_keeps_partial_logs(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    fingerprint = _write_fake_tool(tmp_path / "slow_iverilog", sleep_seconds=0.5)
    adapter = _adapter(tmp_path, store, fingerprint)

    result = execute_tool_job(adapter, _job(store, wall_time_ms=50), store)

    assert result.status == "INFRASTRUCTURE_ERROR"
    assert result.diagnostics[0].code == "INFRASTRUCTURE_TOOL_TIMEOUT"
    artifacts = {item.artifact_id: item for item in result.raw_artifacts}
    assert "stage_sim_001_stdout" in artifacts
    assert "stage_sim_001_stderr" in artifacts
    assert "stage_sim_001_argv" in artifacts
    assert store.open_verified(artifacts["stage_sim_001_stdout"]).read() == b""
    assert store.open_verified(artifacts["stage_sim_001_stderr"]).read() == b""


def test_fingerprint_version_probe_is_verified_exactly(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    fingerprint = _write_fake_tool(tmp_path / "fake_iverilog").model_copy(
        update={"version": "different version"}
    )
    adapter = _adapter(tmp_path, store, fingerprint)

    with pytest.raises(ToolFingerprintMismatchError, match="version"):
        execute_tool_job(adapter, _job(store), store)


def test_fingerprint_probe_uses_the_reviewed_tool_environment(tmp_path: Path) -> None:
    executable = tmp_path / "environment_bound_tool"
    body = """#!/usr/bin/python3
import os
import sys
if '-V' in sys.argv and os.environ.get('NOVA_PROBE_TOKEN') == 'reviewed':
    print('environment-bound 1.0')
    raise SystemExit(0)
raise SystemExit(9)
"""
    executable.write_text(body, encoding="utf-8")
    executable.chmod(0o755)
    fingerprint = ToolFingerprint(
        tool_id="iverilog",
        executable=str(executable.resolve()),
        version="environment-bound 1.0",
        version_args=("-V",),
        executable_sha256=_hash_bytes(executable.read_bytes()),
        build_hash=_hash_bytes(executable.read_bytes() + b"environment-bound 1.0\n"),
        adapter_version="simulation-adapter-v1",
        container_digest=None,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    adapter = SimulationAdapter(
        tool_fingerprint=fingerprint,
        artifact_store=store,
        workspace_root=tmp_path / "workspaces",
        invocation=AdapterInvocation(
            argv_tail=("--run",),
            expected_output_paths=(),
            environment={"NOVA_PROBE_TOKEN": "reviewed"},
        ),
    )

    adapter.verify_fingerprint()


def test_adapter_environment_cannot_override_the_fixed_sandbox() -> None:
    with pytest.raises(ValueError, match="cannot override fixed variables: PATH"):
        AdapterInvocation(
            argv_tail=("--run",),
            expected_output_paths=(),
            environment={"PATH": "/unreviewed"},
        )


def test_executor_rejects_a_different_artifact_store_instance(tmp_path: Path) -> None:
    bound_store = ArtifactStore(tmp_path / "bound_artifacts")
    other_store = ArtifactStore(tmp_path / "other_artifacts")
    fingerprint = _write_fake_tool(tmp_path / "fake_iverilog")
    adapter = _adapter(tmp_path, bound_store, fingerprint)

    with pytest.raises(ValueError, match="artifact store"):
        execute_tool_job(adapter, _job(bound_store), other_store)


def test_tool_job_runs_through_the_existing_bounded_scheduler(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    fingerprint = _write_fake_tool(tmp_path / "fake_iverilog")
    adapter = _adapter(tmp_path, store, fingerprint)
    limits = JobKindLimits(
        max_concurrency=1,
        cpu_cores=2,
        memory_bytes=1024 * 1024 * 1024,
        wall_time_ms=5_000,
    )
    scheduler = BoundedScheduler(
        tmp_path / "scheduler.sqlite3",
        SchedulerPolicy(
            kind_limits={
                "SYNTHESIS_STA": limits,
                "FORMAL": limits,
                "OPENROAD": limits,
                "PLANNER": limits,
            },
            max_candidates=4,
            max_tokens=1,
            max_retry_depth=1,
        ),
    )
    job = _job(store)

    execution = schedule_tool_job(scheduler, adapter, job, store)
    result = execution.result()

    assert result.status == "PASS"
    assert scheduler.job(job.tool_job_id).status == "COMPLETED"
    assert scheduler.job(job.tool_job_id).partial_log_artifacts == result.raw_artifacts


@pytest.mark.parametrize(
    ("adapter_type", "tool_id", "stage", "analysis_view_id", "fixture"),
    [
        (YosysAdapter, "yosys", "YOSYS_SYNTH", None, "yosys/pass.rpt"),
        (
            OpenSTAAdapter,
            "opensta",
            "OPENSTA_FULL",
            "func_setup_slow",
            "opensta/pass_setup.rpt",
        ),
        (
            OpenROADAdapter,
            "openroad",
            "OPENROAD_ROUTED",
            "func_setup_slow",
            "openroad/pass.rpt",
        ),
        (EQYAdapter, "eqy", "EQY_SMOKE", None, "eqy/pass.log"),
        (SymbiYosysAdapter, "sby", "FORMAL_EQUIVALENCE", None, "sby/pass.log"),
        (
            SimulationAdapter,
            "iverilog",
            "SIMULATION",
            None,
            "simulation/pass.log",
        ),
    ],
)
def test_every_adapter_finalizes_a_canonical_stage_result_with_resolvable_raw_artifacts(
    tmp_path: Path,
    adapter_type: type,
    tool_id: str,
    stage: str,
    analysis_view_id: str | None,
    fixture: str,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    report = (
        Path(__file__).resolve().parents[2] / "fixtures/adapters" / fixture
    ).read_text(encoding="utf-8")
    version = f"fake-{tool_id} 1.0"
    version_args = PROBE_VERSION_ARGUMENTS[tool_id]
    executable = tmp_path / f"fake_{tool_id}"
    body = f"""#!/usr/bin/python3
import sys
if {version_args!r}[0] in sys.argv:
    print({version!r})
else:
    print({report!r}, end='')
"""
    executable.write_text(body, encoding="utf-8")
    executable.chmod(0o755)
    fingerprint = ToolFingerprint(
        tool_id=tool_id,
        executable=str(executable.resolve()),
        version=version,
        version_args=version_args,
        executable_sha256=_hash_bytes(executable.read_bytes()),
        build_hash=_hash_bytes(executable.read_bytes() + f"{version}\n".encode()),
        adapter_version=f"{tool_id}-adapter-v1",
        container_digest=None,
    )
    adapter = adapter_type(
        tool_fingerprint=fingerprint,
        artifact_store=store,
        workspace_root=tmp_path / "workspaces",
        invocation=AdapterInvocation(
            argv_tail=("--run",),
            expected_output_paths=(),
            environment={},
        ),
    )

    result = execute_tool_job(
        adapter,
        _job(
            store,
            stage=stage,
            analysis_view_id=analysis_view_id,
        ),
        store,
    )

    assert result.status == "PASS"
    assert result.__class__.__name__ == "StageResult"
    assert all(store.open_verified(item).read() is not None for item in result.raw_artifacts)
    assert adapter.last_stage_result_artifact is not None
    assert store.open_verified(adapter.last_stage_result_artifact).read()
