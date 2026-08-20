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


@pytest.fixture
def platform_lock_path(request: pytest.FixtureRequest) -> Path:
    configured = request.config.getoption("--platform-lock")
    if configured is None:
        pytest.skip("real platform smoke requires --platform-lock")
    path = Path(configured).resolve()
    if not path.is_file():
        pytest.fail(f"platform lock does not exist: {path}")
    return path
