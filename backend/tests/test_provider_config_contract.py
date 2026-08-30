from __future__ import annotations

import pytest

from app.services import function_definition_service


def _definition(provider_config: dict) -> dict:
    return {
        "name": "Versioned provider capability",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "output_schema": {"type": "object"},
        "runtime_kind": "provider",
        "runtime_config": {
            "provider_key": "trusted.example",
            "provider_version": "1.0.0",
            "provider_config": provider_config,
        },
    }


def test_provider_config_accepts_only_publishable_semantic_values() -> None:
    normalized = function_definition_service.normalize_definition(
        _definition(
            {
                "logical_contract": "example/v1",
                "mapping_ids": ["mapping-a", "mapping-b"],
                "options": {"strict": True, "threshold": 2},
            }
        )
    )

    assert normalized["runtime_config"]["provider_config"]["logical_contract"] == (
        "example/v1"
    )


@pytest.mark.parametrize(
    "provider_config",
    [
        {"value": "postgresql://user:password@example.test/database"},
        {"value": "SELECT * FROM physical_records"},
        {"value": "host=database.internal;port=5432"},
        {"value": "C:\\private\\dataset.parquet"},
        {"value": "/srv/private/dataset.parquet"},
        {"nested": {"password": "hidden"}},
    ],
)
def test_provider_config_rejects_physical_or_secret_values(
    provider_config: dict,
) -> None:
    with pytest.raises(function_definition_service.FunctionDefinitionError):
        function_definition_service.normalize_definition(_definition(provider_config))
