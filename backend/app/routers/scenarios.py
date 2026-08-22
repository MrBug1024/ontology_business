"""业务场景 & 本体建模路由。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ..config import get_settings
from ..database import get_db
from ..models import (
    ActionExecutionLog,
    AuthorizationGrant,
    BusinessScenario,
    DataMapping,
    DataMappingRefreshJob,
    DataSource,
    FunctionDefinition,
    MCPConfig,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    Skill,
    RelationInstance,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from ..schemas import (
    ActionExecuteRequest,
    ActionIn,
    ActionOut,
    ActionExecutionLogOut,
    DataMappingIn,
    DataMappingOut,
    DataMappingPreviewOut,
    DataMappingRefreshJobOut,
    DataMappingTestOut,
    EntityIn,
    EntityOut,
    EventIn,
    EventEnvelopeOut,
    EventPublishIn,
    EventOut,
    FunctionDefinitionIn,
    FunctionDefinitionOut,
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
    WorkflowRunCreateRequest,
    WorkflowRunOut,
)
from ..services import (
    ontology_service,
    connector_service,
    function_definition_service,
    mapping_refresh_service,
    operations_service,
    permission_service,
    release_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
    workflow_service,
)
from ..services.auth_service import get_current_user
from ..services.policies import PolicyViolation

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


def _mapping_identity_contract(
    entity: OntologyEntity,
    *,
    data_source_id: str,
    binding_key: str,
    binding_ref: dict,
    table_name: str,
    column_map: dict,
) -> dict:
    """Return the source-side identity boundary used by incremental imports.

    Instances are keyed by a mapping id and the mapped record key.  Editing
    display/value columns can therefore preserve the mapping id and update the
    same objects.  Changing the source, logical binding, table, or key column
    must instead create a new mapping identity so equal-looking record keys in
    a different source cannot overwrite prior imported facts.
    """
    key_property = next((property.name for property in entity.properties if property.is_key), "")
    return {
        "data_source_id": str(data_source_id or ""),
        "data_source_binding_key": str(binding_key or ""),
        "data_source_binding_ref": binding_ref or {},
        "table_name": str(table_name or ""),
        "key_column": str((column_map or {}).get(key_property) or ""),
    }


def _validate_action_executor(db: Session, scenario_id: str, payload: ActionIn) -> None:
    """校验操作执行器引用的资源边界，保持操作配置可移植且不跨场景。"""
    config = payload.executor_config or {}
    # These executors cross the platform boundary. Scenario editors may use
    # pre-approved Actions, but only a tenant manager may bind a new external
    # target or executable implementation to one.
    if payload.executor_type in {"skill", "mcp", "http", "script"}:
        permission_service.require_tenant_permission(db, "manage")
    if payload.executor_type == "sql" and config.get("data_source_id"):
        _source_in_scenario(db, scenario_id, config["data_source_id"])
    if payload.executor_type == "skill":
        try:
            workflow_service.validate_skill_action_config(db, config)
        except PolicyViolation as exc:
            raise HTTPException(400, f"Skill Action 配置无效: {exc}") from exc
    if payload.executor_type == "mcp" and config.get("mcp_id"):
        tenant_service.require_visible(db, MCPConfig, config["mcp_id"], "操作引用的 MCP 服务不存在")
    if payload.executor_type == "http":
        try:
            workflow_service.validate_http_action_config(config)
        except PolicyViolation as exc:
            raise HTTPException(400, f"HTTP Action 配置无效: {exc}") from exc
    if payload.executor_type == "script" and not get_settings().allow_unsafe_workflow_nodes:
        raise HTTPException(400, "脚本 Action 默认停用；请改用受治理的 Action 或工作流节点")


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


def _validate_workflow_trigger(
    db: Session,
    scenario_id: str,
    trigger_type: str,
    trigger_config: dict,
    *,
    steps: list | None = None,
    nodes: list | None = None,
    workflow_id: str | None = None,
) -> None:
    """把 P1 调度配置校验放在服务端，避免保存无法运行的工作流。"""
    try:
        operations_service.validate_trigger_config(trigger_type, trigger_config)
        operations_service.validate_approval_nodes(nodes, steps)
        operations_service.validate_event_feedback_loops(
            db,
            scenario_id,
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            nodes=nodes,
            steps=steps,
            workflow_id=workflow_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"工作流触发配置无效: {exc}") from exc


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
    scenario = tenant_service.require_scenario(db, scenario_id, writable=writable)
    permission_service.require_scenario_permission(
        db,
        scenario,
        "write" if writable else "read",
    )
    return scenario


def _require_restricted_scope_management(db: Session, access_scope: str) -> None:
    """受限资源会改变 ACL 语义，只允许组织管理者创建或调整该标记。"""
    if access_scope != "tenant":
        permission_service.require_tenant_permission(db, "manage")


def _require_sensitive_property_management(db: Session, properties: list[PropertyIn]) -> None:
    if any(property_payload.is_sensitive for property_payload in properties):
        permission_service.require_tenant_permission(db, "manage")


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
        access_scope=a.access_scope or "tenant",
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
        status=w.status or ("active" if w.enabled else "disabled"),
        enabled=w.enabled,
        access_scope=w.access_scope or "tenant",
        created_at=w.created_at,
    )


def _workflow_run_out(db: Session, run: WorkflowRun) -> WorkflowRunOut:
    pending = db.execute(
        select(WorkflowApprovalRequest.id).where(
            WorkflowApprovalRequest.workflow_run_id == run.id,
            WorkflowApprovalRequest.status == "pending",
        ).limit(1)
    ).scalar_one_or_none()
    try:
        definition = runtime_definition_service.resolve_for_run(db, run)
        workflow = runtime_definition_service.resolve_resource(
            definition, "workflow", run.workflow_id
        )
    except runtime_definition_service.RuntimeDefinitionError:
        workflow = None
    return WorkflowRunOut(
        id=run.id,
        scenario_id=run.scenario_id,
        workflow_id=run.workflow_id,
        workflow_name=workflow.name if workflow else "",
        trigger_source=run.trigger_source,
        environment=run.environment or "dev",
        definition_snapshot_id=run.definition_snapshot_id,
        release_id=run.release_id,
        definition_hash=run.definition_hash or "",
        definition_source=run.definition_source or "live",
        status=run.status,
        input_params=run.input_params or {},
        attempt=run.attempt,
        max_attempts=run.max_attempts,
        timeout_seconds=run.timeout_seconds,
        available_at=run.available_at,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        completed_at=run.completed_at,
        next_retry_at=run.next_retry_at,
        error=run.error or "",
        result=run.result or {},
        pending_approval=bool(pending),
        can_execute=bool(workflow and permission_service.check_workflow(db, workflow, "execute").allowed),
        can_approve=bool(workflow and permission_service.check_workflow(db, workflow, "approve").allowed),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _entity_out(db: Session, e: OntologyEntity) -> EntityOut:
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
                is_sensitive=bool(p.is_sensitive),
            )
            for p in e.properties
            if permission_service.can_read_property(db, p)
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
    # A tenant-visible row is not necessarily ACL-visible: an explicit scenario
    # deny must also remove it from navigation/list responses, otherwise its
    # name and counts become an information disclosure even when detail routes
    # correctly return 403.
    return [
        _scenario_out(s)
        for s in db.execute(
            select(BusinessScenario).where(tenant_service.visible_clause(BusinessScenario, db))
        ).scalars().all()
        if permission_service.check_scenario(db, s, "read").allowed
    ]


@router.post("", response_model=ScenarioOut)
def create_scenario(payload: ScenarioIn, db: Session = Depends(get_db)):
    permission_service.require_tenant_permission(db, "write")
    s = BusinessScenario(
        tenant_id=tenant_service.current_tenant_id(db),
        **payload.model_dump(),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _scenario_out(s)


def _instance_out(db: Session, i: OntologyInstance) -> InstanceOut:
    ent = i.entity
    can_read = permission_service.check_object(db, i, "read").allowed
    return InstanceOut(
        id=i.id,
        scenario_id=i.scenario_id,
        entity_id=i.entity_id,
        name=i.name,
        attributes=permission_service.filter_instance_attributes(db, i) if can_read else {},
        source=i.source,
        source_ref=i.source_ref,
        access_scope=i.access_scope or "tenant",
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
    runtime_state = mapping_refresh_service.mapping_runtime_state(m)
    return DataMappingOut(
        id=m.id,
        scenario_id=m.scenario_id,
        entity_id=m.entity_id,
        data_source_id=m.data_source_id,
        data_source_binding_key=m.data_source_binding_key or "",
        data_source_binding_ref=m.data_source_binding_ref or {},
        table_name=m.table_name,
        column_map=m.column_map or {},
        entity_name=ent.name if ent else "",
        data_source_name=ds.name if ds else "",
        data_source_type=ds.type if ds else "",
        status=runtime_state["status"],
        last_error=runtime_state["last_error"],
        last_checked_at=runtime_state["last_checked_at"],
        last_refreshed_at=runtime_state["last_refreshed_at"],
        last_row_count=runtime_state["last_row_count"],
        last_imported_count=runtime_state["last_imported_count"],
        created_at=m.created_at,
    )


def _function_out(function: FunctionDefinition) -> FunctionDefinitionOut:
    """Return the typed contract and safe built-in runtime descriptor."""
    return FunctionDefinitionOut(
        id=function.id,
        scenario_id=function.scenario_id,
        name=function.name,
        description=function.description or "",
        input_schema=function.input_schema or {},
        output_schema=function.output_schema or {},
        tags=function.tags or [],
        visibility=function.visibility or "scenario",
        runtime_kind=function.runtime_kind or "contract",
        runtime_config=function.runtime_config or {},
        created_at=function.created_at,
        updated_at=function.updated_at,
    )


def _mapping_refresh_job_out(job: DataMappingRefreshJob) -> DataMappingRefreshJobOut:
    return DataMappingRefreshJobOut(
        id=job.id,
        mapping_id=job.mapping_id,
        scenario_id=job.scenario_id,
        environment=job.environment,
        status=job.status,
        limit=job.limit,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        timeout_seconds=job.timeout_seconds,
        available_at=job.available_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        next_retry_at=job.next_retry_at,
        rows_scanned=job.rows_scanned,
        instances_created=job.instances_created,
        instances_updated=job.instances_updated,
        relations_created=job.relations_created,
        connector_audit=job.connector_audit or [],
        definition_snapshot_id=job.definition_snapshot_id,
        release_id=job.release_id,
        definition_hash=job.definition_hash or "",
        definition_source=job.definition_source or "live",
        error=connector_service.sanitize_message(job.error or ""),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _object_provenance(
    db: Session,
    instance: OntologyInstance,
    mapping: DataMapping | None = None,
) -> ObjectProvenanceOut:
    """把对象的导入来源解析成安全的、可展示的来源摘要。"""
    metadata = instance.source_metadata if isinstance(instance.source_metadata, dict) else {}
    if mapping is None:
        mapping_id = str(metadata.get("mapping_id") or "").strip()
        candidate = db.get(DataMapping, mapping_id) if mapping_id else None
        if candidate and candidate.scenario_id == instance.scenario_id:
            mapping = candidate
        else:
            # Legacy/manual objects may not carry a mapping id.  Keep the old
            # entity-level fallback only for those records; a historical object
            # from a replaced mapping must not be mislabeled as the new table.
            mapping = db.execute(
                select(DataMapping)
                .where(
                    DataMapping.scenario_id == instance.scenario_id,
                    DataMapping.entity_id == instance.entity_id,
                )
                .limit(1)
            ).scalar_one_or_none() if not mapping_id else None
    source = mapping.data_source if mapping else None
    if source is None:
        source_id = str(metadata.get("data_source_id") or "").strip()
        candidate_source = db.get(DataSource, source_id) if source_id else None
        if candidate_source and candidate_source.scenario_id in (None, instance.scenario_id):
            source = candidate_source
    return ObjectProvenanceOut(
        kind=instance.source or "manual",
        reference=instance.source_ref or "",
        mapping_id=mapping.id if mapping else (str(metadata.get("mapping_id") or "") or None),
        data_source_id=source.id if source else (str(metadata.get("data_source_id") or "") or None),
        data_source_name=source.name if source else "",
        table_name=mapping.table_name if mapping else str(metadata.get("table_name") or ""),
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
        attributes=permission_service.filter_instance_attributes(db, instance),
        source=instance.source or "manual",
        source_ref=instance.source_ref or "",
        access_scope=instance.access_scope or "tenant",
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
        if not related or not permission_service.check_object(db, related, "read").allowed:
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
    entities = [_entity_out(db, e) for e in s.entities]
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
    visible_instances = [
        instance
        for instance in s.instances
        if permission_service.check_object(db, instance, "read").allowed
    ]
    visible_instance_ids = {instance.id for instance in visible_instances}
    instances = [_instance_out(db, instance) for instance in visible_instances]
    rel_instances = [
        _rel_instance_out(ri)
        for ri in s.relation_instances
        if ri.source_instance_id in visible_instance_ids
        and ri.target_instance_id in visible_instance_ids
    ]
    mappings = [_mapping_out(m) for m in s.data_mappings]
    functions = [_function_out(function) for function in s.function_definitions]
    actions = [
        _action_out(action)
        for action in s.actions
        if permission_service.check_action(db, action, "read").allowed
    ]
    rules = [_rule_out(r) for r in s.rules]
    events = [_event_out(e) for e in s.events]
    workflows = [
        _workflow_out(workflow)
        for workflow in s.workflows
        if permission_service.check_workflow(db, workflow, "read").allowed
    ]
    return ScenarioDetail(
        **base.model_dump(),
        can_write=permission_service.check_scenario(db, s, "write").allowed,
        entities=entities,
        relations=relations,
        data_sources=ds_out,
        instances=instances,
        relation_instances=rel_instances,
        mappings=mappings,
        functions=functions,
        actions=actions,
        rules=rules,
        events=events,
        workflows=workflows,
    )


@router.get("/{scenario_id}/graph")
def scenario_graph(scenario_id: str, mode: str = "schema", db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id)
    return ontology_service.build_graph(s, mode=mode, db=db)


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
    query = q.strip().lower()
    candidates = db.execute(
        select(OntologyInstance)
        .options(joinedload(OntologyInstance.entity))
        .where(*filters)
        .order_by(OntologyInstance.created_at.desc(), OntologyInstance.name.asc())
    ).scalars().all()
    # 先做对象 ACL 与属性脱敏，再用安全的字段做查询；否则敏感值即使不返回也会被
    # 搜索命中侧信道泄露。对象查询的分页也必须在 ACL 过滤之后计算总数。
    visible_candidates = [
        instance
        for instance in candidates
        if permission_service.check_object(db, instance, "read").allowed
    ]
    if query:
        visible_candidates = [
            instance
            for instance in visible_candidates
            if query in instance.name.lower()
            or query in str(permission_service.filter_instance_attributes(db, instance)).lower()
        ]
    total = len(visible_candidates)
    instances = visible_candidates[offset : offset + limit]
    instance_ids = [instance.id for instance in instances]
    visible_instance_ids = {instance.id for instance in visible_candidates}
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
            if (
                source_instance_id in relation_ids_by_instance
                and target_instance_id in visible_instance_ids
            ):
                relation_ids_by_instance[source_instance_id].add(relation_id)
            if (
                target_instance_id in relation_ids_by_instance
                and source_instance_id in visible_instance_ids
            ):
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
    permission_service.require_object_permission(db, instance, "read")
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
    try:
        release_service.assert_scenario_deletion_allowed(db, s)
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(s)
    db.commit()
    return Msg(message="已删除")


# ── 实体 ──────────────────────────────────────
@router.post("/{scenario_id}/entities", response_model=EntityOut)
def create_entity(scenario_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _require_sensitive_property_management(db, payload.properties)
    e = OntologyEntity(scenario_id=scenario_id, **{k: v for k, v in payload.model_dump().items() if k != "properties"})
    db.add(e)
    db.flush()
    for p in payload.properties:
        db.add(OntologyProperty(entity_id=e.id, **p.model_dump()))
    db.commit()
    db.refresh(e)
    return _entity_out(db, e)


@router.put("/entities/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    e = db.get(OntologyEntity, entity_id)
    if not e:
        raise HTTPException(404, "实体不存在")
    _scenario_for_request(db, e.scenario_id, writable=True)
    _require_sensitive_property_management(db, payload.properties)
    for k in ("name", "description", "icon", "color", "is_abstract"):
        setattr(e, k, getattr(payload, k))
    # 按名称原位更新属性，避免属性级 ACL 因编辑实体描述而丢失稳定 resource_id。
    remaining_by_name: dict[str, list[OntologyProperty]] = {}
    for prop in e.properties:
        remaining_by_name.setdefault(prop.name, []).append(prop)
    for property_payload in payload.properties:
        candidates = remaining_by_name.get(property_payload.name, [])
        existing = candidates.pop(0) if candidates else None
        if existing:
            for key, value in property_payload.model_dump().items():
                setattr(existing, key, value)
        else:
            db.add(OntologyProperty(entity_id=e.id, **property_payload.model_dump()))
    for obsolete in [prop for items in remaining_by_name.values() for prop in items]:
        for grant in db.execute(
            select(AuthorizationGrant).where(
                AuthorizationGrant.resource_type == "property",
                AuthorizationGrant.resource_id == obsolete.id,
            )
        ).scalars().all():
            db.delete(grant)
        db.delete(obsolete)
    db.commit()
    db.refresh(e)
    return _entity_out(db, e)


@router.delete("/entities/{entity_id}", response_model=Msg)
def delete_entity(entity_id: str, db: Session = Depends(get_db)):
    e = db.get(OntologyEntity, entity_id)
    if not e:
        raise HTTPException(404, "实体不存在")
    scenario = _scenario_for_request(db, e.scenario_id, writable=True)
    # Guard before touching relation instances, mappings, or the entity's
    # cascades; a rejected delete must have no partial operational side effect.
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="entity",
            resource_id=e.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    scenario = _scenario_for_request(db, r.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="relation",
            resource_id=r.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    _require_restricted_scope_management(db, payload.access_scope)
    entity = _entity_in_scenario(db, scenario_id, payload.entity_id)
    if entity:
        permission_service.require_instance_attribute_write_permissions(
            db, entity, payload.attributes
        )
    i = OntologyInstance(scenario_id=scenario_id, **payload.model_dump())
    db.add(i)
    db.commit()
    db.refresh(i)
    return _instance_out(db, i)


@router.put("/instances/{instance_id}", response_model=InstanceOut)
def update_instance(instance_id: str, payload: InstanceIn, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i:
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    permission_service.require_object_permission(db, i, "write")
    _require_restricted_scope_management(db, payload.access_scope)
    entity = _entity_in_scenario(db, i.scenario_id, payload.entity_id)
    if entity:
        permission_service.require_instance_attribute_write_permissions(
            db, entity, payload.attributes
        )
    for k in ("entity_id", "name", "attributes", "source", "source_ref", "access_scope"):
        setattr(i, k, getattr(payload, k))
    db.commit()
    db.refresh(i)
    return _instance_out(db, i)


@router.delete("/instances/{instance_id}", response_model=Msg)
def delete_instance(instance_id: str, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i:
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    permission_service.require_object_permission(db, i, "write")
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
    permission_service.require_object_permission(db, source, "write")
    permission_service.require_object_permission(db, target, "write")
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
    source = ri.source_instance
    target = ri.target_instance
    if source:
        permission_service.require_object_permission(db, source, "write")
    if target:
        permission_service.require_object_permission(db, target, "write")
    db.delete(ri)
    db.commit()
    return Msg(message="已删除")


# ── 数据映射 ──────────────────────────────────
@router.post("/{scenario_id}/mappings", response_model=DataMappingOut)
def create_mapping(scenario_id: str, payload: DataMappingIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    entity = _entity_in_scenario(db, scenario_id, payload.entity_id)
    assert entity is not None
    _source_in_scenario(db, scenario_id, payload.data_source_id)
    mapping_data = payload.model_dump()
    try:
        binding = connector_service.runtime_binding_from_config(mapping_data, "data_source")
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(400, f"映射运行时绑定配置无效: {exc}") from exc
    key_field, ref_field = connector_service.runtime_binding_fields("data_source")
    if binding is None:
        mapping_data[key_field] = ""
        mapping_data[ref_field] = {}
    else:
        # Only retain the compact adapter/capability descriptor.  Names,
        # endpoints and any credential-shaped values never become mapping state.
        mapping_data[key_field] = binding["binding_key"]
        mapping_data[ref_field] = binding["reference"]
    # 同一实体只保留一条映射。旧实现会删除后新建，这会改变 mapping_id；而已导入
    # 对象以 (mapping_id, environment, record_key) 保持幂等身份，因此一次无改动
    # 的“保存”也会在下次刷新时重复导入。对于同一来源/表/主键列，原地更新可保留
    # 稳定身份和对象血缘；身份边界变化时仍新建映射，避免相同 record_key 覆盖另一
    # 数据源或表中的历史对象。
    old = db.execute(
        select(DataMapping)
        .where(
            DataMapping.scenario_id == scenario_id,
            DataMapping.entity_id == payload.entity_id,
        )
        .order_by(DataMapping.created_at.asc(), DataMapping.id.asc())
    ).scalars().all()
    incoming_identity = _mapping_identity_contract(
        entity,
        data_source_id=mapping_data["data_source_id"],
        binding_key=mapping_data.get("data_source_binding_key", ""),
        binding_ref=mapping_data.get("data_source_binding_ref", {}),
        table_name=mapping_data.get("table_name", ""),
        column_map=mapping_data.get("column_map", {}),
    )
    if old:
        m = old[0]
        current_identity = _mapping_identity_contract(
            entity,
            data_source_id=m.data_source_id,
            binding_key=m.data_source_binding_key,
            binding_ref=m.data_source_binding_ref or {},
            table_name=m.table_name,
            column_map=m.column_map or {},
        )
        if current_identity == incoming_identity:
            before = mapping_refresh_service.mapping_fingerprint(m)
            for field, value in mapping_data.items():
                setattr(m, field, value)
            changed = mapping_refresh_service.mapping_fingerprint(m) != before
            if changed:
                mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                    db,
                    m.id,
                    reason="映射定义已更新，请重新提交刷新",
                )
                mapping_refresh_service.invalidate_mapping_runtime_state(m)
            # Older databases may have accumulated duplicate mappings before
            # the one-per-entity API contract existed.  Keep the oldest stable
            # mapping as the canonical identity and remove the extras as the
            # old code did.
            duplicates = old[1:]
        else:
            # The import identity boundary changed.  Preserve prior imported
            # objects as auditable historical facts and give the new source a
            # new mapping id; otherwise colliding record keys could overwrite
            # the old source's objects on the next refresh.
            duplicates = old
            m = DataMapping(scenario_id=scenario_id, **mapping_data)
            db.add(m)
        for duplicate in duplicates:
            mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                db,
                duplicate.id,
                reason="映射已被同实体的规范定义替换",
            )
            db.delete(duplicate)
    else:
        m = DataMapping(scenario_id=scenario_id, **mapping_data)
        db.add(m)
    db.commit()
    db.refresh(m)
    return _mapping_out(m)


@router.delete("/mappings/{mapping_id}", response_model=Msg)
def delete_mapping(mapping_id: str, db: Session = Depends(get_db)):
    m = db.get(DataMapping, mapping_id)
    if not m:
        raise HTTPException(404, "映射不存在")
    scenario = _scenario_for_request(db, m.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="mapping",
            resource_id=m.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    mapping_refresh_service.cancel_active_mapping_refresh_jobs(
        db,
        m.id,
        reason="映射已删除",
    )
    db.delete(m)
    db.commit()
    return Msg(message="已删除")


@router.post("/mappings/{mapping_id}/preview", response_model=DataMappingPreviewOut)
def preview_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    mapping = _mapping_for_request(db, mapping_id)
    limit = _mapping_limit(payload, 20, 100)
    try:
        scenario = _scenario_for_request(db, mapping.scenario_id)
        runtime_mapping, definition = mapping_refresh_service.resolve_mapping_runtime_definition(
            db,
            scenario,
            mapping,
        )
        source, _audit = mapping_refresh_service.resolve_mapping_data_source(
            db,
            scenario,
            runtime_mapping,
            release_id=definition.release_id if definition.is_frozen else None,
        )
        return ontology_service.preview_mapping(
            db,
            scenario,
            runtime_mapping,
            limit=limit,
            data_source=source,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400,
            f"映射预览失败: {connector_service.sanitize_message(exc)}",
        ) from exc


@router.post("/mappings/{mapping_id}/test", response_model=DataMappingTestOut)
def test_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    mapping = _mapping_for_request(db, mapping_id)
    checked_at = datetime.now(timezone.utc)
    scenario = _scenario_for_request(db, mapping.scenario_id)
    runtime_mapping: Any | None = None
    definition = None
    try:
        runtime_mapping, definition = mapping_refresh_service.resolve_mapping_runtime_definition(
            db,
            scenario,
            mapping,
        )
        source, _audit = mapping_refresh_service.resolve_mapping_data_source(
            db,
            scenario,
            runtime_mapping,
            release_id=definition.release_id if definition.is_frozen else None,
        )
        preview = ontology_service.preview_mapping(
            db,
            scenario,
            runtime_mapping,
            limit=_mapping_limit(payload, 20, 100),
            data_source=source,
        )
        status = "ready" if preview["ok"] else "error"
        if (
            definition is not None
            and (
                not definition.is_frozen
                or mapping_refresh_service.mapping_matches_snapshot(mapping, runtime_mapping)
            )
        ):
            mapping_refresh_service.set_mapping_runtime_state(
                mapping,
                status=status,
                error="; ".join(preview.get("errors", [])),
                checked_at=checked_at,
                rows_scanned=int(preview.get("row_count", 0)),
            )
        db.commit()
        return DataMappingTestOut(**preview, status=status, checked_at=checked_at)
    except Exception as exc:  # noqa: BLE001
        safe_error = connector_service.sanitize_message(exc)
        # A frozen staging/prod definition may no longer equal the current dev
        # mapping.  Do not paint that mutable row with an error from another
        # deployment's released config.
        if (
            definition is not None
            and (
                not definition.is_frozen
                or (
                    runtime_mapping is not None
                    and mapping_refresh_service.mapping_matches_snapshot(mapping, runtime_mapping)
                )
            )
        ):
            mapping_refresh_service.set_mapping_runtime_state(
                mapping,
                status="error",
                error=safe_error,
                checked_at=checked_at,
            )
        db.commit()
        ds = mapping.data_source
        ent = mapping.entity
        return DataMappingTestOut(
            mapping_id=mapping.id,
            entity_name=ent.name if ent else "",
            data_source_name=ds.name if ds else "",
            table_name=mapping.table_name,
            ok=False,
            message=f"映射测试失败: {safe_error}",
            errors=[safe_error],
            status="error",
            checked_at=checked_at,
        )


def _enqueue_mapping_refresh(
    mapping_id: str,
    payload: dict | None,
    db: Session,
) -> DataMappingRefreshJobOut:
    """Persist refresh intent only; the worker owns all external side effects."""
    mapping = _mapping_for_request(db, mapping_id, writable=True)
    try:
        job, _created = mapping_refresh_service.enqueue_mapping_refresh(
            db,
            mapping,
            limit=_mapping_limit(payload, 50, 500),
        )
        db.commit()
        db.refresh(job)
        return _mapping_refresh_job_out(job)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"映射刷新入队失败: {connector_service.sanitize_message(exc)}") from exc


@router.post(
    "/mappings/{mapping_id}/refresh-jobs",
    response_model=DataMappingRefreshJobOut,
    status_code=202,
)
def enqueue_mapping_refresh_job(
    mapping_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """快速入队受限批次刷新；外部查询与对象写入由可恢复 worker 完成。"""
    return _enqueue_mapping_refresh(mapping_id, payload, db)


@router.get("/mappings/refresh-jobs/{job_id}", response_model=DataMappingRefreshJobOut)
def get_mapping_refresh_job(
    job_id: str,
    response: Response,
    db: Session = Depends(get_db),
):
    job = db.get(DataMappingRefreshJob, job_id)
    if not job or job.tenant_id != tenant_service.current_tenant_id(db):
        raise HTTPException(404, "映射刷新任务不存在")
    if job.environment != runtime_connector_service.runtime_environment():
        # Shared metadata storage must not expose one deployment's task state
        # through another environment's API surface.
        raise HTTPException(404, "映射刷新任务不存在")
    _scenario_for_request(db, job.scenario_id)
    response.headers["Cache-Control"] = "no-store"
    return _mapping_refresh_job_out(job)


@router.post(
    "/mappings/{mapping_id}/refresh",
    response_model=DataMappingRefreshJobOut,
    status_code=202,
)
def refresh_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    """旧刷新入口的异步兼容别名，不在 HTTP 请求中访问外部数据源。"""
    return _enqueue_mapping_refresh(mapping_id, payload, db)


@router.post(
    "/mappings/{mapping_id}/import",
    response_model=DataMappingRefreshJobOut,
    status_code=202,
)
def import_mapping(mapping_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    """旧导入入口的异步兼容别名，不在 HTTP 请求中写入对象。"""
    return _enqueue_mapping_refresh(mapping_id, payload, db)


# ── 受治理函数（声明式契约，不执行代码）────────────────────────
@router.get("/{scenario_id}/functions", response_model=list[FunctionDefinitionOut])
def list_function_definitions(scenario_id: str, db: Session = Depends(get_db)):
    _scenario_for_request(db, scenario_id)
    return [
        _function_out(function)
        for function in db.execute(
            select(FunctionDefinition)
            .where(FunctionDefinition.scenario_id == scenario_id)
            .order_by(FunctionDefinition.name.asc(), FunctionDefinition.id.asc())
        ).scalars().all()
    ]


@router.post("/{scenario_id}/functions", response_model=FunctionDefinitionOut)
def create_function_definition(
    scenario_id: str,
    payload: FunctionDefinitionIn,
    db: Session = Depends(get_db),
):
    _scenario_for_request(db, scenario_id, writable=True)
    try:
        declaration = function_definition_service.normalize_definition(payload.model_dump())
    except function_definition_service.FunctionDefinitionError as exc:
        raise HTTPException(400, f"函数定义无效: {exc}") from exc
    function = FunctionDefinition(scenario_id=scenario_id, **declaration)
    db.add(function)
    db.commit()
    db.refresh(function)
    return _function_out(function)


@router.put("/functions/{function_id}", response_model=FunctionDefinitionOut)
def update_function_definition(
    function_id: str,
    payload: FunctionDefinitionIn,
    db: Session = Depends(get_db),
):
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(404, "函数定义不存在")
    _scenario_for_request(db, function.scenario_id, writable=True)
    try:
        declaration = function_definition_service.normalize_definition(payload.model_dump())
    except function_definition_service.FunctionDefinitionError as exc:
        raise HTTPException(400, f"函数定义无效: {exc}") from exc
    for key, value in declaration.items():
        setattr(function, key, value)
    db.commit()
    db.refresh(function)
    return _function_out(function)


@router.delete("/functions/{function_id}", response_model=Msg)
def delete_function_definition(function_id: str, db: Session = Depends(get_db)):
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(404, "函数定义不存在")
    scenario = _scenario_for_request(db, function.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="function",
            resource_id=function.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(function)
    db.commit()
    return Msg(message="已删除")


# ── 操作（Actions）────────────────────────────
@router.post("/{scenario_id}/actions", response_model=ActionOut)
def create_action(scenario_id: str, payload: ActionIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _require_restricted_scope_management(db, payload.access_scope)
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
    _require_restricted_scope_management(db, payload.access_scope)
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
    scenario = _scenario_for_request(db, a.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="action",
            resource_id=a.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(a)
    db.commit()
    return Msg(message="已删除")


@router.post("/actions/{action_id}/execute")
def execute_action(action_id: str, payload: ActionExecuteRequest, db: Session = Depends(get_db)):
    live_action = db.get(OntologyAction, action_id)
    if not live_action:
        raise HTTPException(404, "操作不存在")
    # 执行属于写入边界：公共场景可以查看/预演，但只有场景所属租户可确认执行。
    scenario = _scenario_for_request(
        db, live_action.scenario_id, writable=not payload.dry_run
    )
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        a = runtime_definition_service.resolve_resource(definition, "action", action_id)
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise HTTPException(409, f"当前部署定义不可执行该操作: {exc}") from exc
    permission_service.require_action_permission(
        db,
        a,
        "read" if payload.dry_run else "execute",
    )
    try:
        return workflow_service.execute_action(
            db,
            a,
            payload.params,
            confirm=payload.confirm,
            dry_run=payload.dry_run,
            idempotency_key=payload.idempotency_key,
            runtime_environment=definition.environment,
            runtime_definition=definition,
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
    scenario = _scenario_for_request(db, r.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="rule",
            resource_id=r.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    scenario = _scenario_for_request(db, e.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="event",
            resource_id=e.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(e)
    db.commit()
    return Msg(message="已删除")


@router.post("/events/{event_id}/publish", response_model=EventEnvelopeOut)
def publish_event(event_id: str, payload: EventPublishIn, db: Session = Depends(get_db)):
    """发布持久化业务事件，并把订阅该事件的启用工作流异步入队。"""
    live_event = db.get(OntologyEvent, event_id)
    if not live_event:
        raise HTTPException(404, "事件不存在")
    scenario = _scenario_for_request(db, live_event.scenario_id, writable=True)
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        event = runtime_definition_service.resolve_resource(definition, "event", event_id)
        envelope, queued_runs = operations_service.publish_event(
            db,
            event,
            payload.payload,
            source="manual",
            dedupe_key=payload.dedupe_key,
            created_by_user_id=str(db.info.get("user_id") or "") or None,
            runtime_definition=definition,
        )
        db.commit()
        db.refresh(envelope)
        return EventEnvelopeOut(
            id=envelope.id,
            scenario_id=envelope.scenario_id,
            event_id=envelope.event_id,
            name=envelope.name,
            payload=envelope.payload or {},
            source=envelope.source,
            source_run_id=envelope.source_run_id,
            environment=envelope.environment or "dev",
            definition_snapshot_id=envelope.definition_snapshot_id,
            release_id=envelope.release_id,
            definition_hash=envelope.definition_hash or "",
            definition_source=envelope.definition_source or "live",
            created_at=envelope.created_at,
            queued_workflow_run_ids=[run.id for run in queued_runs],
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"发布事件失败: {exc}") from exc


# ── 工作流（Workflows）────────────────────────
@router.post("/{scenario_id}/workflows", response_model=WorkflowOut)
def create_workflow(scenario_id: str, payload: WorkflowIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    _require_restricted_scope_management(db, payload.access_scope)
    _validate_workflow_refs(db, scenario_id, payload.steps, payload.nodes)
    _validate_workflow_trigger(
        db,
        scenario_id,
        payload.trigger_type,
        payload.trigger_config,
        steps=payload.steps,
        nodes=payload.nodes,
    )
    if payload.nodes:
        try:
            workflow_service.validate_workflow_definition(payload.nodes, payload.edges)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"工作流校验失败: {exc}") from exc
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
    try:
        operations_service.assert_workflow_mutable(db, w.id)
    except PolicyViolation as exc:
        raise HTTPException(409, str(exc)) from exc
    _require_restricted_scope_management(db, payload.access_scope)
    _validate_workflow_refs(db, w.scenario_id, payload.steps, payload.nodes)
    _validate_workflow_trigger(
        db,
        w.scenario_id,
        payload.trigger_type,
        payload.trigger_config,
        steps=payload.steps,
        nodes=payload.nodes,
        workflow_id=w.id,
    )
    if payload.nodes:
        try:
            workflow_service.validate_workflow_definition(payload.nodes, payload.edges)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"工作流校验失败: {exc}") from exc
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
    scenario = _scenario_for_request(db, w.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db,
            scenario,
            kind="workflow",
            resource_id=w.id,
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        operations_service.assert_workflow_mutable(db, w.id)
    except PolicyViolation as exc:
        raise HTTPException(409, str(exc)) from exc
    db.delete(w)
    db.commit()
    return Msg(message="已删除")


@router.post("/workflows/{workflow_id}/runs", response_model=WorkflowRunOut, status_code=202)
def create_workflow_run(
    workflow_id: str,
    payload: WorkflowRunCreateRequest,
    db: Session = Depends(get_db),
):
    """P1 异步入口：只提交任务，不在 HTTP 请求中等待工作流完成。"""
    live_workflow = db.get(OntologyWorkflow, workflow_id)
    if not live_workflow:
        raise HTTPException(404, "工作流不存在")
    scenario = _scenario_for_request(db, live_workflow.scenario_id, writable=True)
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        workflow = runtime_definition_service.resolve_resource(
            definition, "workflow", workflow_id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise HTTPException(409, f"当前部署定义不可执行该工作流: {exc}") from exc
    permission_service.require_workflow_permission(db, workflow, "execute")
    try:
        run, _ = operations_service.enqueue_workflow_run(
            db,
            workflow,
            payload.params,
            trigger_source="manual",
            created_by_user_id=str(db.info.get("user_id") or "") or None,
            runtime_definition=definition,
        )
        db.commit()
        db.refresh(run)
        return _workflow_run_out(db, run)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(409, f"提交任务失败: {exc}") from exc


@router.post(
    "/workflows/{workflow_id}/execute",
    response_model=WorkflowRunOut,
    status_code=202,
)
def execute_workflow(workflow_id: str, payload: WorkflowExecuteRequest, db: Session = Depends(get_db)):
    """兼容旧客户端的异步执行别名。

    P1 之后所有工作流都必须先持久化为 ``WorkflowRun``，才能由任务中心承接
    审批、重试和恢复。保留旧 URL 以避免客户端立即失效，但它不再在 HTTP 请求中
    同步执行任何节点。
    """
    return create_workflow_run(
        workflow_id,
        WorkflowRunCreateRequest(params=payload.params),
        db,
    )


@router.post("/{scenario_id}/workflows/generate")
def generate_workflow(scenario_id: str, payload: WorkflowGenerateRequest, db: Session = Depends(get_db)):
    """AI 生成可视化工作流草稿（DAG 节点+连线，不落库）。"""
    s = _scenario_for_request(db, scenario_id)
    try:
        return workflow_service.generate_workflow(db, s, payload.description)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"生成失败: {exc}")


# ── 执行日志 ──────────────────────────────────
def _can_read_execution_log(db: Session, log: ActionExecutionLog) -> bool:
    """Execution logs inherit the target Action/workflow's current read ACL.

    Logs contain parameters and external-result summaries.  A scenario-level read
    grant must never become a fallback path when the underlying target is denied,
    deleted, or an old unsupported target type.
    """
    if log.target_type == "action":
        action = db.get(OntologyAction, log.target_id)
        return bool(
            action
            and action.scenario_id == log.scenario_id
            and permission_service.check_action(db, action, "read").allowed
        )
    if log.target_type == "workflow":
        workflow = db.get(OntologyWorkflow, log.target_id)
        return bool(
            workflow
            and workflow.scenario_id == log.scenario_id
            and permission_service.check_workflow(db, workflow, "read").allowed
        )
    return False


@router.get("/{scenario_id}/execution-logs", response_model=list[ActionExecutionLogOut])
def list_execution_logs(
    scenario_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _scenario_for_request(db, scenario_id)
    logs = db.execute(
        select(ActionExecutionLog)
        .where(
            ActionExecutionLog.scenario_id == scenario_id,
            ActionExecutionLog.environment
            == runtime_connector_service.runtime_environment(),
        )
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
            environment=l.environment or "dev",
            definition_snapshot_id=l.definition_snapshot_id,
            release_id=l.release_id,
            definition_hash=l.definition_hash or "",
            definition_source=l.definition_source or "live",
            result=l.result or {},
            connector_audit=l.connector_audit or [],
            error=l.error,
            duration_ms=l.duration_ms,
            created_at=l.created_at,
        )
        for l in logs
        if _can_read_execution_log(db, l)
    ]
