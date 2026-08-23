"""Deterministic five-master formal event harness construction."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, model_validator

from nova_rtl.constraints.binding import SafetyPreflightError
from nova_rtl.contracts.analysis import ClockInventory
from nova_rtl.contracts.base import (
    EntityId,
    HashRef,
    NonEmptyString,
    StrictContract,
    canonical_sha256,
)


def _hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


class SharedMasterEvent(StrictContract):
    clock_id: EntityId
    event_index: int = Field(strict=True, ge=0)
    gold_event_signal: EntityId
    gate_event_signal: EntityId


class MulticlockHarnessContract(StrictContract):
    """Exact formal clock/reset/environment model used before proof."""

    schema_version: Literal[1] = 1
    master_clock_ids: tuple[EntityId, ...] = Field(min_length=5, max_length=5)
    shared_master_events: tuple[SharedMasterEvent, ...] = Field(min_length=5, max_length=5)
    generated_clock_graph_hash: HashRef
    generated_clock_model: Literal["DERIVED_FROM_PROTECTED_DIVIDER_STATE"]
    multiclock_enabled: Literal[True]
    reset_assumption_hash: HashRef
    environment_assumption_hash: HashRef
    systemverilog_source_hash: HashRef
    sby_options_hash: HashRef
    harness_hash: HashRef

    @model_validator(mode="after")
    def identity_is_complete(self) -> Self:
        if len(self.master_clock_ids) != len(set(self.master_clock_ids)):
            raise ValueError("formal master clock identities must be unique")
        event_ids = tuple(item.clock_id for item in self.shared_master_events)
        if event_ids != self.master_clock_ids:
            raise ValueError("shared formal events must exactly cover the master clocks")
        if tuple(item.event_index for item in self.shared_master_events) != tuple(range(5)):
            raise ValueError("shared formal event indexes must be canonical")
        expected_hash = canonical_sha256(self, exclude=frozenset({"harness_hash"}))
        if self.harness_hash != expected_hash:
            raise ValueError("harness_hash does not match the formal harness identity")
        return self


@dataclass(frozen=True)
class MulticlockHarnessAssets:
    contract: MulticlockHarnessContract
    systemverilog_source: bytes
    sby_options: bytes


def _normalized_assumptions(
    values: tuple[str, ...],
    *,
    kind: str,
) -> tuple[NonEmptyString, ...]:
    if not values or any(not item.strip() for item in values):
        raise SafetyPreflightError(
            "FORMAL_ASSUMPTION_MODEL_INVALID",
            f"{kind} assumptions must be nonempty",
        )
    normalized = tuple(sorted(item.strip() for item in values))
    if len(normalized) != len(set(normalized)):
        raise SafetyPreflightError(
            "FORMAL_ASSUMPTION_MODEL_INVALID",
            f"{kind} assumptions must be unique",
        )
    return normalized


def render_multiclock_harness(
    clock_inventory: ClockInventory,
    *,
    reset_assumptions: tuple[str, ...],
    fairness_assumptions: tuple[str, ...],
    environment_assumptions: tuple[str, ...],
) -> MulticlockHarnessAssets:
    """Render shared independent events while keeping generated clocks DUT-derived."""

    reset = _normalized_assumptions(reset_assumptions, kind="reset")
    fairness = _normalized_assumptions(fairness_assumptions, kind="fairness")
    environment = _normalized_assumptions(environment_assumptions, kind="environment")
    forbidden_fragments = ("gold_output == gate_output", "gate_output == gold_output")
    if any(
        fragment in assumption
        for assumption in (*reset, *fairness, *environment)
        for fragment in forbidden_fragments
    ):
        raise SafetyPreflightError(
            "FORMAL_ASSUMPTION_MASKS_DIVERGENCE",
            "formal assumptions may not directly equate gold and gate outputs",
        )

    master_ids = tuple(sorted(item.clock_id for item in clock_inventory.master_clocks))
    events = tuple(
        SharedMasterEvent(
            clock_id=clock_id,
            event_index=index,
            gold_event_signal=f"gold_{clock_id}",
            gate_event_signal=f"gate_{clock_id}",
        )
        for index, clock_id in enumerate(master_ids)
    )
    source_lines = (
        "module nova_multiclock_event_harness;",
        "  (* anyseq *) logic [4:0] independent_master_events;",
        *(
            line
            for item in events
            for line in (
                f"  wire {item.gold_event_signal} = "
                f"independent_master_events[{item.event_index}];",
                f"  wire {item.gate_event_signal} = "
                f"independent_master_events[{item.event_index}];",
            )
        ),
        "  // Generated clocks remain derived from protected DUT divider state.",
        "endmodule",
        "",
    )
    systemverilog = "\n".join(source_lines).encode("utf-8")
    sby_options = b"[options]\nmulticlock on\n"
    payload = {
        "schema_version": 1,
        "master_clock_ids": master_ids,
        "shared_master_events": tuple(item.model_dump(mode="json") for item in events),
        "generated_clock_graph_hash": clock_inventory.clock_graph_hash,
        "generated_clock_model": "DERIVED_FROM_PROTECTED_DIVIDER_STATE",
        "multiclock_enabled": True,
        "reset_assumption_hash": canonical_sha256({"reset_assumptions": reset}),
        "environment_assumption_hash": canonical_sha256(
            {
                "environment_assumptions": environment,
                "fairness_assumptions": fairness,
            }
        ),
        "systemverilog_source_hash": _hash_bytes(systemverilog),
        "sby_options_hash": _hash_bytes(sby_options),
    }
    contract = MulticlockHarnessContract(
        **payload,
        harness_hash=canonical_sha256(payload),
    )
    return MulticlockHarnessAssets(
        contract=contract,
        systemverilog_source=systemverilog,
        sby_options=sby_options,
    )


__all__ = [
    "MulticlockHarnessAssets",
    "MulticlockHarnessContract",
    "SharedMasterEvent",
    "render_multiclock_harness",
]
