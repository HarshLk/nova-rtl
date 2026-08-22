from __future__ import annotations

import ast
import re
from pathlib import Path

from nova_rtl.contracts.schema_export import SCHEMA_REGISTRY

FORBIDDEN_NEW_CODE_ALIASES = {
    "PlanningRequest",
    "RecoveryAdviser",
    "Ledger",
    "ToolRequest",
    "ExecutionArtifacts",
    "build_command",
}


def declared_identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    identifiers.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return identifiers


def test_forbidden_compatibility_aliases_are_confined_to_migrations() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    allowed_paths = {
        repository_root / "src" / "nova_rtl" / "contracts" / "migrations.py",
        Path(__file__).resolve(),
    }
    scanned_roots = (
        repository_root / "src",
        repository_root / "tests",
        repository_root / "config",
        repository_root / ".github",
    )
    text_suffixes = {".json", ".j2", ".jinja", ".py", ".toml", ".txt", ".yaml", ".yml"}
    violations: dict[str, list[str]] = {}
    for root in scanned_roots:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path in allowed_paths or path.suffix not in text_suffixes:
                continue
            source = path.read_text(encoding="utf-8")
            lexical_aliases = {
                alias
                for alias in FORBIDDEN_NEW_CODE_ALIASES
                if re.search(rf"\b{re.escape(alias)}\b", source)
            }
            identifiers = declared_identifiers(path) if path.suffix == ".py" else set()
            forbidden = sorted(
                (identifiers & FORBIDDEN_NEW_CODE_ALIASES) | lexical_aliases
            )
            if forbidden:
                violations[str(path.relative_to(repository_root))] = forbidden

    assert violations == {}


def test_canonical_registry_has_one_key_per_model() -> None:
    model_names = [registration.model.__name__ for registration in SCHEMA_REGISTRY.values()]
    assert len(model_names) == len(set(model_names))
