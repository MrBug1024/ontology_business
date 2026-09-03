"""业务场景 & 本体建模路由。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import cast, delete, func, or_, select, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only

from ..config import get_settings
from ..database import get_db
from ..models import (
    ActionExecutionLog,
    Agent,
    AssistantAuditLog,
    AssistantCompilationJob,
    AssistantMessage,
    AssistantThread,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    Conversation,
    DataMapping,
    DataMappingRefreshJob,
    DataSource,
    FunctionDefinition,
    MCPConfig,
    Message,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyProposal,
    OntologyRelease,
    OntologyReview,
    OntologyRollback,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    RelationDataMapping,
    Skill,
    RelationInstance,
    ScenarioModelDraftResource,
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
    RelationDataMappingIn,
    RelationDataMappingOut,
    RelationDataMappingPreviewOut,
    RelationInstancePageOut,
    RuleIn,
    RuleOut,
    ScenarioDetail,
    ScenarioIn,
    ScenarioModelDraftResourceListOut,
    ScenarioModelDraftResourceOut,
    ScenarioModelDraftResourcePatch,
    ScenarioModelDraftResourceResolve,
    ScenarioOut,
    WorkflowExecuteRequest,
    WorkflowGenerateRequest,
    WorkflowIn,
    WorkflowOut,
    WorkflowRunCreateRequest,
    WorkflowRunOut,
)
from ..services import (
    agent_capability_service,
    connector_service,
    datasource_service,
    function_definition_service,
    mapping_refresh_service,
    ontology_service,
    object_deletion_service,
    object_storage_service,
    operations_service,
    permission_service,
    release_service,
    runtime_connector_service,
    runtime_definition_service,
    scenario_model_draft_service,
    template_artifact_service,
    template_catalog_service,
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

# Runtime facts are intentionally read through bounded endpoints.  A scenario
# detail request is a model/schema projection, not a request to serialize an
# entire production dataset.
INLINE_RUNTIME_FACT_LIMIT = 500
# Keep ACL checks bounded as well as the SQL result.  The cursor advances over
# the candidate window, so filtered rows are never materialized just to fill a
# cosmetic page size.
OBJECT_SEARCH_CANDIDATE_LIMIT = 100


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


def _lock_template_data_sources(
    db: Session, source_ids: list[str]
) -> dict[str, DataSource]:
    """Lock template-related sources in stable ID order and refresh ORM state."""
    ordered_ids = sorted({str(source_id) for source_id in source_ids if source_id})
    rows = db.scalars(
        select(DataSource)
        .where(DataSource.id.in_(ordered_ids))
        .order_by(DataSource.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    locked = {source.id: source for source in rows}
    if set(locked) != set(ordered_ids):
        raise HTTPException(409, "模板文件桶或附件目标在保存期间已删除，请刷新后重试")
    return locked


def _lock_action_for_update(
    db: Session, action_id: str, scenario_id: str
) -> OntologyAction:
    """Refresh A after the outer S lock so binding comparisons are current."""
    action = db.scalar(
        select(OntologyAction)
        .where(
            OntologyAction.id == action_id,
            OntologyAction.scenario_id == scenario_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if not action:
        raise HTTPException(409, "操作在更新期间已删除或变更场景，请刷新后重试")
    return action


def _mapping_for_request(db: Session, mapping_id: str, writable: bool = False) -> DataMapping:
    mapping = db.get(DataMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "映射不存在")
    _scenario_for_request(db, mapping.scenario_id, writable=writable)
    _source_in_scenario(db, mapping.scenario_id, mapping.data_source_id)
    return mapping


def _relation_mapping_for_request(
    db: Session, mapping_id: str, writable: bool = False
) -> RelationDataMapping:
    mapping = db.get(RelationDataMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "关系映射不存在")
    _scenario_for_request(db, mapping.scenario_id, writable=writable)
    _source_in_scenario(db, mapping.scenario_id, mapping.data_source_id)
    return mapping


def _invalidate_relation_mappings_for_object_mapping(
    db: Session,
    mapping_id: str,
    *,
    remove: bool,
) -> None:
    related = db.execute(
        select(RelationDataMapping).where(
            or_(
                RelationDataMapping.source_mapping_id == mapping_id,
                RelationDataMapping.target_mapping_id == mapping_id,
            )
        )
    ).scalars().all()
    for relation_mapping in related:
        _cancel_relation_mapping_endpoint_jobs(
            db,
            {
                relation_mapping.source_mapping_id,
                relation_mapping.target_mapping_id,
            },
            reason="关系映射端点定义已变化，请重新提交对象映射刷新",
        )
        ontology_service.purge_relation_mapping_instances(db, relation_mapping.id)
        if remove:
            db.delete(relation_mapping)
        else:
            relation_mapping.status = "unknown"
            relation_mapping.last_error = "关联的对象映射定义已变化，请重新预检并刷新"
            relation_mapping.last_refreshed_at = None
            relation_mapping.last_link_count = 0


def _cancel_relation_mapping_endpoint_jobs(
    db: Session, mapping_ids: set[str], *, reason: str
) -> None:
    for mapping_id in sorted(item for item in mapping_ids if item):
        mapping_refresh_service.cancel_active_mapping_refresh_jobs(
            db, mapping_id, reason=reason
        )


def _ensure_relation_mapping_dev_binding(
    db: Session,
    scenario: BusinessScenario,
    derived: dict[str, Any],
) -> None:
    """Materialise the server-derived relation connector contract for dev."""

    source = db.get(DataSource, str(derived.get("data_source_id") or ""))
    if (
        source is None
        or source.tenant_id != scenario.tenant_id
        or source.scenario_id not in (None, scenario.id)
    ):
        raise HTTPException(400, "关系映射的数据源不属于当前租户或业务场景")
    binding_key = str(derived.get("data_source_binding_key") or "")
    binding_ref = connector_service.with_required_capabilities(
        derived.get("data_source_binding_ref") or {}, "sql_read"
    )
    if not binding_key:
        raise HTTPException(400, "关系映射缺少可解析的运行时数据源绑定")
    derived["data_source_binding_ref"] = binding_ref

    if str(derived.get("mode") or "") in {"source_fk", "target_fk"}:
        carrier_id = (
            str(derived.get("source_mapping_id") or "")
            if derived["mode"] == "source_fk"
            else str(derived.get("target_mapping_id") or "")
        )
        carrier = db.get(DataMapping, carrier_id)
        if carrier is None or carrier.scenario_id != scenario.id:
            raise HTTPException(400, "关系映射外键承载侧对象映射不存在")
        if not carrier.data_source_binding_key:
            carrier.data_source_binding_key = binding_key
            carrier.data_source_binding_ref = binding_ref
            mapping_refresh_service.invalidate_mapping_runtime_state(carrier)
        elif (
            carrier.data_source_binding_key != binding_key
            or connector_service.with_required_capabilities(
                carrier.data_source_binding_ref or {}, "sql_read"
            )
            != binding_ref
        ):
            raise HTTPException(400, "关系映射绑定与外键承载侧对象映射不一致")

    binding = connector_service.upsert_binding(
        db,
        scenario,
        environment="dev",
        binding_key_value=binding_key,
        kind="data_source",
        connector_id=source.id,
        reference_label=f"关系映射 / {derived.get('table_name') or '未选择表'}",
        check=True,
        created_by_user_id=(str(db.info.get("user_id")) if db.info.get("user_id") else None),
    )
    try:
        connector_service.require_ready_binding(
            db,
            scenario,
            environment="dev",
            binding_key_value=binding_key,
            kind="data_source",
            reference=binding_ref,
        )
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(
            400,
            f"开发环境关系数据源绑定未就绪: {connector_service.sanitize_message(exc)}",
        ) from exc
    assert binding.connector_id == source.id


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
    transform_rules: dict,
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
        "key_transform_rules": (transform_rules or {}).get(key_property, []),
    }


def _validate_action_executor(
    db: Session,
    scenario_id: str,
    payload: ActionIn,
    *,
    existing_action: OntologyAction | None = None,
) -> None:
    """校验操作执行器引用的资源边界，保持操作配置可移植且不跨场景。"""
    config = payload.executor_config or {}
    if payload.executor_type == "unbound":
        if payload.enabled or config:
            raise HTTPException(400, "待绑定操作必须保持停用且不能包含执行配置")
        return
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
            raise HTTPException(400, f"本地技能操作配置无效: {exc}") from exc
    if payload.executor_type == "mcp" and config.get("mcp_id"):
        tenant_service.require_visible(db, MCPConfig, config["mcp_id"], "操作引用的 MCP 服务不存在")
    if payload.executor_type == "http":
        try:
            workflow_service.validate_http_action_config(config)
        except PolicyViolation as exc:
            raise HTTPException(400, f"外部接口操作配置无效: {exc}") from exc
    if payload.executor_type == "script" and not get_settings().allow_unsafe_workflow_nodes:
        raise HTTPException(400, "脚本操作默认停用；请改用受治理的操作或工作流节点")
    if payload.executor_type == "template":
        if not payload.requires_confirmation or not payload.idempotency_required:
            raise HTTPException(400, "模板附件操作必须启用人工确认和幂等保护")
        try:
            template_catalog_service.lock_scenarios_for_template_write(
                db,
                tenant_id=tenant_service.current_tenant_id(db),
                scenario_ids=[scenario_id],
            )
        except template_catalog_service.TemplateCatalogError as exc:
            raise HTTPException(409, str(exc)) from exc
        template_id = str(config.get("template_id") or "")
        template_file_id = str(config.get("template_file_id") or "")
        target_source_id = str(config.get("target_data_source_id") or "")
        if not (template_id or template_file_id) or not target_source_id:
            raise HTTPException(400, "模板附件操作需要选择源模板和附件目标资料库")
        target_source = _source_in_scenario(db, scenario_id, target_source_id)
        if target_source.tenant_id != tenant_service.current_tenant_id(db):
            raise HTTPException(400, "附件目标必须是当前租户自有资料库")
        if target_source.type != "file_bucket":
            raise HTTPException(400, "附件目标必须是文件桶数据源")
        existing_config = (
            existing_action.executor_config
            if existing_action is not None
            and existing_action.executor_type == "template"
            else {}
        ) or {}
        same_target = bool(
            existing_action is not None
            and str(existing_config.get("target_data_source_id") or "")
            == target_source.id
        )
        same_output = str(existing_config.get("output_filename") or "") == str(
            config.get("output_filename") or ""
        )
        same_template_pin = bool(
            (
                template_id
                and str(existing_config.get("template_id") or "") == template_id
                and config.get("template_version") is not None
                and str(existing_config.get("template_version"))
                == str(config.get("template_version"))
            )
            or (
                template_file_id
                and str(existing_config.get("template_file_id") or "")
                == template_file_id
            )
        )
        unchanged_shared_binding = bool(
            existing_action is not None
            and same_target
            and same_output
            and same_template_pin
        )
        if template_id:
            try:
                requested_version = (
                    int(config["template_version"])
                    if config.get("template_version") is not None else None
                )
                catalog_template, catalog_version, template_file, _template_source = (
                    template_catalog_service.resolve_version(
                        db,
                        template_id=template_id,
                        tenant_id=tenant_service.current_tenant_id(db),
                        scenario_id=scenario_id,
                        version_number=requested_version,
                        # Never trust a client-supplied digest while saving.
                        expected_sha256="",
                        # A deprecated version may remain on the same existing
                        # Action while its description/schema is edited. New or
                        # changed bindings must select an active template.
                        require_active=not unchanged_shared_binding,
                        # Serialize JSON Action binding with catalog deletion
                        # and lifecycle/version mutations (there is no FK from
                        # executor_config to the catalog row).
                        lock_template=True,
                    )
                )
            except (template_catalog_service.TemplateCatalogError, TypeError, ValueError) as exc:
                raise HTTPException(400, f"模板附件操作配置无效: {exc}") from exc
            # Catalog mutations take the template row first. Keep Action
            # binding on the same T -> D order, then replace the previously
            # observed target with its locked/refreshed row before trusting
            # tenant, scenario or type fields.
            locked_sources = _lock_template_data_sources(db, [target_source.id])
            target_source = locked_sources[target_source.id]
            if (
                target_source.tenant_id != tenant_service.current_tenant_id(db)
                or target_source.scenario_id not in (None, scenario_id)
                or target_source.type != "file_bucket"
            ):
                raise HTTPException(
                    409,
                    "附件目标在保存期间已变更且不再属于当前业务场景的文件桶",
                )
            pinned = template_catalog_service.pinned_action_config(
                catalog_template,
                catalog_version,
                target_data_source_id=target_source.id,
                output_filename=str(config.get("output_filename") or ""),
            )
        else:
            # Compatibility for clients and rows created before the catalog.
            legacy_row = db.execute(
                select(BucketFile, DataSource)
                .join(DataSource, DataSource.id == BucketFile.data_source_id)
                .where(
                    BucketFile.id == template_file_id,
                    DataSource.tenant_id == tenant_service.current_tenant_id(db),
                    or_(
                        DataSource.scenario_id.is_(None),
                        DataSource.scenario_id == scenario_id,
                    ),
                )
            ).first()
            if not legacy_row:
                raise HTTPException(400, "模板资源不存在或不在当前访问范围")
            template_file, template_source = legacy_row
            # Legacy bindings have no catalog row to serialize on. Lock both
            # data sources in stable ID order, followed by the source file
            # (D -> F), and use only the refreshed objects below.
            locked_sources = _lock_template_data_sources(
                db, [template_source.id, target_source.id]
            )
            template_source = locked_sources[template_source.id]
            target_source = locked_sources[target_source.id]
            tenant_id = tenant_service.current_tenant_id(db)
            if (
                template_source.tenant_id != tenant_id
                or template_source.scenario_id not in (None, scenario_id)
                or template_source.type != "file_bucket"
            ):
                raise HTTPException(
                    409,
                    "模板来源在保存期间已变更且不再属于当前业务场景的文件桶",
                )
            if (
                target_source.tenant_id != tenant_id
                or target_source.scenario_id not in (None, scenario_id)
                or target_source.type != "file_bucket"
            ):
                raise HTTPException(
                    409,
                    "附件目标在保存期间已变更且不再属于当前业务场景的文件桶",
                )
            template_file = db.scalar(
                select(BucketFile)
                .where(
                    BucketFile.id == template_file_id,
                    BucketFile.data_source_id == template_source.id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if not template_file:
                raise HTTPException(409, "模板文件在保存期间已删除或移动，请刷新后重试")
            try:
                pinned = template_artifact_service.pinned_template_metadata(
                    template_file, template_source
                )
            except template_artifact_service.TemplateArtifactError as exc:
                raise HTTPException(400, f"模板附件操作配置无效: {exc}") from exc
        if target_source.scenario_id is None and not unchanged_shared_binding:
            # Scenario write access does not grant authority to place generated
            # artifacts in a tenant-shared bucket. Existing Actions retain the
            # exact previously governed template/version/output/target binding.
            permission_service.require_tenant_permission(db, "write")
        output_filename = str(config.get("output_filename") or "")
        if len(output_filename) > 240 or "/" in output_filename or "\\" in output_filename:
            raise HTTPException(400, "输出文件名不能包含目录且不能超过 240 个字符")
        if output_filename:
            try:
                datasource_service.validate_bucket_filename(output_filename)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            requested_suffix = template_artifact_service.requested_output_suffix(
                output_filename
            )
            if requested_suffix:
                try:
                    requested_format = template_artifact_service.template_format(
                        f"output{requested_suffix}"
                    )[0]
                    template_format = template_artifact_service.template_format(
                        template_file.filename
                    )[0]
                except template_artifact_service.TemplateArtifactError as exc:
                    raise HTTPException(400, str(exc)) from exc
                if requested_format != template_format:
                    raise HTTPException(400, "输出附件必须与源模板保持相同文件格式")
        variable_paths = set(pinned.get("template_variable_paths") or [])
        variable_paths.update(
            template_artifact_service.referenced_variable_paths(output_filename)
        )
        try:
            payload.input_schema = template_artifact_service.merge_template_input_schema(
                payload.input_schema,
                variable_paths,
            )
        except template_artifact_service.TemplateArtifactError as exc:
            raise HTTPException(400, f"模板变量与输入参数不一致: {exc}") from exc
        pinned["template_variable_paths"] = sorted(variable_paths)
        # Only persist the closed, server-verified configuration.  In
        # particular, clients cannot choose their own template hash or MIME.
        if template_id:
            payload.executor_config = {
                **pinned,
                "target_data_source_id": target_source.id,
                "output_filename": output_filename,
            }
        else:
            payload.executor_config = {
                "template_file_id": template_file.id,
                "template_data_source_id": template_source.id,
                "target_data_source_id": target_source.id,
                "output_filename": output_filename,
                **pinned,
            }


def _validate_trigger_actions(db: Session, scenario_id: str, action_ids: list[str] | None) -> None:
    for action_id in action_ids or []:
        action = db.get(OntologyAction, action_id)
        if not action or action.scenario_id != scenario_id:
            raise HTTPException(400, "规则触发的操作不属于当前业务场景")


def _validate_workflow_refs(db: Session, scenario_id: str, steps: list, nodes: list) -> None:
    """校验工作流引用的 Action/Rule/Event，防止跨场景拼接执行图。"""
    try:
        workflow_service.validate_workflow_references(
            db,
            scenario_id,
            steps=steps,
            nodes=nodes,
        )
    except PolicyViolation as exc:
        raise HTTPException(400, str(exc)) from exc


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
        namespace=s.namespace or "default",
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


def _delete_scenario_governance_history(
    db: Session, scenario: BusinessScenario
) -> None:
    """Remove governance rows before the scenario ORM cascade runs.

    Releases, rollbacks and proposals keep RESTRICT foreign keys to immutable
    snapshots.  Deleting the scenario directly lets SQLAlchemy schedule the
    snapshot deletes before those rows, which fails on PostgreSQL even
    though every row belongs to the same user-owned scenario.  Clear the
    dependency chain explicitly; active staging/prod releases are rejected by
    ``assert_scenario_deletion_allowed`` before this helper is called.
    """
    proposal_ids = select(OntologyProposal.id).where(
        OntologyProposal.scenario_id == scenario.id
    )
    db.execute(
        delete(OntologyReview).where(OntologyReview.proposal_id.in_(proposal_ids))
    )
    # These rows reference snapshots/branches with RESTRICT and therefore must
    # disappear before the snapshot and branch rows are cascaded.
    db.execute(
        delete(OntologyRelease).where(OntologyRelease.scenario_id == scenario.id)
    )
    db.execute(
        delete(OntologyRollback).where(OntologyRollback.scenario_id == scenario.id)
    )
    db.execute(
        delete(OntologyProposal).where(OntologyProposal.scenario_id == scenario.id)
    )
    db.flush()

    # Keep already-loaded relationship collections from reintroducing stale
    # governance objects into the parent delete cascade.
    db.expire(
        scenario,
        (
            "ontology_releases",
            "ontology_rollbacks",
            "ontology_proposals",
            "ontology_snapshots",
            "ontology_branches",
        ),
    )


def _scenario_model_draft_out(
    row: ScenarioModelDraftResource,
    *,
    include_issues: bool = True,
) -> ScenarioModelDraftResourceOut:
    # Validation issue arrays can be very large because a compiler run may
    # attach the same evidence to many candidate resources.  The scene list
    # only needs payloads and lifecycle metadata; callers that open a draft or
    # need the full diagnostics can keep the legacy include_issues=true mode.
    issues = [
        item for item in (row.validation_issues or []) if isinstance(item, dict)
    ] if include_issues else []
    return ScenarioModelDraftResourceOut(
        id=row.id,
        scenario_id=row.scenario_id,
        proposal_id=row.proposal_id,
        predecessor_draft_id=row.predecessor_draft_id or "",
        predecessor_revision=int(row.predecessor_revision or 0),
        superseded_by_proposal_id=row.superseded_by_proposal_id or "",
        task_id=row.task_id or "",
        resource_kind=row.resource_kind,
        resource_key=row.resource_key,
        title=row.title or "",
        payload=row.payload if isinstance(row.payload, dict) else {},
        validation_issues=issues,
        issues_count=len(issues) if include_issues else 0,
        blocking_issue_count=sum(1 for item in issues if item.get("blocking", True)),
        draft_status=row.draft_status,
        # These are platform-owned constants, not editable lifecycle flags.
        enabled=False,
        publishable=False,
        resolved_resource_id=row.resolved_resource_id or "",
        source_thread_id=row.source_thread_id or "",
        source_message_id=row.source_message_id or "",
        compilation_job_id=row.compilation_job_id or "",
        source_refs=[str(value) for value in (row.source_refs or [])],
        revision=max(int(row.revision or 0), 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _refresh_resolved_draft_source_plan(
    db: Session,
    scenario: BusinessScenario,
    row: ScenarioModelDraftResource,
) -> None:
    """Advance and rebaseline the owner-scoped proposal after formal resolution."""
    if not row.source_message_id or not row.task_id:
        return
    thread = db.scalars(
        select(AssistantThread)
        .join(AssistantMessage, AssistantMessage.thread_id == AssistantThread.id)
        .where(
            AssistantMessage.id == row.source_message_id,
            AssistantThread.tenant_id == row.tenant_id,
            AssistantThread.scenario_id == scenario.id,
            AssistantThread.created_by_user_id == row.created_by_user_id,
        )
    ).first()
    message = db.get(AssistantMessage, row.source_message_id)
    if not thread or not message or message.thread_id != thread.id:
        return
    proposal = copy.deepcopy(
        message.proposal if isinstance(message.proposal, dict) else {}
    )
    if (
        proposal.get("kind") != "scenario_model"
        or str(proposal.get("proposal_id") or "") != row.proposal_id
    ):
        return

    # Local import avoids a router import cycle while reusing the one lifecycle
    # state machine that list/apply/result already expose.
    from . import assistant as assistant_router

    proposal_rows = list(db.scalars(
        select(ScenarioModelDraftResource).where(
            ScenarioModelDraftResource.tenant_id == row.tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == row.created_by_user_id,
            ScenarioModelDraftResource.proposal_id == row.proposal_id,
        )
    ).all())
    proposal = assistant_router._attach_draft_materialization(
        proposal,
        scenario_model_draft_service.draft_summary(proposal_rows),
    )
    data = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    remaining_task_rows = [
        candidate for candidate in proposal_rows
        if candidate.task_id == row.task_id
        and candidate.draft_status in scenario_model_draft_service.OPEN_DRAFT_STATUSES
    ]
    if not remaining_task_rows:
        data = assistant_router._refresh_model_task_states(
            data,
            applied_task_id=row.task_id,
            applied_status="applied",
        )
        manual_result = {
            "kind": "scenario_model",
            "task_id": row.task_id,
            "task_status": "applied",
            "manual_resolution": True,
            "resolved_draft_id": row.id,
            "resolved_resource_id": row.resolved_resource_id,
            "applied_change_keys": [],
        }
        task = next(
            (
                item for item in (data.get("tasks") or [])
                if isinstance(item, dict) and str(item.get("id") or "") == row.task_id
            ),
            None,
        )
        if task is not None:
            task["apply_result"] = manual_result
        proposal["apply_result"] = manual_result
    proposal["payload"] = data

    db.flush()
    db.expire(
        scenario,
        (
            "entities", "relations", "data_sources", "data_mappings",
            "relation_data_mappings", "function_definitions", "actions",
            "rules", "events", "workflows",
        ),
    )
    proposal["base_snapshot"] = assistant_router._scenario_snapshot(scenario)
    execution_status = str(data.get("execution_status") or "")
    proposal["status"] = (
        "applied"
        if execution_status == "completed"
        else "completed_with_gaps"
        if execution_status == "completed_with_gaps"
        else "in_progress"
    )
    proposal["requires_confirmation"] = bool(data.get("current_task_id"))
    proposal["run_revision"] = assistant_router._safe_nonnegative_int(
        data.get("execution_revision")
    )
    message.proposal = copy.deepcopy(proposal)
    message_context = (
        copy.deepcopy(message.context) if isinstance(message.context, dict) else {}
    )
    message_context.update({
        "status": (
            "success"
            if execution_status in {"completed", "completed_with_gaps"}
            else "waiting_confirmation"
        ),
        "run_revision": proposal["run_revision"],
        "model_run_id": proposal.get("proposal_id"),
    })
    message.context = message_context
    if row.compilation_job_id:
        job = db.scalars(
            select(AssistantCompilationJob).where(
                AssistantCompilationJob.id == row.compilation_job_id,
                AssistantCompilationJob.tenant_id == row.tenant_id,
                AssistantCompilationJob.created_by_user_id == row.created_by_user_id,
                AssistantCompilationJob.message_id == message.id,
            )
        ).first()
        if job and job.status == "succeeded":
            assistant_router._sync_compilation_job_result(
                job, proposal, canonical=True
            )


def _resolved_draft_ontology_reference_id(
    db: Session,
    *,
    scenario_id: str,
    model: Any,
    resource_prefix: str,
    reference: Any,
) -> str:
    if isinstance(reference, dict):
        stable_id = str(reference.get("id") or "").strip()
        if stable_id:
            resource = db.get(model, stable_id)
            return (
                str(resource.id)
                if resource and str(resource.scenario_id) == scenario_id
                else ""
            )
        reference = next(
            (
                reference.get(field)
                for field in ("api_name", "key", "name", "display_name")
                if str(reference.get(field) or "").strip()
            ),
            "",
        )
    token = str(reference or "").strip()
    if not token:
        return ""

    tokens = {token}
    lowered = token.casefold()
    for separator in (".", "_", ":", "-"):
        marker = f"{resource_prefix}{separator}"
        if lowered.startswith(marker) and len(token) > len(marker):
            tokens.add(token[len(marker):].strip())
    api_names = set(tokens)
    for value in tokens:
        try:
            api_names.add(ontology_service.normalize_api_name(
                value,
                prefix=resource_prefix,
                stable_key=value,
            ))
        except ValueError:
            continue
    matches = list(db.scalars(
        select(model).where(
            model.scenario_id == scenario_id,
            or_(
                model.id.in_(tokens),
                model.api_name.in_(api_names),
                model.name.in_(tokens),
            ),
        )
    ).all())
    unique = {str(item.id): item for item in matches}
    return next(iter(unique)) if len(unique) == 1 else ""


def _resolved_formal_resource_id(
    db: Session,
    *,
    scenario_id: str,
    draft: ScenarioModelDraftResource,
    resource_id: str,
) -> str:
    model_by_kind = {
        "entity": OntologyEntity,
        "relation": OntologyRelation,
        "instance": OntologyInstance,
        "mapping": DataMapping,
        "relation_mapping": RelationDataMapping,
        "function": FunctionDefinition,
        "action": OntologyAction,
        "rule": OntologyRule,
        "event": OntologyEvent,
        "workflow": OntologyWorkflow,
    }
    if draft.resource_kind == "property":
        prop = db.get(OntologyProperty, resource_id)
        if (
            prop
            and prop.entity
            and str(prop.entity.scenario_id) == scenario_id
        ):
            return str(prop.id)
        # Entity create/update responses do not expose child property IDs.
        # Accept the verified parent entity ID and resolve one exact child by
        # the staging payload's immutable API/display identity.
        entity = db.get(OntologyEntity, resource_id)
        if not entity or str(entity.scenario_id) != scenario_id:
            return ""
        working = draft.payload if isinstance(draft.payload, dict) else {}
        api_name = str(working.get("api_name") or "").strip()
        name = str(working.get("name") or "").strip()
        matches = [
            item for item in entity.properties
            if (api_name and str(item.api_name or "") == api_name)
            or (not api_name and name and str(item.name or "") == name)
        ]
        return str(matches[0].id) if len(matches) == 1 else ""
    if draft.resource_kind == "conceptual_mapping":
        source = draft.source_payload if isinstance(draft.source_payload, dict) else {}
        working = draft.payload if isinstance(draft.payload, dict) else {}
        mapping_kind = str(source.get("mapping_kind") or "").strip().casefold()
        if mapping_kind in {"entity", "object"}:
            mapping = db.get(DataMapping, resource_id)
            if not mapping or str(mapping.scenario_id) != scenario_id:
                return ""
            entity_id = _resolved_draft_ontology_reference_id(
                db,
                scenario_id=scenario_id,
                model=OntologyEntity,
                resource_prefix="entity",
                reference=working.get("entity_ref"),
            )
            return (
                str(mapping.id)
                if entity_id and str(mapping.entity_id) == entity_id
                else ""
            )
        if mapping_kind == "relation":
            mapping = db.get(RelationDataMapping, resource_id)
            if not mapping or str(mapping.scenario_id) != scenario_id:
                return ""
            relation_id = _resolved_draft_ontology_reference_id(
                db,
                scenario_id=scenario_id,
                model=OntologyRelation,
                resource_prefix="relation",
                reference=working.get("relation_ref"),
            )
            relation = db.get(OntologyRelation, relation_id) if relation_id else None
            source_mapping = db.get(DataMapping, mapping.source_mapping_id)
            target_mapping = db.get(DataMapping, mapping.target_mapping_id)
            source_entity = (
                db.get(OntologyEntity, relation.source_entity_id) if relation else None
            )
            target_entity = (
                db.get(OntologyEntity, relation.target_entity_id) if relation else None
            )
            expected_mode = str(working.get("mode") or "").strip().casefold()
            if (
                not relation
                or not source_entity
                or not target_entity
                or str(mapping.relation_id) != relation_id
                or not source_mapping
                or not target_mapping
                or str(source_entity.scenario_id) != scenario_id
                or str(target_entity.scenario_id) != scenario_id
                or str(source_mapping.scenario_id) != scenario_id
                or str(target_mapping.scenario_id) != scenario_id
                or str(source_mapping.entity_id) != str(relation.source_entity_id)
                or str(target_mapping.entity_id) != str(relation.target_entity_id)
                or (expected_mode and str(mapping.mode).casefold() != expected_mode)
            ):
                return ""
            return str(mapping.id)
        return ""
    model = model_by_kind.get(draft.resource_kind)
    if model is None:
        return ""
    resource = db.get(model, resource_id)
    return (
        str(resource.id)
        if resource and str(getattr(resource, "scenario_id", "")) == scenario_id
        else ""
    )


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


def _entity_out(
    db: Session,
    e: OntologyEntity,
    *,
    lifecycle_status: str | None = None,
) -> EntityOut:
    model_issues = ontology_service.entity_definition_issues(e)
    return EntityOut(
        id=e.id,
        scenario_id=e.scenario_id,
        name=e.name,
        api_name=e.api_name or ontology_service.normalize_api_name(
            display_name=e.name, prefix="entity", stable_key=e.id
        ),
        lifecycle_status=lifecycle_status or e.lifecycle_status or "active",
        namespace=e.namespace or (e.scenario.namespace if e.scenario else "default") or "default",
        description=e.description,
        icon=e.icon,
        color=e.color,
        is_abstract=e.is_abstract,
        state_property=e.state_property or "",
        created_at=e.created_at,
        properties=[
            PropertyIn(
                name=p.name,
                api_name=p.api_name or ontology_service.normalize_api_name(
                    display_name=p.name, prefix="property", stable_key=p.id
                ),
                data_type=p.data_type,
                description=p.description,
                is_key=p.is_key,
                is_title=bool(p.is_title),
                is_required=p.is_required,
                is_enum=p.is_enum,
                enum_values=p.enum_values or [],
                default_value=p.default_value,
                constraints=p.constraints or {},
                is_sensitive=bool(p.is_sensitive),
            )
            for p in e.properties
            if permission_service.can_read_property(db, p)
        ],
        model_ready=not model_issues,
        model_issues=model_issues,
    )


def _relation_out(r: OntologyRelation, entities: list[OntologyEntity]) -> RelationOut:
    name_map = {e.id: e.name for e in entities}
    relation_api_name = r.api_name or ontology_service.normalize_api_name(
        display_name=r.name, prefix="relation", stable_key=r.id
    )
    navigation = ontology_service.normalize_relation_navigation(
        relation_name=r.name,
        relation_api_name=relation_api_name,
        current=r,
    )
    return RelationOut(
        id=r.id,
        scenario_id=r.scenario_id,
        name=r.name,
        api_name=relation_api_name,
        namespace=r.namespace or (r.scenario.namespace if r.scenario else "default") or "default",
        source_entity_id=r.source_entity_id,
        target_entity_id=r.target_entity_id,
        **navigation,
        storage_kind=ontology_service.normalize_relation_storage_kind(r.storage_kind),
        relation_type=r.relation_type,
        constraints=ontology_service.normalize_relation_constraints(
            r.constraints or {}, relation_type=r.relation_type
        ),
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
    user_id = permission_service.require_principal(db).user_id
    try:
        payload.namespace = ontology_service.validate_namespace(payload.namespace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s = BusinessScenario(
        tenant_id=tenant_service.current_tenant_id(db),
        created_by_user_id=user_id,
        owner_user_id=user_id,
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
        state=i.state or "",
        valid_from=i.valid_from,
        valid_to=i.valid_to,
        quality=i.quality or {},
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
        source=ri.source or "manual",
        source_ref=ri.source_ref or "",
        source_metadata=ri.source_metadata or {},
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
        transform_rules=m.transform_rules or {},
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


def _runtime_definition_for_scenario(db: Session, scenario: BusinessScenario) -> Any:
    """Return the mutable authoring surface for ordinary scene reads/edits."""
    try:
        return runtime_definition_service.resolve_authoring(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise HTTPException(409, f"当前场景定义不可读取: {exc}") from exc


def _instance_in_current_runtime(
    instance: OntologyInstance,
    definition: Any | None = None,
) -> bool:
    if definition is not None:
        return ontology_service.instance_in_runtime_definition(instance, definition)
    return ontology_service.instance_in_runtime_environment(
        instance, runtime_connector_service.runtime_environment()
    )


def _relation_in_current_runtime(instance: RelationInstance, definition: Any) -> bool:
    return ontology_service.relation_instance_in_runtime_definition(instance, definition)


def _relation_mapping_out(m: RelationDataMapping) -> RelationDataMappingOut:
    return RelationDataMappingOut(
        id=m.id,
        scenario_id=m.scenario_id,
        relation_id=m.relation_id,
        relation_name=m.relation.name if m.relation else "",
        source_mapping_id=m.source_mapping_id,
        source_entity_name=(m.source_mapping.entity.name if m.source_mapping and m.source_mapping.entity else ""),
        target_mapping_id=m.target_mapping_id,
        target_entity_name=(m.target_mapping.entity.name if m.target_mapping and m.target_mapping.entity else ""),
        mode=m.mode,
        data_source_id=m.data_source_id,
        data_source_name=m.data_source.name if m.data_source else "",
        table_name=m.table_name or "",
        foreign_key_column=m.foreign_key_column or "",
        source_key_column=m.source_key_column or "",
        target_key_column=m.target_key_column or "",
        status=m.status or "unknown",
        last_error=connector_service.sanitize_message(m.last_error or ""),
        last_checked_at=m.last_checked_at,
        last_refreshed_at=m.last_refreshed_at,
        last_link_count=m.last_link_count or 0,
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
        relation_mapping_fingerprint=job.relation_mapping_fingerprint or "",
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
        state=instance.state or "",
        valid_from=instance.valid_from,
        valid_to=instance.valid_to,
        quality=instance.quality or {},
        access_scope=instance.access_scope or "tenant",
        provenance=_object_provenance(db, instance, mapping),
        relation_count=relation_count,
        created_at=instance.created_at,
    )


def _object_detail_out(
    db: Session,
    instance: OntologyInstance,
    definition: Any,
) -> ObjectDetailOut:
    relations: list[ObjectRelationOut] = []
    seen: set[str] = set()
    for relation_instance in [*instance.source_instances, *instance.target_instances]:
        if relation_instance.id in seen:
            continue
        seen.add(relation_instance.id)
        if not _relation_in_current_runtime(relation_instance, definition):
            continue
        outgoing = relation_instance.source_instance_id == instance.id
        related = relation_instance.target_instance if outgoing else relation_instance.source_instance
        relation = relation_instance.relation
        if (
            not related
            or not _instance_in_current_runtime(related, definition)
            or not permission_service.check_object(db, related, "read").allowed
        ):
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
    item = _object_item_out(db, instance, relation_count=len(relations))
    return ObjectDetailOut(**item.model_dump(), relations=relations)


@router.get(
    "/{scenario_id}/model-drafts",
    response_model=ScenarioModelDraftResourceListOut,
)
def list_scenario_model_drafts(
    scenario_id: str,
    proposal_id: str | None = Query(default=None, max_length=64),
    resource_kind: str | None = Query(default=None, max_length=40),
    draft_status: str | None = Query(default=None, max_length=30),
    include_resolved: bool = False,
    include_issues: bool = Query(
        default=True,
        description="是否返回每个草稿的完整校验问题明细；场景列表可关闭以减少响应体。",
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """List inert AI-generated resources under the normal scenario ACL."""
    scenario = _scenario_for_request(db, scenario_id)
    tenant_id = tenant_service.current_tenant_id(db)
    user_id = str(db.info.get("user_id") or "")
    filters = [
        ScenarioModelDraftResource.tenant_id == tenant_id,
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.created_by_user_id == user_id,
    ]
    if proposal_id:
        filters.append(ScenarioModelDraftResource.proposal_id == proposal_id)
    if resource_kind:
        normalized_kind = scenario_model_draft_service.normalize_resource_kind(resource_kind)
        if not normalized_kind:
            raise HTTPException(400, "不支持的场景模型草稿资源类型")
        filters.append(
            ScenarioModelDraftResource.resource_kind == normalized_kind
        )
    if draft_status:
        allowed_statuses = {
            "pending_confirmation", "ready_for_review", "needs_attention",
            "needs_validation", "accepted", "deferred", "applied", "resolved",
            "superseded",
        }
        if draft_status not in allowed_statuses:
            raise HTTPException(400, "不支持的场景模型草稿状态")
        filters.append(
            ScenarioModelDraftResource.draft_status == draft_status
        )
    elif not proposal_id:
        # A caller that names a proposal is inspecting that exact compilation
        # result, including its pre-confirmation staging rows.  Scene-level
        # lists still hide those rows until the proposal crosses the explicit
        # confirmation boundary.
        filters.append(
            ScenarioModelDraftResource.draft_status
            != scenario_model_draft_service.PENDING_CONFIRMATION_STATUS
        )
    if not draft_status and not include_resolved:
        filters.append(
            ScenarioModelDraftResource.draft_status.notin_({"resolved", "superseded"})
        )
    stmt = select(ScenarioModelDraftResource).where(*filters)
    draft_list_columns = []
    if not include_issues:
        # Keep large immutable provenance and diagnostic JSON columns out of
        # the compact list query entirely.  This prevents SQLAlchemy from
        # decoding them before the response serializer has a chance to omit
        # them.
        draft_list_columns = [
            ScenarioModelDraftResource.id,
            ScenarioModelDraftResource.scenario_id,
            ScenarioModelDraftResource.proposal_id,
            ScenarioModelDraftResource.predecessor_draft_id,
            ScenarioModelDraftResource.predecessor_revision,
            ScenarioModelDraftResource.superseded_by_proposal_id,
            ScenarioModelDraftResource.task_id,
            ScenarioModelDraftResource.resource_kind,
            ScenarioModelDraftResource.resource_key,
            ScenarioModelDraftResource.title,
            ScenarioModelDraftResource.payload,
            ScenarioModelDraftResource.source_refs,
            ScenarioModelDraftResource.draft_status,
            ScenarioModelDraftResource.resolved_resource_id,
            ScenarioModelDraftResource.source_thread_id,
            ScenarioModelDraftResource.source_message_id,
            ScenarioModelDraftResource.compilation_job_id,
            ScenarioModelDraftResource.revision,
            ScenarioModelDraftResource.created_at,
            ScenarioModelDraftResource.updated_at,
        ]
        stmt = stmt.options(load_only(*draft_list_columns))
    total = int(db.scalar(
        select(func.count()).select_from(ScenarioModelDraftResource).where(*filters)
    ) or 0)
    rows = list(db.scalars(
        stmt.order_by(
            ScenarioModelDraftResource.updated_at.desc(),
            ScenarioModelDraftResource.resource_kind,
            ScenarioModelDraftResource.title,
            ScenarioModelDraftResource.id,
        )
        .offset(offset)
        .limit(limit)
    ).all())
    all_rows_stmt = select(ScenarioModelDraftResource).where(*filters)
    if draft_list_columns:
        all_rows_stmt = all_rows_stmt.options(load_only(*draft_list_columns))
    all_rows = list(db.scalars(all_rows_stmt).all())
    next_offset = offset + len(rows)
    return ScenarioModelDraftResourceListOut(
        items=[
            _scenario_model_draft_out(row, include_issues=include_issues)
            for row in rows
        ],
        summary=scenario_model_draft_service.draft_summary(
            all_rows, include_issue_counts=include_issues
        ),
        page_summary=scenario_model_draft_service.draft_summary(
            rows, include_issue_counts=include_issues
        ),
        total=total,
        has_more=next_offset < total,
        next_offset=next_offset if next_offset < total else None,
    )


@router.patch(
    "/{scenario_id}/model-drafts/{draft_id}",
    response_model=ScenarioModelDraftResourceOut,
)
def update_scenario_model_draft(
    scenario_id: str,
    draft_id: str,
    payload: ScenarioModelDraftResourcePatch,
    db: Session = Depends(get_db),
):
    """Edit only the disabled staging copy and require revalidation."""
    scenario = _scenario_for_request(db, scenario_id, writable=True)
    tenant_id = tenant_service.current_tenant_id(db)
    user_id = str(db.info.get("user_id") or "")
    try:
        row = scenario_model_draft_service.update_working_draft_atomic(
            db,
            tenant_id=tenant_id,
            scenario_id=scenario.id,
            draft_id=draft_id,
            created_by_user_id=user_id,
            payload=payload.payload,
            expected_revision=payload.expected_revision,
        )
    except LookupError as exc:
        raise HTTPException(404, "场景模型草稿资源不存在") from exc
    except scenario_model_draft_service.DraftRevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.add(
        AssistantAuditLog(
            tenant_id=tenant_id,
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=scenario.id,
            thread_id=None,
            operation="update_model_draft_resource",
            status="success",
            context={
                "draft_resource_id": row.id,
                "proposal_id": row.proposal_id,
                "resource_kind": row.resource_kind,
                "resource_key": row.resource_key,
                "revision": row.revision,
            },
            result={
                "draft_status": row.draft_status,
                "enabled": False,
                "publishable": False,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _scenario_model_draft_out(row)


@router.post(
    "/{scenario_id}/model-drafts/{draft_id}/resolve",
    response_model=ScenarioModelDraftResourceOut,
)
def resolve_scenario_model_draft(
    scenario_id: str,
    draft_id: str,
    payload: ScenarioModelDraftResourceResolve,
    db: Session = Depends(get_db),
):
    """Resolve staging only after a matching formal scene resource exists."""
    scenario = _scenario_for_request(db, scenario_id, writable=True)
    tenant_id = tenant_service.current_tenant_id(db)
    user_id = str(db.info.get("user_id") or "")
    # Serialize formal-resolution lifecycle changes with assistant task apply.
    scenario = db.scalars(
        select(BusinessScenario)
        .where(
            BusinessScenario.id == scenario.id,
            BusinessScenario.tenant_id == tenant_id,
        )
        .with_for_update()
    ).one()
    row = db.scalars(
        select(ScenarioModelDraftResource)
        .where(
            ScenarioModelDraftResource.id == draft_id,
            ScenarioModelDraftResource.tenant_id == tenant_id,
            ScenarioModelDraftResource.scenario_id == scenario.id,
            ScenarioModelDraftResource.created_by_user_id == user_id,
        )
    ).first()
    if not row:
        raise HTTPException(404, "场景模型草稿资源不存在")
    resolved_resource_id = _resolved_formal_resource_id(
        db,
        scenario_id=scenario.id,
        draft=row,
        resource_id=payload.resolved_resource_id,
    )
    if not resolved_resource_id:
        # Keep missing, cross-scene and wrong-kind identifiers indistinguishable.
        raise HTTPException(409, "正式资源不存在、类型不匹配或不属于当前场景")
    try:
        row = scenario_model_draft_service.resolve_draft_atomic(
            db,
            tenant_id=tenant_id,
            scenario_id=scenario.id,
            draft_id=draft_id,
            created_by_user_id=user_id,
            expected_revision=payload.expected_revision,
            resolved_resource_id=resolved_resource_id,
        )
    except scenario_model_draft_service.DraftRevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    _refresh_resolved_draft_source_plan(db, scenario, row)
    db.add(
        AssistantAuditLog(
            tenant_id=tenant_id,
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=scenario.id,
            thread_id=None,
            operation="resolve_model_draft_resource",
            status="success",
            context={
                "draft_resource_id": row.id,
                "proposal_id": row.proposal_id,
                "resource_kind": row.resource_kind,
                "resource_key": row.resource_key,
                "revision": row.revision,
            },
            result={
                "draft_status": "resolved",
                "resolved_resource_id": row.resolved_resource_id,
                "enabled": False,
                "publishable": False,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _scenario_model_draft_out(row)


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario(
    scenario_id: str,
    db: Session = Depends(get_db),
    include_runtime_facts: bool = Query(
        default=True,
        description="是否返回对象/关系实例；模型与映射页可关闭以避免传输大批运行时事实",
    ),
):
    s = _scenario_for_request(db, scenario_id)
    definition = _runtime_definition_for_scenario(db, s)
    base = _scenario_out(s)
    entity_ids = {str(item) for item in definition.entities}
    relation_ids = {str(item) for item in definition.relations}
    mapping_ids = {str(item) for item in definition.mappings}
    relation_mapping_ids = {str(item) for item in definition.relation_mappings}
    function_ids = {str(item) for item in definition.functions}
    action_ids = {str(item) for item in definition.actions}
    rule_ids = {str(item) for item in definition.rules}
    event_ids = {str(item) for item in definition.events}
    workflow_ids = {str(item) for item in definition.workflows}
    active_entities = [e for e in s.entities if e.id in entity_ids]
    entities = [
        _entity_out(
            db,
            e,
            lifecycle_status=str(
                getattr(definition.entities.get(e.id), "lifecycle_status", "active")
                or "active"
            ),
        )
        for e in active_entities
    ]
    relations = [
        _relation_out(r, active_entities)
        for r in s.relations
        if r.id in relation_ids
        and r.source_entity_id in entity_ids
        and r.target_entity_id in entity_ids
    ]
    from ..schemas import DataSourceOut

    visible_data_source_ids = [
        d.id
        for d in s.data_sources
        if d.tenant_id == tenant_service.current_tenant_id(db) or d.is_public
    ]
    file_counts = {
        str(source_id): int(count)
        for source_id, count in db.execute(
            select(BucketFile.data_source_id, func.count())
            .where(BucketFile.data_source_id.in_(visible_data_source_ids))
            .group_by(BucketFile.data_source_id)
        ).all()
    } if visible_data_source_ids else {}
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
            file_count=file_counts.get(d.id, 0),
        )
        for d in s.data_sources
        if d.tenant_id == tenant_service.current_tenant_id(db) or d.is_public
    ]
    runtime_instance_count = db.scalar(
        select(func.count())
        .select_from(OntologyInstance)
        .where(OntologyInstance.scenario_id == scenario_id)
    ) or 0
    runtime_relation_count = db.scalar(
        select(func.count())
        .select_from(RelationInstance)
        .where(RelationInstance.scenario_id == scenario_id)
    ) or 0
    runtime_facts_truncated = bool(
        include_runtime_facts
        and (
            runtime_instance_count > INLINE_RUNTIME_FACT_LIMIT
            or runtime_relation_count > INLINE_RUNTIME_FACT_LIMIT
        )
    )
    if include_runtime_facts and not runtime_facts_truncated:
        visible_instances = [
            instance
            for instance in s.instances
            if _instance_in_current_runtime(instance, definition)
            and permission_service.check_object(db, instance, "read").allowed
        ]
        visible_instance_ids = {instance.id for instance in visible_instances}
        instances = [_instance_out(db, instance) for instance in visible_instances]
        rel_instances = [
            _rel_instance_out(ri)
            for ri in s.relation_instances
            if ri.source_instance_id in visible_instance_ids
            and ri.target_instance_id in visible_instance_ids
            and _relation_in_current_runtime(ri, definition)
        ]
    else:
        instances = []
        rel_instances = []
    mappings = [_mapping_out(m) for m in s.data_mappings if m.id in mapping_ids]
    relation_mappings = [
        _relation_mapping_out(m)
        for m in s.relation_data_mappings
        if m.id in relation_mapping_ids
    ]
    functions = [
        _function_out(function)
        for function in s.function_definitions
        if function.id in function_ids
    ]
    actions = [
        _action_out(action)
        for action in s.actions
        if action.id in action_ids
        and permission_service.check_action(db, action, "read").allowed
    ]
    rules = [_rule_out(r) for r in s.rules if r.id in rule_ids]
    events = [_event_out(e) for e in s.events if e.id in event_ids]
    workflows = [
        _workflow_out(workflow)
        for workflow in s.workflows
        if workflow.id in workflow_ids
        and permission_service.check_workflow(db, workflow, "read").allowed
    ]
    return ScenarioDetail(
        **base.model_dump(),
        can_write=permission_service.check_scenario(db, s, "write").allowed,
        entities=entities,
        relations=relations,
        data_sources=ds_out,
        instances=instances,
        relation_instances=rel_instances,
        runtime_instance_count=int(runtime_instance_count),
        runtime_relation_count=int(runtime_relation_count),
        runtime_facts_truncated=runtime_facts_truncated,
        mappings=mappings,
        relation_mappings=relation_mappings,
        functions=functions,
        actions=actions,
        rules=rules,
        events=events,
        workflows=workflows,
    )


@router.get("/{scenario_id}/graph")
def scenario_graph(scenario_id: str, mode: str = "schema", db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id)
    definition = _runtime_definition_for_scenario(db, s)
    return ontology_service.build_graph(
        s,
        mode=mode,
        db=db,
        environment=runtime_connector_service.runtime_environment(),
        runtime_definition=definition,
    )


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
    scenario = _scenario_for_request(db, scenario_id)
    definition = _runtime_definition_for_scenario(db, scenario)
    filters = [OntologyInstance.scenario_id == scenario_id]
    active_entity_ids = [str(item) for item in definition.entities]
    if not active_entity_ids:
        return ObjectSearchOut(
            items=[],
            total=0,
            limit=limit,
            offset=offset,
            query=q.strip().lower(),
            entity_id=entity_id,
            has_more=False,
            next_offset=None,
            total_is_exact=True,
        )
    filters.append(OntologyInstance.entity_id.in_(active_entity_ids))
    if entity_id:
        _entity_in_scenario(db, scenario_id, entity_id)
        filters.append(OntologyInstance.entity_id == entity_id)
    query = q.strip().lower()
    if query:
        # Search in SQL first.  The old implementation loaded every runtime
        # object and only then filtered in Python, which made a large imported
        # dataset look like a page that never finishes loading.
        search_pattern = f"%{query}%"
        filters.append(
            or_(
                func.lower(OntologyInstance.name).like(search_pattern),
                cast(OntologyInstance.attributes, String).ilike(search_pattern),
            )
        )
    candidates = db.execute(
        select(OntologyInstance)
        .options(
            joinedload(OntologyInstance.entity).selectinload(OntologyEntity.properties),
            joinedload(OntologyInstance.scenario),
        )
        .where(*filters)
        .order_by(OntologyInstance.created_at.desc(), OntologyInstance.name.asc())
        .offset(offset)
        .limit(OBJECT_SEARCH_CANDIDATE_LIMIT + 1)
    ).scalars().all()
    has_candidate_more = len(candidates) > OBJECT_SEARCH_CANDIDATE_LIMIT
    candidates = candidates[:OBJECT_SEARCH_CANDIDATE_LIMIT]
    # Runtime definition and ACL are still checked in Python.  SQL is only a
    # coarse candidate filter; it must never become a side-channel around
    # object visibility or attribute masking.
    visible_candidates = [
        instance
        for instance in candidates
        if _instance_in_current_runtime(instance, definition)
        and permission_service.check_object(db, instance, "read").allowed
    ]
    # Keep the existing masked-attribute search semantics.  SQL JSON text
    # matching above only narrows candidates; the final check prevents a
    # forbidden attribute from producing a visible hit.
    if query:
        visible_candidates = [
            instance
            for instance in visible_candidates
            if query in instance.name.lower()
            or query in str(permission_service.filter_instance_attributes(db, instance)).lower()
        ]
    instances = visible_candidates[:limit]
    consumed = 0
    if instances:
        last_id = instances[-1].id
        consumed = next(
            (index + 1 for index, item in enumerate(candidates) if item.id == last_id),
            len(candidates),
        )
    elif candidates:
        consumed = len(candidates)
    has_more = has_candidate_more or len(visible_candidates) > limit
    next_offset = offset + consumed if has_more and consumed else None
    # Exact global totals would require scanning and ACL-checking the entire
    # dataset.  Return a useful bounded estimate and a cursor instead; the UI
    # labels estimates as such and can continue fetching without blocking.
    total = offset + len(visible_candidates) + (1 if has_candidate_more else 0)
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
            select(RelationInstance).where(
                RelationInstance.scenario_id == scenario_id,
                or_(
                    RelationInstance.source_instance_id.in_(instance_ids),
                    RelationInstance.target_instance_id.in_(instance_ids),
                )
            )
        ).scalars().all()
        for relation_instance in relation_rows:
            if not _relation_in_current_runtime(relation_instance, definition):
                continue
            relation_id = relation_instance.id
            source_instance_id = relation_instance.source_instance_id
            target_instance_id = relation_instance.target_instance_id
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
        has_more=has_more,
        next_offset=next_offset,
        total_is_exact=not has_candidate_more,
    )


@router.get("/{scenario_id}/objects/{object_id}", response_model=ObjectDetailOut)
def get_object(scenario_id: str, object_id: str, db: Session = Depends(get_db)):
    """返回对象属性、邻接关系和来源追踪信息。"""
    scenario = _scenario_for_request(db, scenario_id)
    definition = _runtime_definition_for_scenario(db, scenario)
    instance = db.get(OntologyInstance, object_id)
    if (
        not instance
        or instance.scenario_id != scenario_id
        or not _instance_in_current_runtime(instance, definition)
    ):
        raise HTTPException(404, "对象不存在")
    permission_service.require_object_permission(db, instance, "read")
    return _object_detail_out(db, instance, definition)


@router.put("/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: str, payload: ScenarioIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    try:
        payload.namespace = ontology_service.validate_namespace(payload.namespace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for k, v in payload.model_dump().items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return _scenario_out(s)


@router.delete("/{scenario_id}", response_model=Msg)
def delete_scenario(scenario_id: str, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    # Serialize deletion with child-row FK inserts (template creation and
    # releases). Reuse only the refreshed locked row for every preflight below.
    s = db.scalar(
        select(BusinessScenario)
        .where(
            BusinessScenario.id == s.id,
            BusinessScenario.tenant_id == tenant_service.current_tenant_id(db),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if not s:
        raise HTTPException(409, "业务场景在删除期间已变化，请刷新后重试")
    try:
        release_service.assert_scenario_deletion_allowed(db, s)
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        template_catalog_service.prepare_scenario_deletion(db, s)
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(409, str(exc)) from exc
    # Uploads lock their owning DataSource before writing MinIO and committing
    # BucketFile metadata.  Lock the same rows before the current-read below so
    # a concurrent upload either finishes and is queued here, or starts after
    # the scenario has gone and cannot commit an untracked object.
    scenario_sources = list(
        db.scalars(
            select(DataSource)
            .where(DataSource.scenario_id == s.id)
            .order_by(DataSource.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    source_by_id = {source.id: source for source in scenario_sources}
    source_ids = list(source_by_id)
    locked_files = (
        list(
            db.scalars(
                select(BucketFile)
                .where(BucketFile.data_source_id.in_(source_ids))
                .order_by(BucketFile.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).all()
        )
        if source_ids
        else []
    )
    bucket_files = [
        (bucket_file, source_by_id[bucket_file.data_source_id])
        for bucket_file in locked_files
    ]
    try:
        deletion_job_ids = [
            object_deletion_service.enqueue_bucket_file_deletion(
                db, bucket_file, data_source
            )
            for bucket_file, data_source in bucket_files
        ]
    except (ValueError, object_storage_service.ObjectStorageError) as exc:
        raise HTTPException(409, str(exc)) from exc
    _delete_scenario_governance_history(db, s)
    db.delete(s)
    db.commit()
    object_deletion_service.drain_jobs_best_effort(db, deletion_job_ids)
    return Msg(message="已删除")


# ── 实体 ──────────────────────────────────────
@router.post("/{scenario_id}/entities", response_model=EntityOut)
def create_entity(scenario_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    try:
        ontology_service.validate_entity_definition(
            payload, scenario_namespace=s.namespace or "default"
        )
        payload.namespace = ontology_service.validate_namespace(
            payload.namespace, default=s.namespace or "default"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _require_sensitive_property_management(db, payload.properties)
    try:
        entity_api_name = ontology_service.allocate_resource_api_name(
            db,
            OntologyEntity,
            scope_field="scenario_id",
            scope_id=scenario_id,
            value=payload.api_name,
            display_name=payload.name,
            prefix="entity",
            stable_key=payload.api_name or payload.name,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    entity_data = payload.model_dump(exclude={"properties"})
    entity_data["api_name"] = entity_api_name
    e = OntologyEntity(scenario_id=scenario_id, **entity_data)
    db.add(e)
    db.flush()
    for p in payload.properties:
        try:
            property_api_name = ontology_service.allocate_resource_api_name(
                db,
                OntologyProperty,
                scope_field="entity_id",
                scope_id=e.id,
                value=p.api_name,
                display_name=p.name,
                prefix="property",
                stable_key=p.api_name or f"{entity_api_name}.{p.name}",
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        property_data = p.model_dump()
        property_data["api_name"] = property_api_name
        db.add(OntologyProperty(entity_id=e.id, **property_data))
    db.commit()
    db.refresh(e)
    return _entity_out(db, e)


@router.put("/entities/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: str, payload: EntityIn, db: Session = Depends(get_db)):
    e = db.get(OntologyEntity, entity_id)
    if not e:
        raise HTTPException(404, "实体不存在")
    scenario = _scenario_for_request(db, e.scenario_id, writable=True)
    try:
        ontology_service.validate_entity_definition(
            payload, scenario_namespace=scenario.namespace or "default"
        )
        payload.namespace = ontology_service.validate_namespace(
            payload.namespace, default=scenario.namespace or "default"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _require_sensitive_property_management(db, payload.properties)
    try:
        entity_api_name = ontology_service.allocate_resource_api_name(
            db,
            OntologyEntity,
            scope_field="scenario_id",
            scope_id=e.scenario_id,
            value=payload.api_name,
            display_name=payload.name,
            prefix="entity",
            stable_key=e.id,
            current=e.api_name,
            resource_id=e.id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    for k in ("name", "namespace", "description", "icon", "color", "is_abstract", "state_property"):
        setattr(e, k, getattr(payload, k))
    # Older clients do not know this field.  Treat an omitted value as "keep"
    # on PUT so editing a retired Object Type cannot accidentally reactivate it.
    if "lifecycle_status" in payload.model_fields_set:
        e.lifecycle_status = payload.lifecycle_status
    e.api_name = entity_api_name
    # Prefer immutable api_name when a caller is renaming a display label. Old
    # clients omit it and retain the historical name-based compatibility path.
    remaining = list(e.properties)
    for property_payload in payload.properties:
        requested_api_name = (
            ontology_service.normalize_api_name(
                property_payload.api_name,
                display_name=property_payload.name,
                prefix="property",
            )
            if property_payload.api_name
            else ""
        )
        existing = next(
            (
                prop for prop in remaining
                if requested_api_name
                and str(getattr(prop, "api_name", "") or "") == requested_api_name
            ),
            None,
        )
        if existing is None:
            existing = next(
                (prop for prop in remaining if prop.name == property_payload.name),
                None,
            )
        if existing:
            remaining.remove(existing)
            try:
                property_api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyProperty,
                    scope_field="entity_id",
                    scope_id=e.id,
                    value=property_payload.api_name,
                    display_name=property_payload.name,
                    prefix="property",
                    stable_key=existing.id,
                    current=existing.api_name,
                    resource_id=existing.id,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            property_data = property_payload.model_dump()
            property_data["api_name"] = property_api_name
            for key, value in property_data.items():
                setattr(existing, key, value)
        else:
            try:
                property_api_name = ontology_service.allocate_resource_api_name(
                    db,
                    OntologyProperty,
                    scope_field="entity_id",
                    scope_id=e.id,
                    value=property_payload.api_name,
                    display_name=property_payload.name,
                    prefix="property",
                    stable_key=property_payload.api_name
                    or f"{entity_api_name}.{property_payload.name}",
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            property_data = property_payload.model_dump()
            property_data["api_name"] = property_api_name
            db.add(OntologyProperty(entity_id=e.id, **property_data))
    for obsolete in remaining:
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
    try:
        payload.namespace = ontology_service.validate_namespace(
            payload.namespace, default=s.namespace or "default"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _entity_in_scenario(db, scenario_id, payload.source_entity_id)
    _entity_in_scenario(db, scenario_id, payload.target_entity_id)
    try:
        constraints = ontology_service.normalize_relation_constraints(
            payload.constraints.model_dump(), relation_type=payload.relation_type
        )
        ontology_service.validate_relation_constraint_endpoints(
            constraints,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
        )
        ontology_service.validate_inverse_relation(
            db,
            scenario_id=scenario_id,
            relation_id=None,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
            constraints=constraints,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    relation_data = payload.model_dump(exclude={"constraints"})
    try:
        relation_api_name = ontology_service.allocate_resource_api_name(
            db,
            OntologyRelation,
            scope_field="scenario_id",
            scope_id=scenario_id,
            value=payload.api_name,
            display_name=payload.name,
            prefix="relation",
            stable_key=payload.api_name
            or f"{payload.source_entity_id}.{payload.name}.{payload.target_entity_id}",
        )
        navigation = ontology_service.normalize_relation_navigation(
            relation_name=payload.name,
            relation_api_name=relation_api_name,
            source_display_name=payload.source_display_name,
            source_api_name=payload.source_api_name,
            target_display_name=payload.target_display_name,
            target_api_name=payload.target_api_name,
        )
        storage_kind = ontology_service.normalize_relation_storage_kind(
            payload.storage_kind
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    relation_data.update(
        api_name=relation_api_name,
        storage_kind=storage_kind,
        **navigation,
    )
    r = OntologyRelation(scenario_id=scenario_id, constraints=constraints, **relation_data)
    db.add(r)
    db.commit()
    db.refresh(r)
    return _relation_out(r, s.entities)


@router.put("/relations/{relation_id}", response_model=RelationOut)
def update_relation(relation_id: str, payload: RelationIn, db: Session = Depends(get_db)):
    r = db.execute(
        select(OntologyRelation)
        .where(OntologyRelation.id == relation_id)
        .with_for_update()
    ).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "关系不存在")
    scenario = _scenario_for_request(db, r.scenario_id, writable=True)
    try:
        payload.namespace = ontology_service.validate_namespace(
            payload.namespace, default=scenario.namespace or "default"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _entity_in_scenario(db, r.scenario_id, payload.source_entity_id)
    _entity_in_scenario(db, r.scenario_id, payload.target_entity_id)
    if r.relation_instances and (
        r.source_entity_id != payload.source_entity_id
        or r.target_entity_id != payload.target_entity_id
    ):
        raise HTTPException(409, "已有关系实例时不能修改关系两端的对象类型")
    try:
        constraints = ontology_service.normalize_relation_constraints(
            payload.constraints.model_dump(), relation_type=payload.relation_type
        )
        ontology_service.validate_relation_constraint_endpoints(
            constraints,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
        )
        ontology_service.validate_inverse_relation(
            db,
            scenario_id=r.scenario_id,
            relation_id=r.id,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
            constraints=constraints,
        )
        ontology_service.validate_inverse_relation_dependents(
            db,
            scenario_id=r.scenario_id,
            relation_id=r.id,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
        )
        ontology_service.validate_existing_relation_graph(
            db, r, constraints=constraints, relation_type=payload.relation_type
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        relation_api_name = ontology_service.allocate_resource_api_name(
            db,
            OntologyRelation,
            scope_field="scenario_id",
            scope_id=r.scenario_id,
            value=payload.api_name,
            display_name=payload.name,
            prefix="relation",
            stable_key=r.id,
            current=r.api_name,
            resource_id=r.id,
        )
        navigation = ontology_service.normalize_relation_navigation(
            relation_name=payload.name,
            relation_api_name=relation_api_name,
            source_display_name=payload.source_display_name,
            source_api_name=payload.source_api_name,
            target_display_name=payload.target_display_name,
            target_api_name=payload.target_api_name,
            current=r,
        )
        storage_kind = ontology_service.normalize_relation_storage_kind(
            payload.storage_kind,
            current=r.storage_kind,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    relation_data = payload.model_dump(exclude={"constraints"})
    relation_data.update(
        api_name=relation_api_name,
        storage_kind=storage_kind,
        **navigation,
    )
    for k, v in relation_data.items():
        setattr(r, k, v)
    r.constraints = constraints
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
    inverse_dependents = [
        relation for relation in ontology_service.inverse_relation_dependents(
            db, scenario_id=r.scenario_id, relation_id=r.id
        )
        if relation.id != r.id
    ]
    if inverse_dependents:
        names = "、".join(relation.name for relation in inverse_dependents[:3])
        raise HTTPException(409, f"关系仍被逆关系定义引用：{names}；请先清除这些引用")
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
        try:
            payload.attributes, payload.state, payload.quality = (
                ontology_service.validate_instance_payload(
                    entity,
                    payload.attributes,
                    state=payload.state,
                    valid_from=payload.valid_from,
                    valid_to=payload.valid_to,
                    quality=payload.quality,
                )
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload.name = ontology_service.resolve_instance_display_name(
            entity,
            payload.attributes,
            explicit_name=payload.name,
        )
    i = OntologyInstance(scenario_id=scenario_id, **payload.model_dump())
    db.add(i)
    db.commit()
    db.refresh(i)
    return _instance_out(db, i)


@router.put("/instances/{instance_id}", response_model=InstanceOut)
def update_instance(instance_id: str, payload: InstanceIn, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i or not _instance_in_current_runtime(i):
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    permission_service.require_object_permission(db, i, "write")
    _require_restricted_scope_management(db, payload.access_scope)
    entity = _entity_in_scenario(db, i.scenario_id, payload.entity_id)
    if entity:
        permission_service.require_instance_attribute_write_permissions(
            db, entity, payload.attributes
        )
        try:
            payload.attributes, payload.state, payload.quality = (
                ontology_service.validate_instance_payload(
                    entity,
                    payload.attributes,
                    state=payload.state,
                    valid_from=payload.valid_from,
                    valid_to=payload.valid_to,
                    quality=payload.quality,
                )
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload.name = ontology_service.resolve_instance_display_name(
            entity,
            payload.attributes,
            explicit_name=payload.name,
        )
    for k in (
        "entity_id",
        "name",
        "attributes",
        "source",
        "source_ref",
        "state",
        "valid_from",
        "valid_to",
        "quality",
        "access_scope",
    ):
        setattr(i, k, getattr(payload, k))
    db.commit()
    db.refresh(i)
    return _instance_out(db, i)


@router.delete("/instances/{instance_id}", response_model=Msg)
def delete_instance(instance_id: str, db: Session = Depends(get_db)):
    i = db.get(OntologyInstance, instance_id)
    if not i or not _instance_in_current_runtime(i):
        raise HTTPException(404, "实例不存在")
    _scenario_for_request(db, i.scenario_id, writable=True)
    permission_service.require_object_permission(db, i, "write")
    for ri in list(i.source_instances) + list(i.target_instances):
        db.delete(ri)
    db.delete(i)
    db.commit()
    return Msg(message="已删除")


# ── 关系实例 ──────────────────────────────────
@router.get(
    "/{scenario_id}/relation-instances",
    response_model=RelationInstancePageOut,
)
def list_relation_instances(
    scenario_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a bounded relation-instance page for the instance workspace."""
    scenario = _scenario_for_request(db, scenario_id)
    definition = _runtime_definition_for_scenario(db, scenario)
    candidates = db.execute(
        select(RelationInstance)
        .options(
            joinedload(RelationInstance.relation),
            joinedload(RelationInstance.source_instance).joinedload(OntologyInstance.entity),
            joinedload(RelationInstance.target_instance).joinedload(OntologyInstance.entity),
        )
        .where(RelationInstance.scenario_id == scenario_id)
        .order_by(RelationInstance.created_at.desc(), RelationInstance.id.asc())
        .offset(offset)
        .limit(limit + 1)
    ).scalars().all()
    has_candidate_more = len(candidates) > limit
    candidates = candidates[:limit]
    visible = [
        row
        for row in candidates
        if row.source_instance
        and row.target_instance
        and _relation_in_current_runtime(row, definition)
        and _instance_in_current_runtime(row.source_instance, definition)
        and _instance_in_current_runtime(row.target_instance, definition)
        and permission_service.check_object(db, row.source_instance, "read").allowed
        and permission_service.check_object(db, row.target_instance, "read").allowed
    ]
    has_more = has_candidate_more
    next_offset = offset + len(candidates) if has_more and candidates else None
    return RelationInstancePageOut(
        items=[_rel_instance_out(row) for row in visible],
        total=offset + len(visible) + (1 if has_candidate_more else 0),
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=next_offset,
        total_is_exact=not has_candidate_more,
    )


@router.post("/{scenario_id}/relation-instances", response_model=RelationInstanceOut)
def create_relation_instance(scenario_id: str, payload: RelationInstanceIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    relation = db.execute(
        select(OntologyRelation)
        .where(OntologyRelation.id == payload.relation_id)
        .with_for_update()
    ).scalar_one_or_none()
    source = db.get(OntologyInstance, payload.source_instance_id)
    target = db.get(OntologyInstance, payload.target_instance_id)
    if not relation or relation.scenario_id != scenario_id:
        raise HTTPException(400, "关系不属于当前业务场景")
    if not source or not target or source.scenario_id != scenario_id or target.scenario_id != scenario_id:
        raise HTTPException(400, "关系两端实例不属于当前业务场景")
    if not _instance_in_current_runtime(source) or not _instance_in_current_runtime(target):
        raise HTTPException(400, "关系两端实例不属于当前运行环境")
    permission_service.require_object_permission(db, source, "write")
    permission_service.require_object_permission(db, target, "write")
    if source.entity_id != relation.source_entity_id or target.entity_id != relation.target_entity_id:
        raise HTTPException(400, "关系实例两端实体与关系定义不匹配")
    try:
        ontology_service.validate_relation_instance_create(
            db,
            relation,
            source_instance_id=source.id,
            target_instance_id=target.id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ri = RelationInstance(scenario_id=scenario_id, **payload.model_dump())
    db.add(ri)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.execute(
            select(RelationInstance.id).where(
                RelationInstance.relation_id == payload.relation_id,
                RelationInstance.source_instance_id == payload.source_instance_id,
                RelationInstance.target_instance_id == payload.target_instance_id,
            )
        ).scalar_one_or_none()
        if duplicate:
            raise HTTPException(409, "该关系实例已存在") from exc
        raise HTTPException(409, "关系实例写入发生完整性冲突") from exc
    db.refresh(ri)
    return _rel_instance_out(ri)


@router.delete("/relation-instances/{ri_id}", response_model=Msg)
def delete_relation_instance(ri_id: str, db: Session = Depends(get_db)):
    ri = db.get(RelationInstance, ri_id)
    if (
        not ri
        or not _instance_in_current_runtime(ri.source_instance)
        or not _instance_in_current_runtime(ri.target_instance)
    ):
        raise HTTPException(404, "关系实例不存在")
    _scenario_for_request(db, ri.scenario_id, writable=True)
    source = ri.source_instance
    target = ri.target_instance
    if source:
        permission_service.require_object_permission(db, source, "write")
    if target:
        permission_service.require_object_permission(db, target, "write")
    relation = db.execute(
        select(OntologyRelation)
        .where(OntologyRelation.id == ri.relation_id)
        .with_for_update()
    ).scalar_one_or_none()
    if relation:
        try:
            ontology_service.validate_relation_instance_delete(db, relation, ri)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    db.delete(ri)
    db.commit()
    return Msg(message="已删除")


# ── 数据映射 ──────────────────────────────────
@router.post(
    "/{scenario_id}/relation-mappings/preflight",
    response_model=RelationDataMappingPreviewOut,
)
def preflight_relation_mapping(
    scenario_id: str,
    payload: RelationDataMappingIn,
    db: Session = Depends(get_db),
):
    scenario = _scenario_for_request(db, scenario_id)
    try:
        _derived, preview = ontology_service.validate_relation_data_mapping(
            db, scenario, payload
        )
        return RelationDataMappingPreviewOut(**preview)
    except Exception as exc:  # noqa: BLE001
        return RelationDataMappingPreviewOut(
            ok=False,
            message="关系映射预检未通过",
            mode=payload.mode,
            errors=[connector_service.sanitize_message(exc)],
        )


@router.post(
    "/{scenario_id}/relation-mappings",
    response_model=RelationDataMappingOut,
)
def create_relation_mapping(
    scenario_id: str,
    payload: RelationDataMappingIn,
    db: Session = Depends(get_db),
):
    scenario = _scenario_for_request(db, scenario_id, writable=True)
    if db.execute(
        select(RelationDataMapping).where(
            RelationDataMapping.scenario_id == scenario_id,
            RelationDataMapping.relation_id == payload.relation_id,
        )
    ).scalar_one_or_none():
        raise HTTPException(409, "该关系已经配置数据映射，请编辑现有映射")
    try:
        derived, _preview = ontology_service.validate_relation_data_mapping(
            db, scenario, payload
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400, f"关系映射校验失败: {connector_service.sanitize_message(exc)}"
        ) from exc
    _ensure_relation_mapping_dev_binding(db, scenario, derived)
    mapping = RelationDataMapping(
        scenario_id=scenario_id,
        status="ready",
        last_checked_at=datetime.now(timezone.utc),
        **derived,
    )
    try:
        with db.begin_nested():
            db.add(mapping)
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该关系已经配置数据映射") from exc
    _cancel_relation_mapping_endpoint_jobs(
        db,
        {mapping.source_mapping_id, mapping.target_mapping_id},
        reason="关系映射已创建，请重新提交对象映射刷新",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该关系已经配置数据映射") from exc
    db.refresh(mapping)
    return _relation_mapping_out(mapping)


@router.put(
    "/relation-mappings/{mapping_id}",
    response_model=RelationDataMappingOut,
)
def update_relation_mapping(
    mapping_id: str,
    payload: RelationDataMappingIn,
    db: Session = Depends(get_db),
):
    mapping = _relation_mapping_for_request(db, mapping_id, writable=True)
    scenario = _scenario_for_request(db, mapping.scenario_id, writable=True)
    duplicate = db.execute(
        select(RelationDataMapping).where(
            RelationDataMapping.scenario_id == mapping.scenario_id,
            RelationDataMapping.relation_id == payload.relation_id,
            RelationDataMapping.id != mapping.id,
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(409, "该关系已经配置数据映射")
    try:
        derived, _preview = ontology_service.validate_relation_data_mapping(
            db, scenario, payload
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400, f"关系映射校验失败: {connector_service.sanitize_message(exc)}"
        ) from exc
    _ensure_relation_mapping_dev_binding(db, scenario, derived)
    before = tuple(
        getattr(mapping, field)
        for field in (
            "relation_id", "source_mapping_id", "target_mapping_id", "mode",
            "data_source_id", "data_source_binding_key", "data_source_binding_ref",
            "table_name", "foreign_key_column",
            "source_key_column", "target_key_column",
        )
    )
    previous_endpoint_ids = {mapping.source_mapping_id, mapping.target_mapping_id}
    for field, value in derived.items():
        setattr(mapping, field, value)
    after = tuple(
        getattr(mapping, field)
        for field in (
            "relation_id", "source_mapping_id", "target_mapping_id", "mode",
            "data_source_id", "data_source_binding_key", "data_source_binding_ref",
            "table_name", "foreign_key_column",
            "source_key_column", "target_key_column",
        )
    )
    if before != after:
        ontology_service.purge_relation_mapping_instances(db, mapping.id)
        mapping.last_refreshed_at = None
        mapping.last_link_count = 0
        _cancel_relation_mapping_endpoint_jobs(
            db,
            previous_endpoint_ids | {mapping.source_mapping_id, mapping.target_mapping_id},
            reason="关系映射定义已更新，请重新提交对象映射刷新",
        )
    mapping.status = "ready"
    mapping.last_error = ""
    mapping.last_checked_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该关系已经配置数据映射") from exc
    db.refresh(mapping)
    return _relation_mapping_out(mapping)


@router.delete("/relation-mappings/{mapping_id}", response_model=Msg)
def delete_relation_mapping(mapping_id: str, db: Session = Depends(get_db)):
    mapping = _relation_mapping_for_request(db, mapping_id, writable=True)
    scenario = _scenario_for_request(db, mapping.scenario_id, writable=True)
    try:
        release_service.assert_resource_deletion_allowed(
            db, scenario, kind="relation_mapping", resource_id=mapping.id
        )
    except release_service.ReleaseValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    ontology_service.purge_relation_mapping_instances(db, mapping.id)
    _cancel_relation_mapping_endpoint_jobs(
        db,
        {mapping.source_mapping_id, mapping.target_mapping_id},
        reason="关系映射已删除，请重新提交对象映射刷新",
    )
    db.delete(mapping)
    db.commit()
    return Msg(message="已删除关系映射及其生成的关系实例")


@router.post("/{scenario_id}/mappings", response_model=DataMappingOut)
def create_mapping(scenario_id: str, payload: DataMappingIn, db: Session = Depends(get_db)):
    s = _scenario_for_request(db, scenario_id, writable=True)
    entity = _entity_in_scenario(db, scenario_id, payload.entity_id)
    assert entity is not None
    selected_source = _source_in_scenario(db, scenario_id, payload.data_source_id)
    mapping_data = payload.model_dump()
    try:
        mapping_data["transform_rules"] = ontology_service.normalize_transform_rules(
            entity, mapping_data.get("transform_rules")
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        binding = connector_service.runtime_binding_from_config(mapping_data, "data_source")
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(400, f"映射运行时绑定配置无效: {exc}") from exc
    key_field, ref_field = connector_service.runtime_binding_fields("data_source")
    if binding is None:
        metadata = connector_service.runtime_binding_metadata(
            "data_source",
            {"name": selected_source.name, "type": selected_source.type},
            path=f"scenario:{scenario_id}:entity:{payload.entity_id}",
        )
        mapping_data[key_field] = metadata["binding_key"]
        mapping_data[ref_field] = connector_service.with_required_capabilities(
            metadata["reference"], "sql_read"
        )
    else:
        # Only retain the compact adapter/capability descriptor.  Names,
        # endpoints and any credential-shaped values never become mapping state.
        mapping_data[key_field] = binding["binding_key"]
        mapping_data[ref_field] = connector_service.with_required_capabilities(
            binding["reference"], "sql_read"
        )
    # A generated logical key is useful only when dev can actually resolve it.
    # Materialise and health-check the binding in the same authoring flow so a
    # later refresh does not enter retries merely because the server generated
    # metadata without its ConnectorBinding authority row.
    dev_binding = connector_service.upsert_binding(
        db,
        s,
        environment="dev",
        binding_key_value=str(mapping_data[key_field]),
        kind="data_source",
        connector_id=selected_source.id,
        reference_label=f"{entity.name} / {mapping_data.get('table_name') or '未选择表'}",
        check=True,
        created_by_user_id=(str(db.info.get("user_id")) if db.info.get("user_id") else None),
    )
    try:
        connector_service.require_ready_binding(
            db,
            s,
            environment="dev",
            binding_key_value=str(mapping_data[key_field]),
            kind="data_source",
            reference=mapping_data[ref_field],
        )
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(
            400,
            f"开发环境数据源绑定未就绪: {connector_service.sanitize_message(exc)}",
        ) from exc
    assert dev_binding.connector_id == selected_source.id
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
        transform_rules=mapping_data.get("transform_rules", {}),
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
            transform_rules=m.transform_rules or {},
        )
        legacy_physical_identity = {
            key: value
            for key, value in current_identity.items()
            if key not in {"data_source_binding_key", "data_source_binding_ref"}
        } == {
            key: value
            for key, value in incoming_identity.items()
            if key not in {"data_source_binding_key", "data_source_binding_ref"}
        }
        legacy_binding_upgrade = (
            not str(m.data_source_binding_key or "")
            and m.data_source_id == mapping_data["data_source_id"]
            and legacy_physical_identity
        )
        if current_identity == incoming_identity or legacy_binding_upgrade:
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
                _invalidate_relation_mappings_for_object_mapping(
                    db, m.id, remove=False
                )
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
            _invalidate_relation_mappings_for_object_mapping(
                db, duplicate.id, remove=True
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
    _invalidate_relation_mappings_for_object_mapping(db, m.id, remove=True)
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
    # The environment records which connector set owns execution.  It is not
    # a data-visibility boundary: authorized members must be able to inspect
    # the durable task even when a control-plane process uses another setting.
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
    observed = db.get(OntologyAction, action_id)
    if not observed:
        raise HTTPException(404, "操作不存在")
    observed_scenario_id = observed.scenario_id
    _scenario_for_request(db, observed_scenario_id, writable=True)
    try:
        template_catalog_service.lock_scenarios_for_template_write(
            db,
            tenant_id=tenant_service.current_tenant_id(db),
            scenario_ids=[observed_scenario_id],
        )
    except template_catalog_service.TemplateCatalogError as exc:
        raise HTTPException(409, str(exc)) from exc
    a = _lock_action_for_update(db, action_id, observed_scenario_id)
    _require_restricted_scope_management(db, payload.access_scope)
    _entity_in_scenario(db, a.scenario_id, payload.entity_id)
    _validate_action_executor(db, a.scenario_id, payload, existing_action=a)
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
        # A dry-run creates a durable confirmation pin when the caller later
        # submits it.  Resolve it from the same released definition as the
        # real effect, rather than letting an authoring preview confirm a
        # different live Action later.
        definition = runtime_definition_service.resolve_execution(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        a = runtime_definition_service.resolve_resource(definition, "action", action_id)
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise HTTPException(409, f"当前部署定义不可执行该操作: {exc}") from exc
    pin_values = (
        payload.correlation_id,
        payload.expected_environment,
        payload.expected_definition_snapshot_id,
        payload.expected_release_id,
        payload.expected_definition_hash,
    )
    if any(value is not None for value in pin_values) and not payload.preview_log_id:
        raise HTTPException(409, "确认信息不完整，请重新预演")
    preview_log: ActionExecutionLog | None = None
    if payload.preview_log_id:
        if payload.dry_run or not payload.confirm:
            raise HTTPException(409, "预演固定版本只能用于显式确认执行")
        preview_log = db.get(ActionExecutionLog, payload.preview_log_id)
        normalized = workflow_service.validate_action_params(
            a.input_schema or {}, payload.params
        )
        current_user_id = str(db.info.get("user_id") or "") or None
        if (
            not preview_log
            or preview_log.scenario_id != scenario.id
            or preview_log.target_type != "action"
            or preview_log.target_id != a.id
            or preview_log.mode != "dry_run"
            or preview_log.status != "dry_run"
            or preview_log.actor_user_id != current_user_id
            or (preview_log.input_params or {}) != normalized
        ):
            raise HTTPException(409, "操作预演与当前用户、目标或参数不一致，请重新预演")
        required_pin = {
            "correlation_id": payload.correlation_id,
            "expected_environment": payload.expected_environment,
            "expected_definition_hash": payload.expected_definition_hash,
        }
        if any(not value for value in required_pin.values()):
            raise HTTPException(409, "确认必须携带预演的 correlation、environment 和 definition_hash")
        if (
            payload.correlation_id != preview_log.correlation_id
            or payload.expected_environment != preview_log.environment
            or payload.expected_definition_snapshot_id != preview_log.definition_snapshot_id
            or payload.expected_release_id != preview_log.release_id
            or payload.expected_definition_hash != preview_log.definition_hash
            or definition.environment != preview_log.environment
            or definition.snapshot_id != preview_log.definition_snapshot_id
            or definition.release_id != preview_log.release_id
            or definition.definition_hash != preview_log.definition_hash
        ):
            raise HTTPException(409, "操作定义在预演后已变化，请重新预演")
        if preview_log.agent_message_id:
            preview_message = db.get(Message, preview_log.agent_message_id)
            if not preview_message or preview_message.role != "assistant":
                raise HTTPException(409, "操作预演所属的 Agent 消息已不可用，请重新预演")
            if not preview_message.stream_finalized:
                raise HTTPException(409, "Agent 回答仍在生成，请等待对话完成后再确认执行")
            preview_conversation = db.get(Conversation, preview_message.conversation_id)
            preview_agent = db.get(Agent, preview_log.agent_id) if preview_log.agent_id else None
            if (
                not preview_agent
                or preview_agent.tenant_id != tenant_service.current_tenant_id(db)
                or preview_agent.scenario_id != scenario.id
                or not preview_conversation
                or preview_conversation.agent_id != preview_agent.id
                or preview_conversation.created_by_user_id != current_user_id
            ):
                raise HTTPException(409, "操作预演与当前 Agent 对话不一致，请重新预演")
            scope = agent_capability_service.normalize_scope(
                preview_agent.capability_scope,
                legacy_default=False,
            )
            action_scope = scope["actions"]
            if (
                action_scope["mode"] != "all"
                and a.id not in set(action_scope["selected_ids"])
            ):
                raise HTTPException(409, "该操作已不在当前 Agent 的授权范围，请重新预演")
            contains_preview = False
            for entry in preview_message.tool_results or []:
                if not isinstance(entry, dict) or str(entry.get("name") or "") != "execute_action":
                    continue
                result: Any = entry.get("result")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        continue
                if (
                    isinstance(result, dict)
                    and result.get("status") == "dry_run"
                    and result.get("log_id") == preview_log.id
                ):
                    contains_preview = True
                    break
            if not contains_preview:
                raise HTTPException(409, "Agent 对话中未找到该操作预演，请重新预演")
        elif preview_log.agent_id:
            raise HTTPException(409, "Agent 操作预演缺少对话绑定，请重新预演")
    permission_service.require_action_permission(
        db,
        a,
        "read" if payload.dry_run else "execute",
    )
    previous_lineage = db.info.get("action_lineage_context")
    if preview_log:
        db.info["action_lineage_context"] = {
            "correlation_id": preview_log.correlation_id,
            "parent_action_log_id": preview_log.id,
            "agent_message_id": preview_log.agent_message_id,
            "assistant_message_id": preview_log.assistant_message_id,
        }
    try:
        response = workflow_service.execute_action(
            db,
            a,
            payload.params,
            confirm=payload.confirm,
            dry_run=payload.dry_run,
            idempotency_key=payload.idempotency_key,
            runtime_environment=definition.environment,
            runtime_definition=definition,
        )
        # A confirmed Action that originated from Agent chat becomes part of
        # that durable conversation. This is especially important for native
        # DOCX/XLSX/Markdown artifacts: history must retain the structured
        # download metadata instead of relying on model-authored Markdown.
        if preview_log and preview_log.agent_message_id:
            message = db.get(Message, preview_log.agent_message_id)
            if message and message.role == "assistant":
                updated_results: list[dict[str, Any]] = []
                changed = False
                for entry in message.tool_results or []:
                    item = dict(entry) if isinstance(entry, dict) else {"result": entry}
                    raw_result = item.get("result")
                    parsed: Any = raw_result
                    if isinstance(raw_result, str):
                        try:
                            parsed = json.loads(raw_result)
                        except json.JSONDecodeError:
                            parsed = None
                    if isinstance(parsed, dict) and parsed.get("log_id") == preview_log.id:
                        item["result"] = json.dumps(response, ensure_ascii=False, default=str)
                        changed = True
                    updated_results.append(item)
                if changed:
                    message.tool_results = updated_results
                    db.commit()
        return response
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"执行失败: {exc}")
    finally:
        if preview_log:
            if previous_lineage is None:
                db.info.pop("action_lineage_context", None)
            else:
                db.info["action_lineage_context"] = previous_lineage


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
    live_rule = db.get(OntologyRule, rule_id)
    if not live_rule:
        raise HTTPException(404, "规则不存在")
    scenario = _scenario_for_request(db, live_rule.scenario_id)
    record = (payload or {}).get("record", {})
    if not isinstance(record, dict):
        raise HTTPException(400, "规则评估记录必须是对象")
    try:
        # Rule evaluation is an authoring/debug operation with no external
        # side effect; it must remain available before the first release.
        definition = runtime_definition_service.resolve_authoring(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        rule = runtime_definition_service.resolve_resource(
            definition, "rule", rule_id
        )
        return workflow_service.evaluate_rule(
            rule,
            record,
            db=db,
            runtime_definition=definition,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"规则当前不可评估: {exc}") from exc


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
        definition = runtime_definition_service.resolve_execution(
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
        definition = runtime_definition_service.resolve_execution(
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
            actor_type=l.actor_type or "unknown",
            actor_user_id=l.actor_user_id,
            agent_id=l.agent_id,
            llm_config_id=l.llm_config_id,
            model_name=l.model_name or "",
            permission_decision=l.permission_decision or {},
            data_context=l.data_context or {},
            correlation_id=l.correlation_id or "",
            parent_action_log_id=l.parent_action_log_id,
            agent_message_id=l.agent_message_id,
            assistant_message_id=l.assistant_message_id,
            error=l.error,
            duration_ms=l.duration_ms,
            created_at=l.created_at,
        )
        for l in logs
        if _can_read_execution_log(db, l)
    ]
