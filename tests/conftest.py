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
    parser.addoption(
        "--m2-packet",
        action="store",
        default=None,
        help="Use this verified M2 report as the M3 dependency.",
    )
    parser.addoption(
        "--candidate-bundle",
        action="store",
        default=None,
        help="Use this verified candidate bundle for M4 sign-off.",
    )
    parser.addoption(
        "--m3-packet",
        action="store",
        default=None,
        help="Use this verified M3 report as the M4 dependency.",
    )
    parser.addoption(
        "--search-bundle",
        action="store",
        default=None,
        help="Use this verified deterministic search bundle for M5 sign-off.",
    )
    parser.addoption(
        "--m4-packet",
        action="store",
        default=None,
        help="Use this verified M4 report as the M5 dependency.",
    )
    parser.addoption(
        "--m5-packet",
        action="store",
        default=None,
        help="Use this verified M5 report as the M6 dependency.",
    )
    parser.addoption(
        "--planner-run",
        action="store",
        default=None,
        help="Use this completed M6 planner run for sign-off verification.",
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


@pytest.fixture
def m2_packet_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--m2-packet")
    if configured is None:
        pytest.skip("M3 sign-off validation requires --m2-packet")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"M2 packet does not exist: {path}")
    return path


@pytest.fixture
def candidate_bundle_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--candidate-bundle")
    if configured is None:
        pytest.skip("M4 sign-off validation requires --candidate-bundle")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"candidate bundle does not exist: {path}")
    return path


@pytest.fixture
def m3_packet_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--m3-packet")
    if configured is None:
        pytest.skip("M4 sign-off validation requires --m3-packet")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"M3 packet does not exist: {path}")
    return path


@pytest.fixture
def search_bundle_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--search-bundle")
    if configured is None:
        pytest.skip("M5 sign-off validation requires --search-bundle")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"search bundle does not exist: {path}")
    return path


@pytest.fixture
def m4_packet_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--m4-packet")
    if configured is None:
        pytest.skip("M5 sign-off validation requires --m4-packet")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"M4 packet does not exist: {path}")
    return path


@pytest.fixture
def m5_packet_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--m5-packet")
    if configured is None:
        pytest.skip("M6 sign-off validation requires --m5-packet")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"M5 packet does not exist: {path}")
    return path


@pytest.fixture
def planner_run_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--planner-run")
    if configured is None:
        pytest.skip("M6 sign-off validation requires --planner-run")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"M6 planner run does not exist: {path}")
    return path
