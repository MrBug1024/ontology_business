"""Govern Catalog mapping candidates through the existing definition boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..catalog_schemas import SemanticFieldMappingCreate, SemanticMappingCreate
from ..models import BusinessScenario, OntologyProperty, SemanticFieldMapping, SemanticMapping
from . import catalog_service, permission_service
from .policies import PolicyViolation


class MappingDeclaration(BaseModel):
    entity_id: str = Field(min_length=1, max_length=32)
    dataset_schema_id: str = Field(min_length=1, max_length=32)
    dataset_relation_id: str = Field(min_length=1, max_length=32)
    mapping_key: str = Field(min_length=1, max_length=180, pattern=r'^[a-z0-9][a-z0-9._-]{0,179}$')
    fields: list[SemanticFieldMappingCreate] = Field(min_length=1, max_length=500)
    existing_id: str | None = Field(default=None, min_length=1, max_length=32)
    expected_updated_at: datetime | None = None

    model_config = {'extra': 'forbid'}

    @model_validator(mode='after')
    def valid_references(self) -> MappingDeclaration:
        if bool(self.existing_id) != bool(self.expected_updated_at):
            raise ValueError('补齐已有映射必须同时声明 existing_id 和 expected_updated_at')
        if self.expected_updated_at and self.expected_updated_at.tzinfo is None:
            raise ValueError('映射修订时间必须包含时区')
        if len({field.ontology_property_id for field in self.fields}) != len(self.fields):
            raise ValueError('对象属性不能重复映射')
        if len({field.dataset_field_id for field in self.fields}) != len(self.fields):
            raise ValueError('同一资料字段不能重复映射')
        if any(field.transform for field in self.fields):
            raise ValueError('当前语义映射只支持直接字段对应，不能编造转换表达式')
        return self


_AUTHOR_METADATA = frozenset({'key', 'evidence_refs', 'confidence', 'name', 'operation'})


def declaration(raw: dict[str, Any]) -> MappingDeclaration:
    try:
        return MappingDeclaration.model_validate({key: value for key, value in raw.items() if key not in _AUTHOR_METADATA})
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        details = '; '.join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors[:8])
        raise ValueError(f'语义映射声明无效：{details}') from exc


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    value = declaration(raw)
    return {**value.model_dump(mode='json', exclude_none=True),
            'operation': 'update' if value.existing_id else 'add'}


def _field_payload(field: SemanticFieldMapping) -> SemanticFieldMappingCreate:
    return SemanticFieldMappingCreate(ontology_property_id=field.ontology_property_id,
        dataset_field_id=field.dataset_field_id, direction=field.direction,
        is_required=field.is_required, transform=field.transform or {})


def validate(
    db: Session, scenario: BusinessScenario, raw: dict[str, Any], *, lock: bool = False,
) -> tuple[MappingDeclaration, SemanticMappingCreate, SemanticMapping | None, list[SemanticFieldMappingCreate]]:
    """Recheck ownership, source authority and exact revision without writing."""
    value = declaration(raw)
    current = None
    if value.existing_id:
        statement = select(SemanticMapping).where(SemanticMapping.id == value.existing_id,
            SemanticMapping.scenario_id == scenario.id, SemanticMapping.tenant_id == scenario.tenant_id)
        if lock:
            statement = statement.with_for_update()
        current = db.scalar(statement.execution_options(populate_existing=True))
        if current is None:
            raise PolicyViolation('语义映射不存在或无权访问')
        if current.updated_at != value.expected_updated_at:
            raise PolicyViolation('语义映射已变化，请重新编译或校验')
        if current.status not in {'draft', 'active'}:
            raise PolicyViolation('已退役或异常的语义映射不能通过补齐候选恢复')
        for field in ('entity_id', 'dataset_schema_id', 'dataset_relation_id', 'mapping_key'):
            if getattr(current, field) != getattr(value, field):
                raise PolicyViolation('补齐映射不能替换已有对象、Schema、关系或稳定标识')
        if current.identifier_strategy or current.filter_expression:
            raise PolicyViolation('带自定义标识或过滤的映射需要单独审阅，不能自动补齐')
    else:
        conflict = db.scalar(select(SemanticMapping.id).where(SemanticMapping.scenario_id == scenario.id,
            or_(SemanticMapping.mapping_key == value.mapping_key,
                (SemanticMapping.entity_id == value.entity_id) & (SemanticMapping.dataset_schema_id == value.dataset_schema_id))))
        if conflict:
            raise PolicyViolation('对应语义映射已存在，补齐时必须显式引用其身份和修订时间')
    merged = {item.ontology_property_id: _field_payload(item) for item in current.field_mappings} if current else {}
    additions = []
    for item in value.fields:
        previous = merged.get(item.ontology_property_id)
        if previous and previous != item:
            raise PolicyViolation('补齐候选不能改变已有属性的字段对应或要求')
        if previous is None:
            additions.append(item)
        merged[item.ontology_property_id] = item
    if current and not additions:
        raise PolicyViolation('候选没有新增字段，现有语义映射已覆盖这些对应')
    if len({item.dataset_field_id for item in merged.values()}) != len(merged):
        raise PolicyViolation('字段对应与现有映射重复')
    payload = SemanticMappingCreate(entity_id=value.entity_id, dataset_schema_id=value.dataset_schema_id,
        dataset_relation_id=value.dataset_relation_id, mapping_key=value.mapping_key,
        fields=list(merged.values()), status='active')
    catalog_service.validate_semantic_mapping(db, scenario.id, payload)
    properties = list(db.scalars(select(OntologyProperty).where(OntologyProperty.entity_id == value.entity_id)).all())
    keys = [prop.id for prop in properties if prop.is_key]
    if not keys or any(key not in merged for key in keys):
        raise PolicyViolation('语义映射必须包含对象的全部主键属性')
    for prop in properties:
        if prop.id in merged and not permission_service.can_read_property(db, prop):
            raise PolicyViolation('映射包含无权访问的对象属性')
    return value, payload, current, additions


def apply(db: Session, scenario: BusinessScenario, raw: dict[str, Any]) -> SemanticMapping:
    value, payload, current, additions = validate(db, scenario, raw, lock=True)
    try:
        if current is None:
            return catalog_service.create_semantic_mapping(db, scenario.id, payload)
        ordinal = max((field.ordinal for field in current.field_mappings), default=-1) + 1
        for index, item in enumerate(additions):
            db.add(SemanticFieldMapping(tenant_id=current.tenant_id, scenario_id=scenario.id,
                dataset_id=current.dataset_id, dataset_schema_id=current.dataset_schema_id,
                dataset_relation_id=current.dataset_relation_id, ontology_entity_id=current.entity_id,
                semantic_mapping_id=current.id, ordinal=ordinal + index, **item.model_dump()))
        current.status = 'active'
        current.updated_at = datetime.now(timezone.utc)
        db.flush()
        db.expire(current, ['field_mappings'])
        return current
    except IntegrityError as exc:
        raise PolicyViolation('语义映射在写入时发生并发变更，本批次未应用，请重新校验') from exc


AUTHORING_GUIDANCE = """
Catalog 建模资料优先输出 semantic_mappings，而不是旧 DataSource mappings 或不可晋级的 conceptual_mappings。
semantic_mappings 条目：{key,mapping_key,entity_id,dataset_schema_id,dataset_relation_id,
fields:[{ontology_property_id,dataset_field_id,direction:"input",is_required:true}],evidence_refs,confidence}。
只能使用目录中明确列出的已有对象/属性和建模 Schema/关系/字段 ID；禁止引用运行版本、物理地址或 SQL。
mapping_key 为小写英文稳定标识。字段数不应只取示例或主键；逐项匹配已有对象中有可靠来源的全部字段，不能猜测缺失语义。
补齐已有语义映射必须带 existing_id 和 expected_updated_at，mapping_key/对象/Schema/关系沿用原值；
fields 可仅列新增属性，原有对应会保留，禁止改变其字段、方向、必填或转换。
只遍历当前正式对象已有的属性，不能因为源表还有其他列就编造属性 ID。
已明确安排到后续阶段的计算字段，在 coverage.context 的 reason 记录计算任务；本轮不伪造直接映射，也不把已确定口径当作业务歧义。
新对象与属性须先完成本体阶段再建立语义映射。候选需要服务端校验与人工确认；映射不是运行数据。
有 Catalog Schema 时不再声称没有数据源。未匹配字段明确记录原因，不生成空壳映射。
本轮顶层协议另包含 semantic_mappings 数组；非 mapping 阶段输出 []。
"""
