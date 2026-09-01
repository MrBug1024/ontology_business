from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.external_api_schemas import (
    ExternalApiKeyCreateIn,
    ExternalCapabilityKind,
    ExternalCapabilityInvocationIn,
    ExternalManagedInputIn,
)
from app.services import external_api_service


def test_capability_scopes_are_explicit_and_supported() -> None:
    payload = ExternalApiKeyCreateIn(
        name="Capability client",
        scopes=["capabilities:read", "capabilities:invoke"],
    )

    assert payload.scopes == ["capabilities:read", "capabilities:invoke"]
    assert set(payload.scopes).issubset(external_api_service.SUPPORTED_SCOPES)


def test_external_capability_kinds_match_the_discoverable_runtime() -> None:
    adapter = TypeAdapter(ExternalCapabilityKind)

    assert [adapter.validate_python(kind) for kind in ("function", "action", "rule", "workflow")] == [
        "function",
        "action",
        "rule",
        "workflow",
    ]
    for unsupported in ("query", "provider"):
        with pytest.raises(ValidationError):
            adapter.validate_python(unsupported)


@pytest.mark.parametrize(
    "forbidden",
    [
        {"data_source_id": "source-1"},
        {"table_name": "records"},
        {"sql": "select 1"},
        {"password": "unsafe"},
        {"connection_string": "unsafe"},
    ],
)
def test_managed_input_rejects_physical_or_secret_overrides(forbidden: dict) -> None:
    with pytest.raises(ValidationError):
        ExternalManagedInputIn(
            port_key="records.input",
            dataset_version_id="version-1",
            **forbidden,
        )


def test_managed_input_requires_exactly_one_governed_reference() -> None:
    with pytest.raises(ValidationError, match="必须且只能"):
        ExternalManagedInputIn(port_key="records.input")

    with pytest.raises(ValidationError, match="必须且只能"):
        ExternalManagedInputIn(
            port_key="records.input",
            dataset_version_id="version-1",
            asset_version_id="asset-version-1",
        )

    value = ExternalManagedInputIn(
        port_key="records.input",
        binding_key="runtime.records",
    )
    assert value.runtime_document() == {
        "port_key": "records.input",
        "binding_key": "runtime.records",
    }


def test_invocation_confirmation_shape_is_mode_bound_and_strict() -> None:
    with pytest.raises(ValidationError, match="必须提供"):
        ExternalCapabilityInvocationIn(mode="confirm")

    with pytest.raises(ValidationError, match="只有 confirm"):
        ExternalCapabilityInvocationIn(
            mode="execute",
            confirmation={
                "preview_invocation_id": "preview-1",
                "confirmation_token": "opaque-token",
            },
        )

    with pytest.raises(ValidationError):
        ExternalCapabilityInvocationIn(
            mode="confirm",
            confirmation={
                "preview_invocation_id": "preview-1",
                "confirmation_token": "opaque-token",
                "data_source_id": "source-1",
            },
        )


def test_invocation_rejects_duplicate_port_keys_case_insensitively() -> None:
    with pytest.raises(ValidationError, match="不能重复"):
        ExternalCapabilityInvocationIn(
            managed_inputs=[
                {
                    "port_key": "records.input",
                    "dataset_version_id": "version-1",
                },
                {
                    "port_key": "Records.Input",
                    "dataset_head_id": "head-1",
                },
            ]
        )
