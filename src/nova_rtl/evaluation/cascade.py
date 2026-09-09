"""Deterministic ordered candidate evaluation through mandatory gates 0--7."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, Self

from pydantic import field_validator, model_validator

from nova_rtl.contracts.base import EntityId, HashRef, StrictContract, canonical_sha256
from nova_rtl.contracts.execution import StageStatus
from nova_rtl.contracts.optimization import CandidateRecord

GateId = Literal["0", "0.5", "1", "2", "3", "4", "5", "6", "7"]
EVALUATION_GATE_ORDER: tuple[GateId, ...] = (
    "0",
    "0.5",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
)
GATE_NAMES: dict[GateId, str] = {
    "0": "PROPOSAL_POLICY",
    "0.5": "TRANSFORM_FEASIBILITY",
    "1": "SOURCE_PARSE",
    "2": "STRUCTURAL_INVARIANTS",
    "3": "FAST_SYNTHESIS",
    "4": "STRICT_FORMAL",
    "5": "FULL_STA",
    "6": "OPENROAD_PHYSICAL",
    "7": "FINAL_REPROOF",
}


class EvaluationCascadeError(ValueError):
    """Gate configuration or runner output is incomplete or inconsistent."""


class GateAssessment(StrictContract):
    """Terminal status and evidence references returned by one gate runner."""

    gate_id: GateId
    status: StageStatus
    stage_result_ids: tuple[EntityId, ...]
    diagnostic_codes: tuple[str, ...]

    @field_validator("stage_result_ids", "diagnostic_codes")
    @classmethod
    def sequences_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("gate assessment sequences must be unique")
        return value

    @model_validator(mode="after")
    def nonpass_has_diagnostic(self) -> Self:
        if self.status != "PASS" and not self.diagnostic_codes:
            raise ValueError("non-pass gate assessment requires a diagnostic code")
        return self


class GateEvent(StrictContract):
    """Replayable start or terminal event for one attempted evaluation gate."""

    schema_version: Literal[1] = 1
    candidate_id: EntityId
    sequence: int
    gate_id: GateId
    gate_name: str
    event_type: Literal[
        "GATE_STARTED",
        "GATE_COMPLETED",
        "GATE_FAILED",
        "GATE_INCONCLUSIVE",
        "GATE_INFRASTRUCTURE_ERROR",
    ]
    status: StageStatus | None
    stage_result_ids: tuple[EntityId, ...]
    diagnostic_codes: tuple[str, ...]
    event_hash: HashRef

    @model_validator(mode="after")
    def event_is_coherent_and_self_hashed(self) -> Self:
        if self.sequence < 0:
            raise ValueError("gate event sequence cannot be negative")
        if self.gate_name != GATE_NAMES[self.gate_id]:
            raise ValueError("gate event name does not match gate ID")
        if self.event_type == "GATE_STARTED":
            if self.status is not None or self.stage_result_ids or self.diagnostic_codes:
                raise ValueError("started gate event cannot contain terminal evidence")
        elif self.status is None:
            raise ValueError("terminal gate event requires a status")
        if self.event_hash != canonical_sha256(self, exclude=frozenset({"event_hash"})):
            raise ValueError("gate event hash is not canonical")
        return self


class EvaluationResult(StrictContract):
    """Ordered attempted gates and the first terminal hard outcome."""

    candidate_id: EntityId
    status: StageStatus
    terminal_gate_id: GateId
    assessments: tuple[GateAssessment, ...]
    event_hashes: tuple[HashRef, ...]
    result_hash: HashRef

    @model_validator(mode="after")
    def result_is_ordered_and_self_hashed(self) -> Self:
        attempted = tuple(item.gate_id for item in self.assessments)
        if attempted != EVALUATION_GATE_ORDER[: len(attempted)]:
            raise ValueError("evaluation assessments are not a gate-order prefix")
        if not attempted or attempted[-1] != self.terminal_gate_id:
            raise ValueError("terminal gate must be the last attempted gate")
        if self.status != self.assessments[-1].status:
            raise ValueError("evaluation status must match terminal gate status")
        if self.status == "PASS" and attempted != EVALUATION_GATE_ORDER:
            raise ValueError("passing evaluation requires every mandatory gate")
        if self.result_hash != canonical_sha256(self, exclude=frozenset({"result_hash"})):
            raise ValueError("evaluation result hash is not canonical")
        return self


GateRunner = Callable[[CandidateRecord], GateAssessment]
GateEventSink = Callable[[GateEvent], None]


def _event(
    candidate_id: str,
    sequence: int,
    gate_id: GateId,
    assessment: GateAssessment | None,
) -> GateEvent:
    if assessment is None:
        event_type = "GATE_STARTED"
        status = None
        stage_ids: tuple[str, ...] = ()
        diagnostics: tuple[str, ...] = ()
    else:
        event_type = {
            "PASS": "GATE_COMPLETED",
            "FAIL": "GATE_FAILED",
            "INCONCLUSIVE": "GATE_INCONCLUSIVE",
            "INFRASTRUCTURE_ERROR": "GATE_INFRASTRUCTURE_ERROR",
        }[assessment.status]
        status = assessment.status
        stage_ids = assessment.stage_result_ids
        diagnostics = assessment.diagnostic_codes
    payload = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "sequence": sequence,
        "gate_id": gate_id,
        "gate_name": GATE_NAMES[gate_id],
        "event_type": event_type,
        "status": status,
        "stage_result_ids": stage_ids,
        "diagnostic_codes": diagnostics,
    }
    return GateEvent(**payload, event_hash=canonical_sha256(payload))


class EvaluationCascade:
    """Execute all mandatory gates in order, stopping at the first hard non-pass."""

    def __init__(
        self,
        gate_runners: Mapping[str, GateRunner],
        *,
        event_sink: GateEventSink | None = None,
    ) -> None:
        if set(gate_runners) != set(EVALUATION_GATE_ORDER):
            raise EvaluationCascadeError("cascade requires exactly mandatory gates 0 through 7")
        self._runners = {gate: gate_runners[gate] for gate in EVALUATION_GATE_ORDER}
        self._event_sink = event_sink

    def evaluate(self, candidate: CandidateRecord) -> EvaluationResult:
        assessments: list[GateAssessment] = []
        events: list[GateEvent] = []
        for gate_id in EVALUATION_GATE_ORDER:
            started = _event(candidate.candidate_id, len(events), gate_id, None)
            events.append(started)
            if self._event_sink is not None:
                self._event_sink(started)
            try:
                assessment = self._runners[gate_id](candidate)
                if assessment.gate_id != gate_id:
                    raise EvaluationCascadeError("gate runner returned the wrong gate identity")
            except Exception as error:
                assessment = GateAssessment(
                    gate_id=gate_id,
                    status="INFRASTRUCTURE_ERROR",
                    stage_result_ids=(),
                    diagnostic_codes=(f"GATE_RUNNER_{type(error).__name__.upper()}",),
                )
            assessments.append(assessment)
            terminal = _event(candidate.candidate_id, len(events), gate_id, assessment)
            events.append(terminal)
            if self._event_sink is not None:
                self._event_sink(terminal)
            if assessment.status != "PASS":
                break

        status = assessments[-1].status
        payload = {
            "candidate_id": candidate.candidate_id,
            "status": status,
            "terminal_gate_id": assessments[-1].gate_id,
            "assessments": tuple(item.model_dump(mode="json") for item in assessments),
            "event_hashes": tuple(item.event_hash for item in events),
        }
        return EvaluationResult(
            candidate_id=candidate.candidate_id,
            status=status,
            terminal_gate_id=assessments[-1].gate_id,
            assessments=tuple(assessments),
            event_hashes=tuple(item.event_hash for item in events),
            result_hash=canonical_sha256(payload),
        )


__all__ = [
    "EVALUATION_GATE_ORDER",
    "EvaluationCascade",
    "EvaluationCascadeError",
    "EvaluationResult",
    "GATE_NAMES",
    "GateAssessment",
    "GateEvent",
]
