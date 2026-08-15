from __future__ import annotations

import pytest
from pydantic import ValidationError

from nova_rtl.contracts.base import StrictContract, canonical_json_bytes


class ExampleContract(StrictContract):
    name: str
    metadata: dict[str, int]


def test_strict_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExampleContract(name="example", metadata={}, unexpected=True)


def test_strict_contract_is_immutable() -> None:
    value = ExampleContract(name="example", metadata={})

    with pytest.raises(ValidationError, match="frozen_instance"):
        value.name = "changed"


def test_canonical_json_bytes_sorts_nested_mapping_keys() -> None:
    first = ExampleContract(name="example", metadata={"z": 1, "a": 2})
    second = ExampleContract(name="example", metadata={"a": 2, "z": 1})

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_bytes(first) == b'{"metadata":{"a":2,"z":1},"name":"example"}'


def test_canonical_m0_contracts_are_exported_from_contract_package() -> None:
    from nova_rtl import contracts

    expected_names = {
        "DoctorCheck",
        "DoctorIssue",
        "DoctorReport",
        "HostPlatform",
        "PlatformArtifact",
        "PlatformLock",
        "TimingCorner",
        "ToolExecutableSource",
        "ToolFingerprint",
        "ToolSource",
        "ToolchainSourceManifest",
    }

    assert expected_names <= set(contracts.__all__)
    assert all(hasattr(contracts, name) for name in expected_names)
