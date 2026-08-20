"""业务场景 & 本体建模路由。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..models import (
    ActionExecutionLog,
    BusinessScenario,
    DataMapping,
    DataSource,
    MCPConfig,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    RelationInstance,
)
from ..schemas import (
    ActionExecuteRequest,
    ActionIn,
    ActionOut,
    ActionExecutionLogOut,
    DataMappingIn,
    DataMappingOut,
    DataMappingPreviewOut,
    DataMappingRefreshOut,
    DataMappingTestOut,
    EntityIn,
    EntityOut,
    EventIn,
    EventOut,
    InstanceIn,
    InstanceOut,
    Msg,
    ObjectDetailOut,
    ObjectProvenanceOut,
    ObjectRelationOut,
    ObjectSearchItemOut,
    ObjectSearchOut,
    PropertyIn,
    RelationIn,
    RelationInstanceIn,
    RelationInstanceOut,
    RelationOut,
    RuleIn,
    RuleOut,
    ScenarioDetail,
    ScenarioIn,
    ScenarioOut,
    WorkflowExecuteRequest,
    WorkflowGenerateRequest,
    WorkflowIn,
    WorkflowOut,
)
from ..services import ontology_service, tenant_service, workflow_service
from ..services.auth_service import get_current_user

router = APIRouter(
    prefix="/scenarios",
    tags=["scenarios"],
    dependencies=[Depends(get_current_user)],
)


def _entity_in_scenario(db: Session, scenario_id: str, entity_id: str | None) -> OntologyEntity | None:
    if not entity_id:
        return None
    entity = db.get(OntologyEntity, entity_id)
    if not entity or entity.scenario_id != scenario_id:
        raise HTTPException(400, "实体不属于当前业务场景")
    return entity


def _source_in_scenario(db: Session, scenario_id: str, source_id: str) -> DataSource:
    source = tenant_service.require_visible(db, DataSource, source_id, "数据源不存在")
    if not source or source.scenario_id not in (None, scenario_id):
        raise HTTPException(400, "数据源不属于当前业务场景")
    return source


def _mapping_for_request(db: Session, mapping_id: str, writable: bool = False) -> DataMapping:
    mapping = db.get(DataMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "映射不存在")
    _scenario_for_request(db, mapping.scenario_id, writable=writable)
    _source_in_scenario(db, mapping.scenario_id, mapping.data_source_id)
    return mapping


def _mapping_limit(payload: dict | None, default: int, maximum: int) -> int:
    try:
        value = int((payload or {}).get("limit", default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "limit 必须是整数") from exc
    return max(1, min(value, maximum))


def _validate_action_executor(db: Session, scenario_id: str, payload: ActionIn) -> None:
    """校验操作执行器引用的资源边界，保持操作配置可移植且不跨场景。"""
    config = payload.executor_config or {}
    if payload.executor_type == "sql" and config.get("data_source_id"):
        _source_in_scenario(db, scenario_id, config["data_source_id"])
    if payload.executor_type == "mcp" and config.get("mcp_id"):
        tenant_service.require_visible(db, MCPConfig, config["mcp_id"], "操作引用的 MCP 服务不存在")


def _validate_trigger_actions(db: Session, scenario_id: str, action_ids: list[str] | None) -> None:
    for action_id in action_ids or []:
        action = db.get(OntologyAction, action_id)
        if not action or action.scenario_id != scenario_id:
            raise HTTPException(400, "规则触发的操作不属于当前业务场景")


def _validate_workflow_refs(db: Session, scenario_id: str, steps: list, nodes: list) -> None:
    """校验工作流引用的 Action/Rule/Event，防止跨场景拼接执行图。"""
    refs = [(s.get("type"), s) for s in (steps or [])] + [(n.get("type"), n.get("data") or n) for n in (nodes or [])]
    for kind, data in refs:
        model, key = {
            "action": (OntologyAction, "action_id"),
            "rule": (OntologyRule, "rule_id"),
            "event": (OntologyEvent, "event_id"),
        }.get(kind, (None, ""))
        if not model or not data.get(key):
            continue
        item = db.get(model, data[key])
        if not item or item.scenario_id != scenario_id:
            raise HTTPException(400, f"工作流引用的 {kind} 不属于当前业务场景")


def _scenario_out(s: BusinessScenario) -> ScenarioOut:
    return ScenarioOut(
        id=s.id,
        name=s.name,
        description=s.description,
        industry=s.industry,
        status=s.status,
        created_at=s.created_at,
        updated_at=s.updated_at,
        entity_count=len(s.entities),
        relation_count=len(s.relations),
        data_source_count=len(s.data_sources),
        action_count=len(s.actions),
        rule_count=len(s.rules),
        event_count=len(s.events),
        workflow_count=len(s.workflows),
    )


def _scenario_for_request(db: Session, scenario_id: str, writable: bool = False) -> BusinessScenario:
    return tenant_service.require_scenario(db, scenario_id, writable=writable)


def _safe_source_config(config: dict) -> dict:
    safe = dict(config or {})
    for key in ("password", "api_key", "token", "secret", "access_token"):
        if key in safe:
            safe[key] = ""
    return safe


def _action_out(a: OntologyAction) -> ActionOut:
    return ActionOut(
        id=a.id,
        scenario_id=a.scenario_id,
        entity_id=a.entity_id,
        name=a.name,
        description=a.description,
        input_schema=a.input_schema or {},
        executor_type=a.executor_type,
        executor_config=a.executor_config or {},
        precondition=a.precondition,
        postcondition=a.postcondition,
        enabled=a.enabled,
        requires_confirmation=a.requires_confirmation,
        idempotency_required=a.idempotency_required,
        permission_scope=a.permission_scope or "scenario",
        entity_name=a.entity.name if a.entity else "",
        created_at=a.created_at,
    )


def _rule_out(r: OntologyRule) -> RuleOut:
    return RuleOut(
        id=r.id,
        scenario_id=r.scenario_id,
        entity_id=r.entity_id,
        name=r.name,
        description=r.description,
        condition=r.condition or {},
        action_on_match=r.action_on_match,
        trigger_action_ids=r.trigger_action_ids or [],
        severity=r.severity,
        enabled=r.enabled,
        entity_name=r.entity.name if r.entity else "",
        created_at=r.created_at,
    )


def _event_out(e: OntologyEvent) -> EventOut:
    return EventOut(
        id=e.id,
        scenario_id=e.scenario_id,
        name=e.name,
        description=e.description,
        payload_schema=e.payload_schema or {},
        trigger_source=e.trigger_source,
        enabled=e.enabled,
        created_at=e.created_at,
    )


def _workflow_out(w: OntologyWorkflow) -> WorkflowOut:
    return WorkflowOut(
        id=w.id,
        scenario_id=w.scenario_id,
        name=w.name,
        description=w.description,
        trigger_type=w.trigger_type,
        trigger_config=w.trigger_config or {},
        steps=w.steps or [],
        nodes=w.nodes or [],
        edges=w.edges or [],
        enabled=w.enabled,
        created_at=w.created_at,
    )


def _entity_out(e: OntologyEntity) -> EntityOut:
    return EntityOut(
        id=e.id,
        scenario_id=e.scenario_id,
        name=e.name,
        description=e.description,
        icon=e.icon,
        color=e.color,
        is_abstract=e.is_abstract,
        created_at=e.created_at,
        properties=[
            PropertyIn(
                name=p.name,
                data_type=p.data_type,
                description=p.description,
                is_key=p.is_key,
                is_required=p.is_required,
                is_enum=p.is_enum,
                enum_values=p.enum_values or [],
                default_value=p.default_value,
            )
            for p in e.properties
        ],
    )


def _relation_out(r: OntologyRelation, entities: list[OntologyEntity]) -> RelationOut:
    name_map = {e.id: e.name for e in entities}
    return RelationOut(
        id=r.id,
        scenario_id=r.scenario_id,
        name=r.name,
        source_entity_id=r.source_entity_id,
        target_entity_id=r.target_entity_id,
        relation_type=r.relation_type,
        description=r.description,
        source_entity_name=name_map.get(r.source_entity_id, ""),
        target_entity_name=name_map.get(r.target_entity_id, ""),
    )


@router.get("", response_model=list[ScenarioOut])
def list_scenarios(db: Session = Depends(get_db)):
    return [_scenario_out(s) for s in db.execute(
        select(BusinessScenario).where(tenant_service.visible_clause(BusinessScenario, db))
    ).scalars().all()]


@router.post("", response_model=ScenarioOut)
def create_scenario(payload: ScenarioIn, db: Session = Depends(get_db)):
    s = BusinessScenario(
        tenant_id=tenant_service.current_tenant_id(db),
        **payload.model_dump(),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _scenario_out(s)


def _instance_out(i: OntologyInstance) -> InstanceOut:
    ent = i.entity
    return InstanceOut(
        id=i.id,
        scenario_id=i.scenario_id,
        entity_id=i.entity_id,
        name=i.name,
        attributes=i.attributes or {},
        source=i.source,
        source_ref=i.source_ref,
        entity_name=ent.name if ent else "",
        entity_color=ent.color if ent else "",
        created_at=i.created_at,
    )


def _rel_instance_out(ri: RelationInstance) -> RelationInstanceOut:
    rel = ri.relation
    return RelationInstanceOut(
        id=ri.id,
        scenario_id=ri.scenario_id,
        relation_id=ri.relation_id,
        source_instance_id=ri.source_instance_id,
        target_instance_id=ri.target_instance_id,
        attributes=ri.attributes or {},
        relation_name=rel.name if rel else "",
        source_instance_name=ri.source_instance.name if ri.source_instance else "",
        target_instance_name=ri.target_instance.name if ri.target_instance else "",
        created_at=ri.created_at,
    )


def _mapping_out(m: DataMapping) -> DataMappingOut:
    ent = m.entity
    ds = m.data_source
    return DataMappingOut(
        id=m.id,
        scenario_id=m.scenario_id,
        entity_id=m.entity_id,
        data_source_id=m.data_source_id,
        table_name=m.table_name,
        column_map=m.column_map or {},
        entity_name=ent.name if ent else "",
        data_source_name=ds.name if ds else "",
        data_source_type=ds.type if ds else "",
        status=m.status or "unknown",
        last_error=m.last_error or "",
        last_checked_at=m.last_checked_at,
        last_refreshed_at=m.last_refreshed_at,
        last_row_count=m.last_row_count or 0,
        last_imported_count=m.last_imported_count or 0,
        created_at=m.created_at,
    )


def _object_provenance(
    db: Session,
    instance: OntologyInstance,
    mapping: DataMapping | None = None,
) -> ObjectProvenanceOut:
    """把对象的导入来源解析成安全的、可展示的来源摘要。"""
    if mapping is None:
        mapping = db.execute(
            select(DataMapping)
            .where(
                DataMapping.scenario_id == instance.scenario_id,
                DataMapping.entity_id == instance.entity_id,
            )
            .limit(1)
        ).scalar_one_or_none()
    source = mapping.data_source if mapping else None
    return ObjectProvenanceOut(
        kind=instance.source or "manual",
        reference=instance.source_ref or "",
        mapping_id=mapping.id if mapping else None,
        data_source_id=source.id if source else None,
        data_source_name=source.name if source else "",
        table_name=mapping.table_name if mapping else "",
        status=source.status if source else "unknown",
    )


def _object_item_out(
    db: Session,
    instance: OntologyInstance,
    *,
    mapping: DataMapping | None = None,
    relation_count: int | None = None,
) -> ObjectSearchItemOut:
    entity = instance.entity
    if relation_count is None:
        relation_ids = {r.id for r in [*instance.source_instances, *instance.target_instances]}
        relation_count = len(relation_ids)
    return ObjectSearchItemOut(
        id=instance.id,
        scenario_id=instance.scenario_id,
        entity_id=instance.entity_id,
        entity_name=entity.name if entity else "",
        entity_color=entity.color if entity else "",
        name=instance.name,
        attributes=instance.attributes or {},
        source=instance.source or "manual",
        source_ref=instance.source_ref or "",
        provenance=_object_provenance(db, instance, mapping),
        relation_count=relation_count,
        created_at=instance.created_at,
    )


def _object_detail_out(db: Session, instance: OntologyInstance) -> ObjectDetailOut:
    item = _object_item_out(db, instance)
    relations: list[ObjectRelationOut] = []
    seen: set[str] = set()
    for relation_instance in [*instance.source_instances, *instance.target_instances]:
        if relation_instance.id in seen:
            continue
        seen.add(relation_instance.id)
        outgoing = relation_instance.source_instance_id == instance.id
        related = relation_instance.target_instance if outgoing else relation_instance.source_instance
        relation = relation_instance.relation
        if not related:
            continue
        related_entity = related.entity
        relations.append(
            ObjectRelationOut(
                id=relation_instance.id,
                direction="outgoing" if outgoing else "incoming",
                relation_id=relation_instance.relation_id,
                relation_name=relation.name if relation else "",
                relation_type=(relation.relation_type if relation else ""),
                related_object_id=related.id,
                related_object_name=related.name,
                related_entity_id=related.entity_id,
                related_entity_name=related_entity.name if related_entity else "",
                attributes=relation_instance.attributes or {},
                created_at=relation_instance.created_at,
            )
        )
    relations.sort(key=lambda r: (r.direction, r.relation_name, r.related_object_name))
    return ObjectDetailOut(**item.model_dump(), relations=relations)


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario(scenario_id: str, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id)
    base = _scenario_out(s)
    entities = [_entity_out(e) for e in s.entities]
    relations = [_relation_out(r, s.entities) for r in s.relations]
    from ..schemas import DataSourceOut

    ds_out = [
        DataSourceOut(
            id=d.id,
            scenario_id=d.scenario_id,
            name=d.name,
            type=d.type,
            config=_safe_source_config(d.config or {}),
            status=d.status,
            last_error=d.last_error,
            created_at=d.created_at,
            file_count=len(d.files),
        )
        for d in s.data_sources
        if d.tenant_id == tenant_service.current_tenant_id(db) or d.is_public
    ]
    instances = [_instance_out(i) for i in s.instances]
    rel_instances = [_rel_instance_out(ri) for ri in s.relation_instances]
    mappings = [_mapping_out(m) for m in s.data_mappings]
    actions = [_action_out(a) for a in s.actions]
    rules = [_rule_out(r) for r in s.rules]
    events = [_event_out(e) for e in s.events]
    workflows = [_workflow_out(w) for w in s.workflows]
    return ScenarioDetail(
        **base.model_dump(),
        entities=entities,
        relations=relations,
        data_sources=ds_out,
        instances=instances,
        relation_instances=rel_instances,
        mappings=mappings,
        actions=actions,
        rules=rules,
        events=events,
        workflows=workflows,
    )


@router.get("/{scenario_id}/graph")
def scenario_graph(scenario_id: str, mode: str = "schema", db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id)
    return ontology_service.build_graph(s, mode=mode)


@router.get("/{scenario_id}/objects", response_model=ObjectSearchOut)
def search_objects(
    scenario_id: str,
    q: str = Query(default="", max_length=200),
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """对象运行时搜索：只返回当前场景可见对象及安全来源摘要。"""
    _scenario_for_request(db, scenario_id)
    filters = [OntologyInstance.scenario_id == scenario_id]
    if entity_id:
        _entity_in_scenario(db, scenario_id, entity_id)
        filters.append(OntologyInstance.entity_id == entity_id)
    query = q.strip()
    if query:
        pattern = f"%{query}%"
        filters.append(
            or_(
                OntologyInstance.name.ilike(pattern),
                cast(OntologyInstance.attributes, String).ilike(pattern),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(OntologyInstance).where(*filters)) or 0)
    instances = db.execute(
        select(OntologyInstance)
        .options(joinedload(OntologyInstance.entity))
        .where(*filters)
        .order_by(OntologyInstance.created_at.desc(), OntologyInstance.name.asc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    instance_ids = [instance.id for instance in instances]
    mappings_by_entity: dict[str, DataMapping] = {}
    if instances:
        mappings_by_entity = {
            mapping.entity_id: mapping
            for mapping in db.execute(
                select(DataMapping)
                .where(
                    DataMapping.scenario_id == scenario_id,
                    DataMapping.entity_id.in_({instance.entity_id for instance in instances}),
                )
            ).scalars().all()
        }
    relation_ids_by_instance: dict[str, set[str]] = {instance_id: set() for instance_id in instance_ids}
    if instance_ids:
        relation_rows = db.execute(
            select(
                RelationInstance.id,
                RelationInstance.source_instance_id,
                RelationInstance.target_instance_id,
            ).where(
                or_(
                    RelationInstance.source_instance_id.in_(instance_ids),
                    RelationInstance.target_instance_id.in_(instance_ids),
                )
            )
        ).all()
        for relation_id, source_instance_id, target_instance_id in relation_rows:
            if source_instance_id in relation_ids_by_instance:
                relation_ids_by_instance[source_instance_id].add(relation_id)
            if target_instance_id in relation_ids_by_instance:
                relation_ids_by_instance[target_instance_id].add(relation_id)
    return ObjectSearchOut(
        items=[
            _object_item_out(
                db,
                instance,
                mapping=mappings_by_entity.get(instance.entity_id),
                relation_count=len(relation_ids_by_instance.get(instance.id, set())),
            )
            for instance in instances
        ],
        total=total,
        limit=limit,
        offset=offset,
        query=query,
        entity_id=entity_id,
    )


@router.get("/{scenario_id}/objects/{object_id}", response_model=ObjectDetailOut)
def get_object(scenario_id: str, object_id: str, db: Session = Depends(get_db)):
    """返回对象属性、邻接关系和来源追踪信息。"""
    _scenario_for_request(db, scenario_id)
    instance = db.get(OntologyInstance, object_id)
    if not instance or instance.scenario_id != scenario_id:
        raise HTTPException(404, "对象不存在")
    return _object_detail_out(db, instance)


@router.put("/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: str, payload: ScenarioIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    for k, v in payload.model_dump().items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return _scenario_out(s)


@router.delete("/{scenario_id}", response_model=Msg)
def delete_scenario(scenario_id: str, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    db.delete(s)
    db.commit()
    return Msg(message="已删除")


# ── 实体 ──────────────────────────────────────
@router.post("/{scenario_id}/entities", response_model=EntityOut)
def create_entity(scenario_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    e = OntologyEntity(scenario_id=scenario_id, **{k: v for k, v in payload.model_dump().items() if k != "properties"})
    db.add(e)
    db.flush()
    for p in payload.properties:
        db.add(OntologyProperty(entity_id=e.id, **p.model_dump()))
    db.commit()
    db.refresh(e)
    return _entity_out(e)


@router.put("/entities/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    e = db.get(OntologyEntity, entity_id)
    if not e:
        raise HTTPException(404, "实体不存在")
    _scenario_for_request(db, e.scenario_id, writable=True)
    for k in ("name", "description", "icon", "color", "is_abstract"):
        setattr(e, k, getattr(payload, k))
    # 重建属性
    for p in list(e.properties):
        db.delete(p)
    db.flush()
    for p in payload.properties:
        db.add(OntologyProperty(entity_id=e.id, **p.model_dump()))
    db.commit()
    db.refresh(e)
    return _entity_out(e)


@router.delete("/entities/{entity_id}", response_model=Msg)
def delete_entity(entity_id: str, db: Session = Depends(get_db)):
    e = db.get(OntologyEntity, entity_id)
    if not e:
        raise HTTPException(404, "实体不存在")
    _scenario_for_request(db, e.scenario_id, writable=True)
    # 删除关联关系（含关系实例）
    for r in list(e.scenario.relations):
        if r.source_entity_id == entity_id or r.target_entity_id == entity_id:
            for ri in list(r.relation_instances):
                db.delete(ri)
            db.delete(r)
    # 删除实例（含其关系实例）与数据映射
    for i in list(e.instances):
        for ri in list(i.source_instances) + list(i.target_instances):
            db.delete(ri)
        db.delete(i)
    for m in list(e.data_mappings):
        db.delete(m)
    db.delete(e)
    db.commit()
    return Msg(message="已删除")


# ── 关系 ──────────────────────────────────────
@router.post("/{scenario_id}/relations", response_model=RelationOut)
def create_relation(scenario_id: str, payload: RelationIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _entity_in_scenario(db, scenario_id, payload.source_entity_id)
    _entity_in_scenario(db, scenario_id, payload.target_entity_id)
    r = OntologyRelation(scenario_id=scenario_id, **payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return _relation_out(r, s.entities)


@router.put("/relations/{relation_id}", response_model=RelationOut)
def update_relation(relation_id: str, payload: RelationIn, db: Session = Depends(get_db)):
    r = db.get(OntologyRelation, relation_id)
    if not r:
        raise HTTPException(404, "关系不存在")
    _scenario_for_request(db, r.scenario_id, writable=True)
    _entity_in_scenario(db, r.scenario_id, payload.source_entity_id)
    _entity_in_scenario(db, r.scenario_id, payload.target_entity_id)
    for k, v in payload.model_dump().items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    s = db.get(BusinessScenario, r.scenario_id)
    return _relation_out(r, s.entities if s else [])


@router.delete("/relations/{relation_id}", response_model=Msg)
def delete_relation(relation_id: str, db: Session = Depends(get_db)):
    r = db.get(OntologyRelation, relation_id)
    if not r:
        raise HTTPException(404, "关系不存在")
    _scenario_for_request(db, r.scenario_id, writable=True)
    # 级联删除关系实例
    for ri in list(r.relation_instances):
        db.delete(ri)
    db.delete(r)
    db.commit()
    return Msg(message="已删除")


# ── AI 生成本体 ───────────────────────────────
@router.post("/{scenario_id}/generate-ontology")
def generate_ontology(scenario_id: str, payload: dict, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    description = (payload.get("description") or "").strip() or s.description or s.name
    try:
        return ontology_service.generate_ontology(db, s, description)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"AI 生成失败: {exc}")


@router.post("/{scenario_id}/apply-ontology", response_model=Msg)
def apply_ontology(scenario_id: str, payload: dict, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    ontology_service.apply_generated_ontology(db, s, payload)
    return Msg(message="已应用")


# ── 实例 ──────────────────────────────────────
@router.post("/{scenario_id}/instances", response_model=InstanceOut)
def create_instance(scenario_id: str, payload: InstanceIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _entity_in_scenario(db, scenario_id, payload.entity_id)
    i = OntologyInstance(scenario_id=scenario_id, **payload.model_dump())
    db.add(i)
    db.commit()
    db.refresh(i)
    return _instance_out(i)


@router.put("/instances/{instance_id}", response_model=InstanceOut)
def update_instance(instance_id: str, payload: InstanceIn, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i:
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    _entity_in_scenario(db, i.scenario_id, payload.entity_id)
    for k in ("entity_id", "name", "attributes", "source", "source_ref"):
        setattr(i, k, getattr(payload, k))
    db.commit()
    db.refresh(i)
    return _instance_out(i)


@router.delete("/instances/{instance_id}", response_model=Msg)
def delete_instance(instance_id: str, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i:
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    for ri in list(i.source_instances) + list(i.target_instances):
        db.delete(ri)
    db.delete(i)
    db.commit()
    return Msg(message="已删除")


# ── 关系实例 ──────────────────────────────────
@router.post("/{scenario_id}/relation-instances", response_model=RelationInstanceOut)
def create_relation_instance(scenario_id: str, payload: RelationInstanceIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    relation = db.get(OntologyRelation, payload.relation_id)
    source = db.get(OntologyInstance, payload.source_instance_id)
    target = db.get(OntologyInstance, payload.target_instance_id)
    if not relation or relation.scenario_id != scenario_id:
        raise HTTPException(400, "关系不属于当前业务场景")
    if not source or not target or source.scenario_id != scenario_id or target.scenario_id != scenario_id:
        raise HTTPException(400, "关系两端实例不属于当前业务场景")
    if source.entity_id != relation.source_entity_id or target.entity_id != relation.target_entity_id:
        raise HTTPException(400, "关系实例两端实体与关系定义不匹配")
    ri = RelationInstance(scenario_id=scenario_id, **payload.model_dump())
    db.add(ri)
    db.commit()
    db.refresh(ri)
    return _rel_instance_out(ri)


@router.delete("/relation-instances/{ri_id}", response_model=Msg)
def delete_relation_instance(ri_id: str, db: Session = Depends(get_db)):
    ri = db.get(RelationInstance, ri_id)
    if not ri:
        raise HTTPException(404, "关系实例不存在")
    _scenario_for_request(db, ri.scenario_id, writable=True)
    db.delete(ri)
    db.commit()
    return Msg(message="已删除")


# ── 数据映射 ──────────────────────────────────
@router.post("/{scenario_id}/mappings", response_model=DataMappingOut)
def create_mapping(scenario_id: str, payload: DataMappingIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _entity_in_scenario(db, scenario_id, payload.entity_id)
    _source_in_scenario(db, scenario_id, payload.data_source_id)
    # 同一实体只保留一条映射
    old = db.execute(
        select(DataMapping).where(
            DataMapping.scenario_id == scenario_id, DataMapping.entity_id == payload.entity_id
        )
    ).scalars().all()
    for o in old:
        db.delete(o)
    m = DataMapping(scenario_id=scenario_id, **payload.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _mapping_out(m)


@router.delete("/mappings/{mapping_id}", response_model=Msg)
def delete_mapping(mapping_id: str, db: Session = Depends(get_db)):
    m = db.get(DataMapping, mapping_id)
    if not m:
        raise HTTPException(404, "映射不存在")
    _scenario_for_request(db, m.scenario_id, writable=True)
    db.delete(m)
    db.commit()
    return Msg(message="已删除")


@router.post("/mappings/{mapping_id}/preview", response_model=DataMappingPreviewOut)
def preview_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    mapping = _mapping_for_request(db, mapping_id)
    limit = _mapping_limit(payload, 20, 100)
    try:
        scenario = _scenario_for_request(db, mapping.scenario_id)
        return ontology_service.preview_mapping(db, scenario, mapping, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"映射预览失败: {exc}")


@router.post("/mappings/{mapping_id}/test", response_model=DataMappingTestOut)
def test_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    mapping = _mapping_for_request(db, mapping_id)
    checked_at = datetime.now(timezone.utc)
    scenario = _scenario_for_request(db, mapping.scenario_id)
    try:
        preview = ontology_service.preview_mapping(db, scenario, mapping, limit=_mapping_limit(payload, 20, 100))
        mapping.status = "ready" if preview["ok"] else "error"
        mapping.last_error = "; ".join(preview.get("errors", []))
        mapping.last_checked_at = checked_at
        mapping.last_row_count = int(preview.get("row_count", 0))
        db.commit()
        return DataMappingTestOut(**preview, status=mapping.status, checked_at=checked_at)
    except Exception as exc:  # noqa: BLE001
        mapping.status = "error"
        mapping.last_error = str(exc)
        mapping.last_checked_at = checked_at
        db.commit()
        ds = mapping.data_source
        ent = mapping.entity
        return DataMappingTestOut(
            mapping_id=mapping.id,
            entity_name=ent.name if ent else "",
            data_source_name=ds.name if ds else "",
            table_name=mapping.table_name,
            ok=False,
            message=f"映射测试失败: {exc}",
            errors=[str(exc)],
            status="error",
            checked_at=checked_at,
        )


@router.post("/mappings/{mapping_id}/refresh", response_model=DataMappingRefreshOut)
def refresh_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    mapping = _mapping_for_request(db, mapping_id, writable=True)
    refreshed_at = datetime.now(timezone.utc)
    scenario = _scenario_for_request(db, mapping.scenario_id)
    limit = _mapping_limit(payload, 50, 500)
    try:
        result = ontology_service.import_instances_from_mapping(db, scenario, mapping, limit=limit)
        mapping.status = "ok"
        mapping.last_error = ""
        mapping.last_checked_at = refreshed_at
        mapping.last_refreshed_at = refreshed_at
        mapping.last_row_count = int(result.get("rows_scanned", 0))
        mapping.last_imported_count = int(result.get("instances_created", 0))
        db.commit()
        return DataMappingRefreshOut(
            mapping_id=mapping.id,
            ok=True,
            status=mapping.status,
            message="映射刷新完成",
            rows_scanned=int(result.get("rows_scanned", 0)),
            instances_created=int(result.get("instances_created", 0)),
            relations_created=int(result.get("relations_created", 0)),
            last_refreshed_at=refreshed_at,
        )
    except Exception as exc:  # noqa: BLE001
        mapping.status = "error"
        mapping.last_error = str(exc)
        mapping.last_checked_at = refreshed_at
        db.commit()
        return DataMappingRefreshOut(
            mapping_id=mapping.id,
            ok=False,
            status="error",
            message=f"映射刷新失败: {exc}",
            last_error=str(exc),
        )


@router.post("/mappings/{mapping_id}/import")
def import_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    """兼容旧客户端：旧的“导入实例”接口复用新的刷新状态闭环。"""
    return refresh_mapping(mapping_id, payload, db).model_dump()


# ── 操作（Actions）────────────────────────────
@router.post("/{scenario_id}/actions", response_model=ActionOut)
def create_action(scenario_id: str, payload: ActionIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _entity_in_scenario(db, scenario_id, payload.entity_id)
    _validate_action_executor(db, scenario_id, payload)
    a = OntologyAction(scenario_id=scenario_id, **payload.model_dump())
    db.add(a)
    db.commit()
    db.refresh(a)
    return _action_out(a)


@router.put("/actions/{action_id}", response_model=ActionOut)
def update_action(action_id: str, payload: ActionIn, db: Session = Depends(get_db)):
    a = db.get(OntologyAction, action_id)
    if not a:
        raise HTTPException(404, "操作不存在")
    _scenario_for_request(db, a.scenario_id, writable=True)
    _entity_in_scenario(db, a.scenario_id, payload.entity_id)
    _validate_action_executor(db, a.scenario_id, payload)
    for k, v in payload.model_dump().items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return _action_out(a)


@router.delete("/actions/{action_id}", response_model=Msg)
def delete_action(action_id: str, db: Session = Depends(get_db)):
    a = db.get(OntologyAction, action_id)
    if not a:
        raise HTTPException(404, "操作不存在")
    _scenario_for_request(db, a.scenario_id, writable=True)
    db.delete(a)
    db.commit()
    return Msg(message="已删除")


@router.post("/actions/{action_id}/execute")
def execute_action(action_id: str, payload: ActionExecuteRequest, db: Session = Depends(get_db)):
    a = db.get(OntologyAction, action_id)
    if not a:
        raise HTTPException(404, "操作不存在")
    # 执行属于写入边界：公共场景可以查看/预演，但只有场景所属租户可确认执行。
    _scenario_for_request(db, a.scenario_id, writable=not payload.dry_run)
    try:
        return workflow_service.execute_action(
            db,
            a,
            payload.params,
            confirm=payload.confirm,
            dry_run=payload.dry_run,
            idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"执行失败: {exc}")


# ── 规则（Rules）──────────────────────────────
@router.post("/{scenario_id}/rules", response_model=RuleOut)
def create_rule(scenario_id: str, payload: RuleIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    if payload.entity_id:
        _entity_in_scenario(db, scenario_id, payload.entity_id)
    _validate_trigger_actions(db, scenario_id, payload.trigger_action_ids)
    r = OntologyRule(scenario_id=scenario_id, **payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return _rule_out(r)


@router.put("/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: str, payload: RuleIn, db: Session = Depends(get_db)):
    r = db.get(OntologyRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    _scenario_for_request(db, r.scenario_id, writable=True)
    if payload.entity_id:
        _entity_in_scenario(db, r.scenario_id, payload.entity_id)
    _validate_trigger_actions(db, r.scenario_id, payload.trigger_action_ids)
    for k, v in payload.model_dump().items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    return _rule_out(r)


@router.delete("/rules/{rule_id}", response_model=Msg)
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    r = db.get(OntologyRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    _scenario_for_request(db, r.scenario_id, writable=True)
    db.delete(r)
    db.commit()
    return Msg(message="已删除")


@router.post("/rules/{rule_id}/evaluate")
def evaluate_rule(rule_id: str, payload: dict, db: Session = Depends(get_db)):
    """对给定数据记录评估规则是否命中。payload: {record: {...}}"""
    r = db.get(OntologyRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    _scenario_for_request(db, r.scenario_id)
    record = (payload or {}).get("record", {})
    return workflow_service.evaluate_rule(r, record)


# ── 事件（Events）─────────────────────────────
@router.post("/{scenario_id}/events", response_model=EventOut)
def create_event(scenario_id: str, payload: EventIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    e = OntologyEvent(scenario_id=scenario_id, **payload.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return _event_out(e)


@router.put("/events/{event_id}", response_model=EventOut)
def update_event(event_id: str, payload: EventIn, db: Session = Depends(get_db)):
    e = db.get(OntologyEvent, event_id)
    if not e:
        raise HTTPException(404, "事件不存在")
    _scenario_for_request(db, e.scenario_id, writable=True)
    for k, v in payload.model_dump().items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return _event_out(e)


@router.delete("/events/{event_id}", response_model=Msg)
def delete_event(event_id: str, db: Session = Depends(get_db)):
    e = db.get(OntologyEvent, event_id)
    if not e:
        raise HTTPException(404, "事件不存在")
    _scenario_for_request(db, e.scenario_id, writable=True)
    db.delete(e)
    db.commit()
    return Msg(message="已删除")


# ── 工作流（Workflows）────────────────────────
@router.post("/{scenario_id}/workflows", response_model=WorkflowOut)
def create_workflow(scenario_id: str, payload: WorkflowIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _validate_workflow_refs(db, scenario_id, payload.steps, payload.nodes)
    w = OntologyWorkflow(scenario_id=scenario_id, **payload.model_dump())
    db.add(w)
    db.commit()
    db.refresh(w)
    return _workflow_out(w)


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: str, payload: WorkflowIn, db: Session = Depends(get_db)):
    w = db.get(OntologyWorkflow, workflow_id)
    if not w:
        raise HTTPException(404, "工作流不存在")
    _scenario_for_request(db, w.scenario_id, writable=True)
    _validate_workflow_refs(db, w.scenario_id, payload.steps, payload.nodes)
    for k, v in payload.model_dump().items():
        setattr(w, k, v)
    db.commit()
    db.refresh(w)
    return _workflow_out(w)


@router.delete("/workflows/{workflow_id}", response_model=Msg)
def delete_workflow(workflow_id: str, db: Session = Depends(get_db)):
    w = db.get(OntologyWorkflow, workflow_id)
    if not w:
        raise HTTPException(404, "工作流不存在")
    _scenario_for_request(db, w.scenario_id, writable=True)
    db.delete(w)
    db.commit()
    return Msg(message="已删除")


@router.post("/workflows/{workflow_id}/execute")
def execute_workflow(workflow_id: str, payload: WorkflowExecuteRequest, db: Session = Depends(get_db)):
    w = db.get(OntologyWorkflow, workflow_id)
    if not w:
        raise HTTPException(404, "工作流不存在")
    _scenario_for_request(db, w.scenario_id, writable=True)
    try:
        return workflow_service.execute_workflow(db, w, payload.params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"执行失败: {exc}")


@router.post("/{scenario_id}/workflows/generate")
def generate_workflow(scenario_id: str, payload: WorkflowGenerateRequest, db: Session = Depends(get_db)):
    """AI 生成可视化工作流草稿（DAG 节点+连线，不落库）。"""
    s = _scenario_for_request(db, scenario_id)
    try:
        return workflow_service.generate_workflow(db, s, payload.description)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"生成失败: {exc}")


# ── 执行日志 ──────────────────────────────────
@router.get("/{scenario_id}/execution-logs", response_model=list[ActionExecutionLogOut])
def list_execution_logs(scenario_id: str, limit: int = 50, db: Session = Depends(get_db)):
    _scenario_for_request(db, scenario_id)
    logs = db.execute(
        select(ActionExecutionLog)
        .where(ActionExecutionLog.scenario_id == scenario_id)
        .order_by(ActionExecutionLog.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        ActionExecutionLogOut(
            id=l.id,
            scenario_id=l.scenario_id,
            target_type=l.target_type,
            target_id=l.target_id,
            target_name=l.target_name,
            input_params=l.input_params or {},
            status=l.status,
            mode=l.mode or "execute",
            idempotency_key=l.idempotency_key,
            result=l.result or {},
            error=l.error,
            duration_ms=l.duration_ms,
            created_at=l.created_at,
        )
        for l in logs
    ]
