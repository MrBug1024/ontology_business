from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from app.providers.semantic_dataset_query import (
    SemanticDatasetQueryProvider,
    SemanticDatasetQueryProviderError,
    _require_query_property_access,
)
from app.services import input_contract_validator, semantic_mapping_contract_service


def _mapping_contract() -> dict[str, Any]:
    return {
        "contract_version": semantic_mapping_contract_service.CONTRACT_VERSION,
        "id": "mapping-1",
        "entity_id": "entity-1",
        "mapping_key": "records.entity",
        "schema_document": {
            input_contract_validator.CONTENT_CONTRACT_KEY: {
                "version": input_contract_validator.CONTENT_CONTRACT_VERSION,
                "relations": [
                    {
                        "fields": [
                            {
                                "name": "record_id",
                                "aliases": [],
                                "logical_types": ["string"],
                                "required": True,
                            },
                            {
                                "name": "private_value",
                                "aliases": [],
                                "logical_types": ["string"],
                                "required": True,
                            },
                        ],
                        "minimum_data_rows": 1,
                        "allow_additional_fields": True,
                    }
                ],
                "allow_additional_relations": True,
            }
        },
        "fields": [
            {
                "ontology_property_id": "property-id",
                "contract_field_index": 0,
                "direction": "input",
                "is_required": True,
                "transform": {},
            },
            {
                "ontology_property_id": "property-private",
                "contract_field_index": 1,
                "direction": "input",
                "is_required": True,
                "transform": {},
            },
        ],
        "identifier_strategy": {},
        "filter_expression": {},
    }


def test_semantic_mapping_contract_recursively_rejects_unknown_schema_fields() -> None:
    valid = _mapping_contract()
    normalized = semantic_mapping_contract_service.normalize_contract(valid)
    assert normalized == valid

    content_extra = copy.deepcopy(valid)
    content_extra["schema_document"][
        input_contract_validator.CONTENT_CONTRACT_KEY
    ]["dataset"] = "runtime-dataset"
    relation_extra = copy.deepcopy(valid)
    relation_extra["schema_document"][
        input_contract_validator.CONTENT_CONTRACT_KEY
    ]["relations"][0]["table"] = "physical_table"
    field_extra = copy.deepcopy(valid)
    field_extra["schema_document"][
        input_contract_validator.CONTENT_CONTRACT_KEY
    ]["relations"][0]["fields"][0]["binding"] = "runtime-binding"

    for invalid in (content_extra, relation_extra, field_extra):
        with pytest.raises(
            semantic_mapping_contract_service.SemanticMappingContractError,
            match="unsupported fields",
        ):
            semantic_mapping_contract_service.normalize_contract(invalid)


@pytest.mark.parametrize(
    "field_name",
    ("transform", "identifier_strategy", "filter_expression"),
)
def test_unversioned_authoring_documents_cannot_enter_portable_contract(
    field_name: str,
) -> None:
    invalid = _mapping_contract()
    if field_name == "transform":
        invalid["fields"][0][field_name] = {
            "portable": {"relation_key": "authored-sheet"}
        }
    else:
        invalid[field_name] = {"portable": {"relation_key": "authored-sheet"}}

    with pytest.raises(
        semantic_mapping_contract_service.SemanticMappingContractError,
        match="no supported portable fields",
    ):
        semantic_mapping_contract_service.normalize_contract(invalid)


def test_query_property_acl_rejects_static_and_resolved_hidden_references() -> None:
    catalog = [
        {
            "entity_id": "entity-1",
            "entity_name": "Record",
            "properties": [{"property_name": "Record ID"}],
        }
    ]
    visible = {"base_entity": "Record", "base_properties": ["Record ID"]}
    hidden = {"base_entity": "Record", "base_properties": ["Private value"]}
    dynamic = {
        "base_entity": "Record",
        "base_properties": [{"$input": "property_name"}],
    }

    _require_query_property_access(
        visible,
        catalog,
        allow_input_references=True,
    )
    _require_query_property_access(
        dynamic,
        catalog,
        allow_input_references=True,
    )
    with pytest.raises(SemanticDatasetQueryProviderError, match="unavailable"):
        _require_query_property_access(
            hidden,
            catalog,
            allow_input_references=True,
        )
    with pytest.raises(SemanticDatasetQueryProviderError, match="unavailable"):
        _require_query_property_access(
            hidden,
            catalog,
            allow_input_references=False,
        )


@dataclass(frozen=True)
class _Definition:
    entities: dict[str, Any]
    semantic_mapping_contracts: dict[str, Any]
    semantic_relation_mapping_contracts: dict[str, Any] = field(default_factory=dict)
    mappings: dict[str, Any] = field(default_factory=dict)
    relation_mappings: dict[str, Any] = field(default_factory=dict)


def test_provider_runtime_mapping_contains_only_currently_readable_properties() -> None:
    public_property = SimpleNamespace(id="property-id", name="Record ID", is_key=True)
    private_property = SimpleNamespace(
        id="property-private",
        name="Private value",
        is_key=False,
    )
    entity = SimpleNamespace(
        id="entity-1",
        name="Record",
        properties=[public_property, private_property],
    )
    definition = _Definition(
        entities={entity.id: entity},
        semantic_mapping_contracts={"mapping-1": _mapping_contract()},
    )
    runtime_relation = SimpleNamespace(
        id="relation-runtime",
        ordinal=0,
        relation_key="runtime_records",
        fields=[
            SimpleNamespace(
                id="field-id",
                ordinal=0,
                source_name="record_id",
                field_key="record_id",
                logical_type="string",
            ),
            SimpleNamespace(
                id="field-private",
                ordinal=1,
                source_name="private_value",
                field_key="private_value",
                logical_type="string",
            ),
        ],
    )
    provider = SemanticDatasetQueryProvider(_db=object())  # type: ignore[arg-type]

    with patch(
        "app.providers.semantic_dataset_query.permission_service.can_read_property",
        side_effect=lambda _db, prop: prop.id == public_property.id,
    ):
        _definition, mappings = provider._semantic_mappings(
            definition=definition,
            deployment=SimpleNamespace(scenario_id="scenario-1"),
            mapping_ids=("mapping-1",),
            runtime_version=SimpleNamespace(record_count=1, manifest={}),
            runtime_schema=SimpleNamespace(relations=[runtime_relation]),
            source=SimpleNamespace(id="source-1"),
            query_args={"base_entity": "Record", "base_properties": ["Record ID"]},
        )

    assert mappings[0].column_map == {"Record ID": "record_id"}
    assert "Private value" not in mappings[0].column_map
