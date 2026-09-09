"""Manual object identity, lifecycle policy and current integrity assessment."""
from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import OntologyEntity, OntologyInstance, OntologyRelation
from ..ontology_semantics_schemas import InstanceIntegrity, StatePolicy


class InstanceContractConflict(ValueError):
    pass


def protect_existing_identity(db: Session, entity: Any, properties: list[Any]) -> None:
    db.execute(select(OntologyEntity.id).where(OntologyEntity.id == entity.id).with_for_update())
    old = [(prop.name, prop.data_type) for prop in entity.properties if prop.is_key]
    new = [(prop.name, prop.data_type) for prop in properties if prop.is_key]
    if old != new and db.scalar(select(OntologyInstance.id).where(
        OntologyInstance.entity_id == entity.id, OntologyInstance.source == "manual").limit(1)):
        raise InstanceContractConflict("已有手工实例时不能直接更换、重命名或删除业务主键，请先完成显式数据迁移")


def normalize_state_policy(value: Any, state_property: str, properties: list[Any]) -> dict:
    policy = StatePolicy.model_validate(value or {})
    if policy.enabled:
        state = next((prop for prop in properties if prop.name == state_property), None)
        if state is None or not state.is_enum:
            raise ValueError("启用状态迁移策略必须先选择枚举状态属性")
        allowed = set(state.enum_values or [])
        if not policy.initial_states or not set(policy.initial_states) <= allowed:
            raise ValueError("初始状态必须从状态属性的枚举中选择，且至少选择一项")
        transitions = [(item.from_state, item.to_state) for item in policy.transitions]
        if len(transitions) != len(set(transitions)):
            raise ValueError("状态迁移不能重复")
        if any(source not in allowed or target not in allowed for source, target in transitions):
            raise ValueError("状态迁移的起点和终点必须属于状态属性枚举")
    return policy.model_dump()


def validate_manual_write(entity: Any, attributes: dict, state: str, *, previous: Any = None) -> None:
    keys = [prop for prop in entity.properties if prop.is_key]
    if len(keys) == 1:
        value = attributes.get(keys[0].name)
        if value is None or value == "" or not isinstance(value, (str, int, float, bool)):
            raise InstanceContractConflict("对象主键必须填写有效的标量值")
    if previous is not None and previous.entity_id != entity.id:
        raise InstanceContractConflict("已有对象不能直接更换对象类型")
    policy = StatePolicy.model_validate(getattr(entity, "state_policy", {}) or {})
    if not policy.enabled:
        return
    if previous is None:
        if state not in policy.initial_states:
            raise InstanceContractConflict("当前状态不属于允许的初始状态")
    elif state != previous.state and not any(
        item.from_state == previous.state and item.to_state == state for item in policy.transitions
    ):
        raise InstanceContractConflict("当前状态迁移不在对象类型允许的迁移中")


def commit_instance(db: Session) -> None:
    """Router-owned transaction boundary with safe domain conflict messages."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
        messages = {
            "uq_instances_business_key": "该对象类型已存在相同主键的手工实例，请编辑已有对象或使用其他主键",
            "ck_instance_identity": "对象主键缺失、定义冲突或对象类型发生变化",
            "ck_instance_state_policy": "对象状态不符合初始状态或允许的迁移策略",
        }
        if name in messages:
            raise InstanceContractConflict(messages[name]) from exc
        raise


def integrity(db: Session, instance: Any) -> InstanceIntegrity:
    from . import ontology_service

    issues: list[str] = []
    entity = instance.entity
    if entity is None:
        return InstanceIntegrity(status="invalid", issues=["对象类型不可用"])
    try:
        ontology_service.validate_instance_payload(entity, instance.attributes, state=instance.state or "")
    except ValueError:
        issues.append("对象属性不符合当前类型或约束")
    keys = [prop for prop in entity.properties if prop.is_key]
    if len(keys) != 1 or instance.attributes.get(keys[0].name) in (None, ""):
        issues.append("缺少明确的业务主键")
    invalid = bool(issues)
    # Reuse loaded edge collections. Completeness changes immediately after a
    # relationship write, without a client-controlled quality flag.
    outgoing = Counter(edge.relation_id for edge in instance.source_instances)
    incoming = Counter(edge.relation_id for edge in instance.target_instances)
    relations = db.execute(select(OntologyRelation).where(
        OntologyRelation.scenario_id == instance.scenario_id,
        (OntologyRelation.source_entity_id == instance.entity_id)
        | (OntologyRelation.target_entity_id == instance.entity_id),
    )).scalars().all()
    for relation in relations:
        limits = ontology_service.effective_relation_cardinality_limits(
            relation.relation_type, relation.constraints or {})
        for applies, count, side in (
            (relation.source_entity_id == entity.id, outgoing[relation.id], "source"),
            (relation.target_entity_id == entity.id, incoming[relation.id], "target"),
        ):
            minimum = limits[f"{side}_min"]
            if applies and minimum is not None and count < minimum:
                issues.append(f"关系“{relation.name}”尚未满足最小关联要求")
    return InstanceIntegrity(status="invalid" if invalid else "incomplete" if issues else "valid",
                             issues=issues)
