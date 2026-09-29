"""Bounded modeling Schema metadata for the frozen compiler context."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import BusinessScenario, DatasetRelation, DatasetSchema
from . import catalog_service


MAX_AUTHORING_DATASETS = 50
MAX_AUTHORING_FIELDS = 1500
MAX_AUTHORING_MAPPINGS = 100


def catalog(db: Session, scenario: BusinessScenario) -> list[dict[str, Any]]:
    datasets = [dataset for dataset in catalog_service.list_datasets(db,
        usage_plane='modeling_material', scenario_id=scenario.id) if dataset.lifecycle_status == 'active']
    if len(datasets) > MAX_AUTHORING_DATASETS:
        raise ValueError('建模 Schema 目录超过 50 个数据集，请缩小场景资料范围')
    result = []
    field_count = 0
    for dataset in datasets:
        catalog_service._require_modeling_contract_source_access(db, dataset, scenario_id=scenario.id, writable=True)
        latest = max(dataset.schemas, key=lambda item: item.schema_version, default=None)
        if latest is None:
            continue
        schema = db.scalar(select(DatasetSchema).where(DatasetSchema.id == latest.id,
            DatasetSchema.tenant_id == scenario.tenant_id).options(
                selectinload(DatasetSchema.relations).selectinload(DatasetRelation.fields)))
        relations = []
        for relation in schema.relations:
            field_count += len(relation.fields)
            if field_count > MAX_AUTHORING_FIELDS:
                raise ValueError('建模字段目录超过单次 1500 项边界，请缩小场景资料范围')
            relations.append({'id': relation.id, 'name': relation.display_name,
                'fields': [{'id': item.id, 'name': item.source_name, 'type': item.logical_type}
                           for item in relation.fields]})
        result.append({'kind': 'semantic_schema', 'dataset_name': dataset.name,
            'dataset_schema_id': schema.id, 'schema_hash': schema.schema_hash, 'relations': relations})
    mappings = catalog_service.list_semantic_mappings(db, scenario.id)
    if len(mappings) > MAX_AUTHORING_MAPPINGS:
        raise ValueError('现有映射超过单次 100 项边界，请缩小场景资料范围')
    for mapping in mappings:
        field_count += len(mapping.field_mappings)
        if field_count > MAX_AUTHORING_FIELDS:
            raise ValueError('建模字段和映射目录超过单次 1500 项边界，请缩小场景资料范围')
        result.append({'kind': 'semantic_mapping', 'existing_id': mapping.id,
            'expected_updated_at': mapping.updated_at.isoformat(), 'entity_id': mapping.entity_id,
            'mapping_key': mapping.mapping_key, 'dataset_schema_id': mapping.dataset_schema_id,
            'dataset_relation_id': mapping.dataset_relation_id,
            'fields': [{'ontology_property_id': field.ontology_property_id,
                        'dataset_field_id': field.dataset_field_id, 'direction': field.direction,
                        'is_required': field.is_required, 'transform': field.transform or {}}
                       for field in mapping.field_mappings]})
    return result
