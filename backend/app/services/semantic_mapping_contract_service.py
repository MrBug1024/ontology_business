"""Portable semantic mapping contracts derived from modeling schemas.

The persisted Catalog rows are authoring evidence. Runtime definitions retain
only ontology identities and a content-addressable structural contract; they do
not retain Dataset, DatasetVersion, relation-name, sheet-name, or object-store
references.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    DatasetRelation,
    LogicalDataset,
    SemanticFieldMapping,
    SemanticMapping,
    SemanticRelationMapping,
)
from . import input_contract_validator


CONTRACT_VERSION = 1
_MAX_FIELDS = 512
_MAX_ALIASES = 64
_CONTENT_KEYS = {"version", "relations", "allow_additional_relations"}
_RELATION_KEYS = {"fields", "minimum_data_rows", "allow_additional_fields"}
_FIELD_KEYS = {"name", "aliases", "logical_types", "required"}


class SemanticMappingContractError(ValueError):
    """A semantic mapping cannot be represented as a portable definition."""


def _required_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise SemanticMappingContractError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise SemanticMappingContractError(f"{label} is invalid")
    return normalized


def _closed_keys(value: Mapping[Any, Any], allowed: set[str], label: str) -> None:
    if any(not isinstance(key, str) for key in value) or set(value) - allowed:
        raise SemanticMappingContractError(f"{label} contains unsupported fields")


def _canonical_content_schema(value: Any, *, label: str) -> dict[str, Any]:
    """Return the only portable schema grammar admitted to a Definition."""

    contract_key = input_contract_validator.CONTENT_CONTRACT_KEY
    if not isinstance(value, Mapping) or set(value) != {contract_key}:
        raise SemanticMappingContractError(
            f"{label} must contain only one tabular content contract"
        )
    content = value[contract_key]
    if not isinstance(content, Mapping):
        raise SemanticMappingContractError(f"{label} content contract must be an object")
    _closed_keys(content, _CONTENT_KEYS, f"{label} content contract")
    if content.get("version") != input_contract_validator.CONTENT_CONTRACT_VERSION:
        raise SemanticMappingContractError(f"{label} content contract version is unsupported")
    raw_relations = content.get("relations")
    if (
        not isinstance(raw_relations, Sequence)
        or isinstance(raw_relations, (str, bytes, bytearray))
        or len(raw_relations) != 1
    ):
        raise SemanticMappingContractError(f"{label} must describe exactly one logical relation")

    relations: list[dict[str, Any]] = []
    for raw_relation in raw_relations:
        if not isinstance(raw_relation, Mapping):
            raise SemanticMappingContractError(f"{label} relation must be an object")
        _closed_keys(raw_relation, _RELATION_KEYS, f"{label} relation")
        raw_fields = raw_relation.get("fields")
        if (
            not isinstance(raw_fields, Sequence)
            or isinstance(raw_fields, (str, bytes, bytearray))
            or not raw_fields
            or len(raw_fields) > _MAX_FIELDS
        ):
            raise SemanticMappingContractError(
                f"{label} relation must declare 1-{_MAX_FIELDS} fields"
            )
        fields: list[dict[str, Any]] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                raise SemanticMappingContractError(f"{label} field must be an object")
            _closed_keys(raw_field, _FIELD_KEYS, f"{label} field")
            name = _required_text(raw_field.get("name"), f"{label} field name", maximum=300)
            raw_aliases = raw_field.get("aliases", [])
            if (
                not isinstance(raw_aliases, Sequence)
                or isinstance(raw_aliases, (str, bytes, bytearray))
                or len(raw_aliases) > _MAX_ALIASES
            ):
                raise SemanticMappingContractError(f"{label} field aliases are invalid")
            aliases = sorted(
                {
                    _required_text(alias, f"{label} field alias", maximum=300)
                    for alias in raw_aliases
                },
                key=input_contract_validator.normalize_content_name,
            )
            raw_types = raw_field.get("logical_types")
            if (
                not isinstance(raw_types, Sequence)
                or isinstance(raw_types, (str, bytes, bytearray))
                or not raw_types
            ):
                raise SemanticMappingContractError(f"{label} field logical types are invalid")
            logical_types = sorted(
                {
                    _required_text(
                        item,
                        f"{label} field logical type",
                        maximum=40,
                    ).lower()
                    for item in raw_types
                }
            )
            required = raw_field.get("required", True)
            if not isinstance(required, bool):
                raise SemanticMappingContractError(f"{label} field required must be boolean")
            fields.append(
                {
                    "name": name,
                    "aliases": aliases,
                    "logical_types": logical_types,
                    "required": required,
                }
            )
        minimum_rows = raw_relation.get("minimum_data_rows", 0)
        if (
            isinstance(minimum_rows, bool)
            or not isinstance(minimum_rows, int)
            or not 0 <= minimum_rows <= 1_000_000_000
        ):
            raise SemanticMappingContractError(f"{label} minimum_data_rows is invalid")
        allow_fields = raw_relation.get("allow_additional_fields", True)
        if not isinstance(allow_fields, bool):
            raise SemanticMappingContractError(
                f"{label} allow_additional_fields must be boolean"
            )
        relations.append(
            {
                "fields": fields,
                "minimum_data_rows": minimum_rows,
                "allow_additional_fields": allow_fields,
            }
        )
    allow_relations = content.get("allow_additional_relations", True)
    if not isinstance(allow_relations, bool):
        raise SemanticMappingContractError(
            f"{label} allow_additional_relations must be boolean"
        )
    canonical = {
        contract_key: {
            "version": input_contract_validator.CONTENT_CONTRACT_VERSION,
            "relations": relations,
            "allow_additional_relations": allow_relations,
        }
    }
    try:
        input_contract_validator.validate_content_contract(canonical)
    except input_contract_validator.InputContractError as exc:
        raise SemanticMappingContractError(exc.message) from exc
    return canonical


def _empty_extension(value: Any, *, label: str) -> dict[str, Any]:
    """Reject unversioned authoring JSON instead of turning it into runtime code/data."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or value:
        raise SemanticMappingContractError(
            f"{label} has no supported portable fields"
        )
    return {}


def normalize_contract(raw: Any) -> dict[str, Any]:
    """Normalize one closed, portable semantic mapping definition."""

    if not isinstance(raw, Mapping):
        raise SemanticMappingContractError("semantic mapping contract must be an object")
    allowed = {
        "contract_version",
        "id",
        "entity_id",
        "mapping_key",
        "schema_document",
        "fields",
        "identifier_strategy",
        "filter_expression",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise SemanticMappingContractError(
            "semantic mapping contract contains unsupported fields"
        )
    version = raw.get("contract_version")
    if version != CONTRACT_VERSION:
        raise SemanticMappingContractError("semantic mapping contract version is unsupported")

    schema_document = _canonical_content_schema(
        raw.get("schema_document"),
        label="semantic mapping contract",
    )
    content_contract = schema_document[input_contract_validator.CONTENT_CONTRACT_KEY]
    relations = content_contract.get("relations") if isinstance(content_contract, Mapping) else None
    if not isinstance(relations, Sequence) or len(relations) != 1:
        raise SemanticMappingContractError(
            "semantic mapping contract must describe exactly one logical relation"
        )
    contract_fields = relations[0].get("fields") if isinstance(relations[0], Mapping) else None
    if not isinstance(contract_fields, Sequence):
        raise SemanticMappingContractError("semantic mapping contract fields are invalid")

    raw_fields = raw.get("fields")
    if (
        not isinstance(raw_fields, Sequence)
        or isinstance(raw_fields, (str, bytes, bytearray))
        or not raw_fields
        or len(raw_fields) > _MAX_FIELDS
    ):
        raise SemanticMappingContractError(
            f"semantic mapping contract must declare 1-{_MAX_FIELDS} mapped fields"
        )
    fields: list[dict[str, Any]] = []
    property_ids: set[str] = set()
    contract_indexes: set[int] = set()
    for item in raw_fields:
        if not isinstance(item, Mapping) or set(item) - {
            "ontology_property_id",
            "contract_field_index",
            "direction",
            "is_required",
            "transform",
        }:
            raise SemanticMappingContractError("semantic field contract is invalid")
        property_id = _required_text(
            item.get("ontology_property_id"), "ontology property id", maximum=32
        )
        field_index = item.get("contract_field_index")
        if (
            isinstance(field_index, bool)
            or not isinstance(field_index, int)
            or field_index < 0
            or field_index >= len(contract_fields)
        ):
            raise SemanticMappingContractError("semantic contract field index is invalid")
        direction = str(item.get("direction") or "input").strip().lower()
        if direction not in {"input", "output", "bidirectional"}:
            raise SemanticMappingContractError("semantic field direction is invalid")
        is_required = item.get("is_required", False)
        if not isinstance(is_required, bool):
            raise SemanticMappingContractError("semantic field is_required must be boolean")
        if property_id in property_ids or field_index in contract_indexes:
            raise SemanticMappingContractError(
                "semantic field contracts must be unambiguous"
            )
        property_ids.add(property_id)
        contract_indexes.add(field_index)
        fields.append(
            {
                "ontology_property_id": property_id,
                "contract_field_index": field_index,
                "direction": direction,
                "is_required": is_required,
                "transform": _empty_extension(
                    item.get("transform"), label="semantic field transform"
                ),
            }
        )
    if not any(item["direction"] in {"input", "bidirectional"} for item in fields):
        raise SemanticMappingContractError(
            "semantic mapping contract has no readable input fields"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "id": _required_text(raw.get("id"), "semantic mapping id", maximum=32),
        "entity_id": _required_text(raw.get("entity_id"), "entity id", maximum=32),
        "mapping_key": _required_text(raw.get("mapping_key"), "mapping key", maximum=180),
        "schema_document": schema_document,
        "fields": fields,
        "identifier_strategy": _empty_extension(
            raw.get("identifier_strategy"), label="identifier strategy"
        ),
        "filter_expression": _empty_extension(
            raw.get("filter_expression"), label="filter expression"
        ),
    }


def contract_from_mapping(mapping: SemanticMapping) -> dict[str, Any]:
    """Project an authorized authoring row into its runtime-safe definition."""

    relation = mapping.dataset_relation
    if relation is None:
        raise SemanticMappingContractError("semantic mapping relation is unavailable")
    ordered_fields = sorted(
        tuple(relation.fields or ()),
        key=lambda item: (int(item.ordinal or 0), str(item.id or "")),
    )
    field_indexes = {str(field.id): index for index, field in enumerate(ordered_fields)}
    mapped_fields: list[dict[str, Any]] = []
    for field_mapping in sorted(
        tuple(mapping.field_mappings or ()),
        key=lambda item: (int(item.ordinal or 0), str(item.id or "")),
    ):
        field_index = field_indexes.get(str(field_mapping.dataset_field_id))
        if field_index is None:
            raise SemanticMappingContractError(
                "semantic field mapping is outside the authored logical relation"
            )
        mapped_fields.append(
            {
                "ontology_property_id": str(field_mapping.ontology_property_id),
                "contract_field_index": field_index,
                "direction": str(field_mapping.direction or "input"),
                "is_required": bool(field_mapping.is_required),
                "transform": copy.deepcopy(field_mapping.transform or {}),
            }
        )
    try:
        content_contract = input_contract_validator.build_tabular_content_contract(
            [relation]
        )
    except input_contract_validator.InputContractError as exc:
        raise SemanticMappingContractError(exc.message) from exc
    return normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "id": str(mapping.id),
            "entity_id": str(mapping.entity_id),
            "mapping_key": str(mapping.mapping_key or ""),
            "schema_document": {
                input_contract_validator.CONTENT_CONTRACT_KEY: content_contract
            },
            "fields": mapped_fields,
            "identifier_strategy": copy.deepcopy(mapping.identifier_strategy or {}),
            "filter_expression": copy.deepcopy(mapping.filter_expression or {}),
        }
    )


def load_live_contracts(
    db: Session,
    *,
    scenario_id: str,
    tenant_id: str,
) -> dict[str, dict[str, Any]]:
    """Load active modeling mappings and detach them from Catalog identities."""

    rows = list(
        db.scalars(
            select(SemanticMapping)
            .join(
                LogicalDataset,
                (LogicalDataset.id == SemanticMapping.dataset_id)
                & (LogicalDataset.tenant_id == SemanticMapping.tenant_id),
            )
            .options(
                selectinload(SemanticMapping.dataset_relation).selectinload(
                    DatasetRelation.fields
                ),
                selectinload(SemanticMapping.field_mappings).selectinload(
                    SemanticFieldMapping.dataset_field
                ),
            )
            .where(
                SemanticMapping.scenario_id == scenario_id,
                SemanticMapping.tenant_id == tenant_id,
                SemanticMapping.status == "active",
                LogicalDataset.usage_plane == "modeling_material",
                LogicalDataset.lifecycle_status == "active",
            )
            .order_by(SemanticMapping.id.asc())
        ).all()
    )
    return {str(mapping.id): contract_from_mapping(mapping) for mapping in rows}


def normalize_relation_contract(raw: Any) -> dict[str, Any]:
    """Normalize one relation descriptor without retaining Catalog identity."""

    if not isinstance(raw, Mapping):
        raise SemanticMappingContractError(
            "semantic relation mapping contract must be an object"
        )
    allowed = {
        "contract_version",
        "id",
        "ontology_relation_id",
        "source_semantic_mapping_id",
        "target_semantic_mapping_id",
        "source_contract_field_index",
        "target_contract_field_index",
        "mode",
        "relation_schema_document",
        "condition_document",
    }
    if set(raw) - allowed:
        raise SemanticMappingContractError(
            "semantic relation mapping contract contains unsupported fields"
        )
    if raw.get("contract_version") != CONTRACT_VERSION:
        raise SemanticMappingContractError(
            "semantic relation mapping contract version is unsupported"
        )
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in {"foreign_key", "join_relation", "computed"}:
        raise SemanticMappingContractError("semantic relation mapping mode is invalid")
    indexes: list[int] = []
    for key in ("source_contract_field_index", "target_contract_field_index"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SemanticMappingContractError(
                "semantic relation mapping field index is invalid"
            )
        indexes.append(value)
    schema_document = _canonical_content_schema(
        raw.get("relation_schema_document"),
        label="semantic relation mapping",
    )
    relation_document = schema_document[input_contract_validator.CONTENT_CONTRACT_KEY]
    relation_entries = (
        relation_document.get("relations")
        if isinstance(relation_document, Mapping)
        else None
    )
    if not isinstance(relation_entries, Sequence) or len(relation_entries) != 1:
        raise SemanticMappingContractError(
            "semantic relation mapping must describe exactly one logical relation"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "id": _required_text(raw.get("id"), "semantic relation mapping id", maximum=32),
        "ontology_relation_id": _required_text(
            raw.get("ontology_relation_id"), "ontology relation id", maximum=32
        ),
        "source_semantic_mapping_id": _required_text(
            raw.get("source_semantic_mapping_id"),
            "source semantic mapping id",
            maximum=32,
        ),
        "target_semantic_mapping_id": _required_text(
            raw.get("target_semantic_mapping_id"),
            "target semantic mapping id",
            maximum=32,
        ),
        "source_contract_field_index": indexes[0],
        "target_contract_field_index": indexes[1],
        "mode": mode,
        "relation_schema_document": schema_document,
        "condition_document": _empty_extension(
            raw.get("condition_document"), label="semantic relation condition"
        ),
    }


def _relation_field_index(mapping: SemanticMapping, field_id: str) -> int:
    fields = sorted(
        tuple(mapping.dataset_relation.fields or ()),
        key=lambda item: (int(item.ordinal or 0), str(item.id or "")),
    )
    for index, field in enumerate(fields):
        if str(field.id) == str(field_id):
            return index
    raise SemanticMappingContractError(
        "semantic relation field is outside its endpoint mapping contract"
    )


def contract_from_relation_mapping(
    mapping: SemanticRelationMapping,
) -> dict[str, Any]:
    """Detach one governed relation mapping from all modeling Catalog ids."""

    source_mapping = mapping.source_semantic_mapping
    target_mapping = mapping.target_semantic_mapping
    relation = mapping.dataset_relation
    if source_mapping is None or target_mapping is None or relation is None:
        raise SemanticMappingContractError(
            "semantic relation mapping dependencies are unavailable"
        )
    try:
        relation_contract = input_contract_validator.build_tabular_content_contract(
            [relation]
        )
    except input_contract_validator.InputContractError as exc:
        raise SemanticMappingContractError(exc.message) from exc
    return normalize_relation_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "id": str(mapping.id),
            "ontology_relation_id": str(mapping.ontology_relation_id),
            "source_semantic_mapping_id": str(mapping.source_semantic_mapping_id),
            "target_semantic_mapping_id": str(mapping.target_semantic_mapping_id),
            "source_contract_field_index": _relation_field_index(
                source_mapping, str(mapping.source_field_id)
            ),
            "target_contract_field_index": _relation_field_index(
                target_mapping, str(mapping.target_field_id)
            ),
            "mode": str(mapping.mode or ""),
            "relation_schema_document": {
                input_contract_validator.CONTENT_CONTRACT_KEY: relation_contract
            },
            "condition_document": copy.deepcopy(mapping.condition_document or {}),
        }
    )


def load_live_relation_contracts(
    db: Session,
    *,
    scenario_id: str,
    tenant_id: str,
    semantic_mapping_contracts: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Load active relation mappings whose schema-first endpoints are active."""

    rows = list(
        db.scalars(
            select(SemanticRelationMapping)
            .options(
                selectinload(SemanticRelationMapping.dataset_relation).selectinload(
                    DatasetRelation.fields
                ),
                selectinload(
                    SemanticRelationMapping.source_semantic_mapping
                ).selectinload(SemanticMapping.dataset_relation).selectinload(
                    DatasetRelation.fields
                ),
                selectinload(
                    SemanticRelationMapping.target_semantic_mapping
                ).selectinload(SemanticMapping.dataset_relation).selectinload(
                    DatasetRelation.fields
                ),
            )
            .where(
                SemanticRelationMapping.scenario_id == scenario_id,
                SemanticRelationMapping.tenant_id == tenant_id,
                SemanticRelationMapping.status == "active",
            )
            .order_by(SemanticRelationMapping.id.asc())
        ).all()
    )
    result: dict[str, dict[str, Any]] = {}
    available = set(semantic_mapping_contracts)
    for mapping in rows:
        endpoints = {
            str(mapping.source_semantic_mapping_id),
            str(mapping.target_semantic_mapping_id),
        }
        if not endpoints <= available:
            raise SemanticMappingContractError(
                "active semantic relation mapping has an unavailable modeling endpoint"
            )
        result[str(mapping.id)] = contract_from_relation_mapping(mapping)
    return result


__all__ = [
    "CONTRACT_VERSION",
    "SemanticMappingContractError",
    "contract_from_mapping",
    "contract_from_relation_mapping",
    "load_live_contracts",
    "load_live_relation_contracts",
    "normalize_contract",
    "normalize_relation_contract",
]
