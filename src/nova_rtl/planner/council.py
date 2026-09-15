"""Bounded, role-scoped M8 council planning primitives."""

from __future__ import annotations

from pathlib import Path

import yaml

from nova_rtl.contracts.planning import CouncilPolicy


class CouncilPolicyError(RuntimeError):
    """The council policy cannot be loaded without expanding authority."""


def load_council_policy(path: Path) -> CouncilPolicy:
    """Load and canonically bind one strict council policy."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CouncilPolicyError(f"cannot load council policy: {error}") from error
    if not isinstance(payload, dict):
        raise CouncilPolicyError("council policy must be a YAML mapping")
    try:
        return CouncilPolicy.build(**payload)
    except (TypeError, ValueError) as error:
        raise CouncilPolicyError(f"invalid council policy: {error}") from error


__all__ = ["CouncilPolicyError", "load_council_policy"]
