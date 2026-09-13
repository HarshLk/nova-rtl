from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nova_rtl.optimization.planner_flow import M6PlannerFlowError, verify_planner_run


def test_planner_package_does_not_import_execution_or_process_authority() -> None:
    root = Path("src/nova_rtl/planner")
    forbidden = {
        "os",
        "subprocess",
        "nova_rtl.adapters",
        "nova_rtl.optimization.flow",
        "nova_rtl.optimization.search_flow",
        "nova_rtl.transforms.executor",
    }

    violations = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == item or name.startswith(f"{item}.") for item in forbidden):
                    violations.append(f"{path}:{node.lineno}:{name}")

    assert violations == []


def test_verifier_rejects_tampered_document_before_dependency_lookup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "planner-run.json"
    path.write_text('{"schema_version":1,"status":"PASS"}', encoding="utf-8")

    with pytest.raises(M6PlannerFlowError, match="planner run is invalid"):
        verify_planner_run(path, repository_root=Path("."))
