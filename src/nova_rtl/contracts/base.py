"""Shared strict primitives for persisted NOVA-RTL contracts."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

HashRef = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$")]


class StrictContract(BaseModel):
    """Immutable contract that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_json_bytes(value: StrictContract) -> bytes:
    """Serialize a contract into stable UTF-8 JSON bytes."""

    payload = value.model_dump(mode="json")
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = ["AwareDatetime", "EntityId", "HashRef", "StrictContract", "canonical_json_bytes"]
