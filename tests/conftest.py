from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--platform-lock",
        action="store",
        default=None,
        help="Run real-platform integration tests against this platform lock.",
    )
    parser.addoption(
        "--run",
        action="store",
        default=None,
        help="Inspect an already completed NOVA run directory.",
    )
    parser.addoption(
        "--calibration-directory",
        action="store",
        default=None,
        help="Use this completed full-benchmark calibration for M2 sign-off.",
    )
    parser.addoption(
        "--m1-packet",
        action="store",
        default=None,
        help="Use this verified M1 packet for M2 sign-off.",
    )


@pytest.fixture
def platform_lock_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--platform-lock")
    if configured is None:
        pytest.skip("real platform smoke requires --platform-lock")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"platform lock does not exist: {path}")
    return path


@pytest.fixture
def completed_run_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--run")
    if configured is None:
        pytest.skip("completed baseline validation requires --run")
    path = Path(configured).resolve()
    if not path.is_dir():
        pytest.fail(f"completed run directory does not exist: {path}")
    return path


@pytest.fixture
def calibration_directory_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--calibration-directory")
    if configured is None:
        pytest.skip("M2 sign-off validation requires --calibration-directory")
    path = Path(configured).resolve()
    if not path.is_dir():
        pytest.fail(f"calibration directory does not exist: {path}")
    return path


@pytest.fixture
def m1_packet_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--m1-packet")
    if configured is None:
        pytest.skip("M2 sign-off validation requires --m1-packet")
    path = Path(configured).resolve()
    if not path.is_dir():
        pytest.fail(f"M1 packet does not exist: {path}")
    return path
