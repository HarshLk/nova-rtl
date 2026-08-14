"""Platform-readiness contracts used by the bootstrap doctor."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

HashRef = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictContract(BaseModel):
    """Immutable contract that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolFingerprint(StrictContract):
    """Identity of the executable inspected by the doctor."""

    schema_version: Literal[1] = 1
    tool_id: str = Field(min_length=1)
    executable: str = Field(min_length=1)
    version: str = Field(min_length=1)
    build_hash: HashRef
    adapter_version: str = Field(min_length=1)
    container_digest: HashRef | None = None


class DoctorCheck(StrictContract):
    """One executable or platform-file readiness check."""

    name: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    resolved_path: str | None
    tool_fingerprint: ToolFingerprint | None
    artifact_hash: HashRef | None
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def passing_check_has_one_identity(self) -> Self:
        identities = sum(
            identity is not None for identity in (self.tool_fingerprint, self.artifact_hash)
        )
        if self.status == "PASS" and identities != 1:
            raise ValueError("a passing doctor check requires exactly one identity")
        return self


class DoctorReport(StrictContract):
    """Complete tool and optional platform-lock readiness report."""

    schema_version: Literal[1] = 1
    status: Literal["PASS", "FAIL"]
    checks: tuple[DoctorCheck, ...]
    platform_lock_hash: HashRef | None
    generated_at: datetime
    exit_code: Literal[0, 2]

    @model_validator(mode="after")
    def status_matches_checks_and_exit_code(self) -> Self:
        expected_status = "PASS" if all(check.status == "PASS" for check in self.checks) else "FAIL"
        expected_exit_code = 0 if expected_status == "PASS" else 2
        if self.status != expected_status or self.exit_code != expected_exit_code:
            raise ValueError("doctor status and exit code must match all checks")
        return self
