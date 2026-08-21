"""P1 端到端血缘图构建。

该服务不把业务原文、Action 输入参数或外部系统返回全文暴露给图接口。它只汇总
已经持久化的稳定标识与安全摘要，从而把数据源、映射、对象、AI 回答、Action 及
外部结果串成可导航的有向图。图本身是派生视图：即使运行记录来自旧版本，也能
安全地展示可推导的部分血缘。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    BucketFile,
    Conversation,
    DataMapping,
    DataSource,
    DocumentChunk,
    Message,
    OntologyAction,
    OntologyInstance,
    OntologyRule,
    OntologyWorkflow,
    WorkflowRun,
)
from . import (
    permission_service,
    rag_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)


MAX_NODES = 800
MAX_EDGES = 1_600


def _node_id(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _json(value: Any) -> Any:
    """容忍历史工具结果中保存的 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _walk_ids(value: Any, key: str) -> set[str]:
    """从嵌套的工作流/工具结果中提取运行日志标识。"""
    value = _json(value)
    found: set[str] = set()
    if isinstance(value, dict):
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            found.add(raw)
        for child in value.values():
            found.update(_walk_ids(child, key))
    elif isinstance(value, list):
        for child in value:
            found.update(_walk_ids(child, key))
    return found


def _pinned_definition_for_record(
    db: Session,
    record: Any,
    cache: dict[tuple[str, str, str, str, str], Any | None],
) -> tuple[Any | None, bool]:
    """Return a record's frozen definition, without falling forward to live.

    Both durable workflow runs and action/workflow execution logs carry the
    release pin selected at dispatch.  A graph rendered from current ORM rows
    would otherwise give a reviewer the name and ACL of a later dev edit.  A
    non-dev or release-labelled record therefore needs a complete, valid pin;
    malformed historic evidence is hidden rather than guessed.
    """
    environment = str(getattr(record, "environment", None) or "dev")
    snapshot_id = str(getattr(record, "definition_snapshot_id", None) or "")
    release_id = str(getattr(record, "release_id", None) or "")
    source = str(getattr(record, "definition_source", None) or "live")
    needs_frozen = bool(
        environment != "dev"
        or source == "release"
        or snapshot_id
        or release_id
    )
    if not needs_frozen:
        return None, False
    if not snapshot_id or not release_id:
        return None, True

    scenario_id = str(getattr(record, "scenario_id", None) or "")
    key = (scenario_id, environment, snapshot_id, release_id, source)
    if key not in cache:
        try:
            # ``resolve_for_run`` intentionally consumes only these durable
            # fields.  A tiny immutable-shaped probe lets logs use the same
            # strict release/snapshot validation as workflow runs.
            probe = SimpleNamespace(
                scenario_id=scenario_id,
                environment=environment,
                definition_snapshot_id=snapshot_id,
                release_id=release_id,
            )
            cache[key] = runtime_definition_service.resolve_for_run(db, probe)
        except runtime_definition_service.RuntimeDefinitionError:
            cache[key] = None
    definition = cache[key]
    if not definition or not definition.is_frozen:
        return None, True
    # The stored hash is evidence too.  Do not draw a lineage edge that claims
    # a release whose normalized content differs from the execution record.
    stored_hash = str(getattr(record, "definition_hash", None) or "")
    if stored_hash and stored_hash != definition.definition_hash:
        return None, True
    return definition, True


def _resource_for_execution_log(
    db: Session,
    log: ActionExecutionLog,
    cache: dict[tuple[str, str, str, str, str], Any | None],
) -> tuple[Any | None, Any | None]:
    """Resolve an execution target and its definition with ACL parity."""
    target_kind = str(log.target_type or "")
    if target_kind not in {"action", "workflow", "rule"}:
        return None, None
    definition, needs_frozen = _pinned_definition_for_record(db, log, cache)
    if definition:
        try:
            resource = runtime_definition_service.resolve_resource(
                definition,
                target_kind,
                log.target_id,
            )
        except runtime_definition_service.RuntimeDefinitionError:
            return None, None
    elif needs_frozen:
        return None, None
    else:
        model = {
            "action": OntologyAction,
            "workflow": OntologyWorkflow,
            "rule": OntologyRule,
        }[target_kind]
        resource = db.get(model, log.target_id)
        if not resource or resource.scenario_id != log.scenario_id:
            return None, None

    if target_kind == "action":
        allowed = permission_service.check_action(db, resource, "read").allowed
    elif target_kind == "workflow":
        allowed = permission_service.check_workflow(db, resource, "read").allowed
    else:
        scenario = getattr(resource, "scenario", None) or db.get(
            BusinessScenario, resource.scenario_id
        )
        allowed = bool(
            scenario and permission_service.check_scenario(db, scenario, "read").allowed
        )
    return (resource, definition) if allowed else (None, None)


def _workflow_for_run_lineage(
    db: Session,
    run: WorkflowRun,
    cache: dict[tuple[str, str, str, str, str], Any | None],
) -> tuple[Any | None, Any | None]:
    """Resolve the workflow name/ACL from the definition pinned by ``run``."""
    definition, needs_frozen = _pinned_definition_for_record(db, run, cache)
    if definition:
        try:
            workflow = runtime_definition_service.resolve_resource(
                definition,
                "workflow",
                run.workflow_id,
            )
        except runtime_definition_service.RuntimeDefinitionError:
            return None, None
    elif needs_frozen:
        return None, None
    else:
        workflow = db.get(OntologyWorkflow, run.workflow_id)
        if not workflow or workflow.scenario_id != run.scenario_id:
            return None, None
    if not permission_service.check_workflow(db, workflow, "read").allowed:
        return None, None
    return workflow, definition


def _definition_meta(record: Any, definition: Any | None) -> dict[str, str]:
    """Expose only credential-free immutable version evidence in graph meta."""
    if definition and definition.is_frozen:
        return {
            "environment": definition.environment,
            "definition_source": definition.source,
            "definition_snapshot_id": definition.snapshot_id or "",
            "release_id": definition.release_id or "",
            "definition_hash": definition.definition_hash or "",
        }
    return {
        "environment": str(getattr(record, "environment", None) or "dev"),
        "definition_source": str(getattr(record, "definition_source", None) or "live"),
    }


def _versioned_resource_id(resource_id: str, definition: Any | None) -> str:
    """Keep separately released versions from collapsing into one graph node."""
    if definition and definition.is_frozen:
        evidence_id = definition.release_id or definition.snapshot_id
        if evidence_id:
            return f"{resource_id}@{evidence_id}"
    return resource_id


def _citation_items(message: Message) -> list[dict[str, Any]]:
    """新字段与历史 tool_results 均可作为引用来源，便于平滑升级。"""
    items = getattr(message, "citations", None)
    # 空数组代表旧消息或尚未使用检索；继续尝试从历史 tool_results 恢复。
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict)]

    # 新字段还未回填的历史会话：从 search_documents 工具结果恢复引用。
    recovered: list[dict[str, Any]] = []
    for result in message.tool_results or []:
        if not isinstance(result, dict) or result.get("name") != "search_documents":
            continue
        payload = _json(result.get("result"))
        if isinstance(payload, dict):
            citations = payload.get("citations") or []
            if isinstance(citations, list):
                recovered.extend(item for item in citations if isinstance(item, dict))
    return recovered


def _ontology_object_ids(message: Message) -> set[str]:
    """提取 Agent 通过 search_ontology 实际读取过的对象，形成对象→AI 回答血缘。"""
    found: set[str] = set()
    for result in message.tool_results or []:
        if not isinstance(result, dict) or result.get("name") != "search_ontology":
            continue
        payload = _json(result.get("result"))
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                found.add(str(item["id"]))
    return found


def _dedupe(items: Iterable[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        marker = tuple(item.get(key) for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _is_assistant_rag_source(source: object) -> bool:
    """Recognize both current and pre-versioned assistant RAG source cards."""
    if not isinstance(source, dict):
        return False
    return bool(
        source.get("kind") == "rag"
        or str(source.get("id") or "").startswith("rag:")
        or source.get("data_source_id")
        or source.get("file_id")
        or source.get("chunk_id")
        or source.get("file_content_hash")
    )


def _current_assistant_rag_source(
    db: Session,
    scenario_id: str,
    source_meta: object,
) -> tuple[DataSource, BucketFile, DocumentChunk | None] | None:
    """Resolve a durable global-assistant citation only if it is still current.

    Lineage is a read surface and must not reveal an answer's historic document
    path after an ACL change, source rebinding, file deletion or reindex.  The
    checks mirror assistant-history reauthorization rather than trusting JSON
    stored with a message.
    """
    if not isinstance(source_meta, dict):
        return None
    source_id = str(source_meta.get("data_source_id") or "")
    file_id = str(source_meta.get("file_id") or "")
    chunk_id = str(source_meta.get("chunk_id") or "")
    expected_file_hash = str(source_meta.get("file_content_hash") or "")
    if not source_id or not file_id or not expected_file_hash:
        return None

    source = tenant_service.get_visible(db, DataSource, source_id)
    scenario = tenant_service.get_visible(db, BusinessScenario, scenario_id)
    if (
        not source
        or source.tenant_id != tenant_service.current_tenant_id(db)
        or source.type != "file_bucket"
        or source.scenario_id != scenario_id
        or not scenario
        or not permission_service.check_scenario(db, scenario, "read").allowed
    ):
        return None
    bucket_file = db.get(BucketFile, file_id)
    if (
        not bucket_file
        or bucket_file.data_source_id != source.id
        or bucket_file.status != "parsed"
        or not rag_service._index_is_current(bucket_file)
        or bucket_file.indexed_content_hash != expected_file_hash
    ):
        return None
    if not chunk_id:
        return source, bucket_file, None
    chunk = db.get(DocumentChunk, chunk_id)
    if (
        not chunk
        or chunk.bucket_file_id != bucket_file.id
        or chunk.data_source_id != source.id
    ):
        return None
    expected_chunk_hash = str(source_meta.get("content_hash") or "")
    if expected_chunk_hash and chunk.content_hash != expected_chunk_hash:
        return None
    return source, bucket_file, chunk


def build_scenario_lineage(db: Session, scenario_id: str, *, limit: int = 300) -> dict[str, Any]:
    """生成 ``data source → object → AI → Action → external result`` 血缘图。

    ``scenario_id`` 的访问控制由路由在进入本服务前完成；服务仍然只读取该场景的
    实体、映射、运行和本场景 Agent 对话，不接受客户端提供的任意资源 ID。
    """
    max_items = max(1, min(int(limit), 500))
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    truncated = False

    def add_node(kind: str, value: str, label: str, meta: dict[str, Any] | None = None) -> str:
        nonlocal truncated
        nid = _node_id(kind, value)
        if nid not in nodes:
            if len(nodes) >= MAX_NODES:
                truncated = True
                return nid
            nodes[nid] = {"id": nid, "kind": kind, "label": label, "meta": meta or {}}
        return nid

    def add_edge(source: str, target: str, kind: str, label: str = "", meta: dict[str, Any] | None = None) -> None:
        nonlocal truncated
        if source not in nodes or target not in nodes:
            return
        edge_id = f"{source}>{kind}>{target}"
        if edge_id in edges:
            return
        if len(edges) >= MAX_EDGES:
            truncated = True
            return
        edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "kind": kind,
            "label": label,
            "meta": meta or {},
        }

    mappings = db.execute(
        select(DataMapping).where(DataMapping.scenario_id == scenario_id)
    ).scalars().all()
    mappings_by_id = {mapping.id: mapping for mapping in mappings}
    mappings_by_entity: dict[str, list[DataMapping]] = {}
    for mapping in mappings:
        mappings_by_entity.setdefault(mapping.entity_id, []).append(mapping)
        source = db.get(DataSource, mapping.data_source_id)
        if not source:
            continue
        source_node = add_node(
            "data_source",
            source.id,
            source.name,
            {"data_source_id": source.id, "type": source.type, "status": source.status or "unknown"},
        )
        mapping_node = add_node(
            "mapping",
            mapping.id,
            mapping.table_name or mapping.id,
            {"mapping_id": mapping.id, "table_name": mapping.table_name or "", "entity_id": mapping.entity_id},
        )
        add_edge(source_node, mapping_node, "mapped_as", mapping.table_name or "映射")

    instances = db.execute(
        select(OntologyInstance)
        .where(OntologyInstance.scenario_id == scenario_id)
        .order_by(OntologyInstance.created_at.desc())
        .limit(max_items + 1)
    ).scalars().all()
    if len(instances) > max_items:
        truncated = True
        instances = instances[:max_items]
    visible_objects: dict[str, OntologyInstance] = {}
    for instance in instances:
        # 对象级 ACL 必须在派生图中同样生效；否则只读血缘会成为绕过对象详情
        # 接口的侧信道。
        if not permission_service.check_object(db, instance, "read").allowed:
            continue
        visible_objects[instance.id] = instance
        object_node = add_node(
            "object",
            instance.id,
            instance.name,
            {"object_id": instance.id, "entity_id": instance.entity_id, "source": instance.source or "manual"},
        )
        # 新版可在 source_metadata 中精确记录 mapping_id；旧对象只有在实体只有一条
        # 映射时才安全地推导，避免把多源对象错误归因。
        metadata = getattr(instance, "source_metadata", None) or {}
        mapping_id = metadata.get("mapping_id") if isinstance(metadata, dict) else None
        mapping = mappings_by_id.get(str(mapping_id)) if mapping_id else None
        if mapping is None:
            candidates = mappings_by_entity.get(instance.entity_id, [])
            mapping = candidates[0] if len(candidates) == 1 and instance.source == "imported" else None
        if mapping:
            add_edge(_node_id("mapping", mapping.id), object_node, "materialized_as", instance.source_ref or "对象")

    messages = db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Agent, Agent.id == Conversation.agent_id)
        .where(Agent.scenario_id == scenario_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .limit(max_items + 1)
    ).scalars().all()
    if len(messages) > max_items:
        truncated = True
        messages = messages[:max_items]
    citations_by_message: dict[str, list[dict[str, Any]]] = {}
    action_ids_by_message: dict[str, set[str]] = {}
    object_ids_by_message: dict[str, set[str]] = {}
    for message in messages:
        answer_node = add_node(
            "ai_answer",
            message.id,
            "AI 回答",
            {"message_id": message.id, "created_at": message.created_at.isoformat() if message.created_at else ""},
        )
        citations = _dedupe(_citation_items(message), "chunk_id", "file_id", "char_start", "char_end")
        citations_by_message[message.id] = citations
        action_ids_by_message[message.id] = _walk_ids(message.tool_results or [], "log_id")
        object_ids_by_message[message.id] = _ontology_object_ids(message)
        for citation in citations:
            chunk_id = str(citation.get("chunk_id") or "")
            file_id = str(citation.get("file_id") or "")
            source_id = str(citation.get("data_source_id") or "")
            if source_id:
                source = db.get(DataSource, source_id)
                # 历史引用不能绕过当前资料库/场景权限。撤销公开或场景读权限后，
                # 图上不展示该资料节点、片段或其到 AI 回答的边。
                if not source or not tenant_service.get_visible(db, DataSource, source_id):
                    continue
                if source.scenario_id:
                    source_scenario = db.get(BusinessScenario, source.scenario_id)
                    if source_scenario and not permission_service.check_scenario(db, source_scenario, "read").allowed:
                        continue
                source_node = add_node(
                    "data_source",
                    source_id,
                    source.name if source else (citation.get("data_source_name") or source_id),
                    {"data_source_id": source_id, "type": source.type if source else "file_bucket"},
                )
            else:
                source_node = ""
            if file_id:
                file = db.get(BucketFile, file_id)
                file_node = add_node(
                    "document",
                    file_id,
                    file.filename if file else (citation.get("filename") or file_id),
                    {"file_id": file_id, "data_source_id": source_id},
                )
                if source_node:
                    add_edge(source_node, file_node, "contains")
            else:
                file_node = ""
            if chunk_id:
                chunk = db.get(DocumentChunk, chunk_id)
                chunk_node = add_node(
                    "document_chunk",
                    chunk_id,
                    f"片段 {citation.get('ordinal', '')}".strip(),
                    {
                        "chunk_id": chunk_id,
                        "file_id": file_id,
                        "char_start": citation.get("char_start", chunk.char_start if chunk else None),
                        "char_end": citation.get("char_end", chunk.char_end if chunk else None),
                    },
                )
                if file_node:
                    add_edge(file_node, chunk_node, "chunked_as")
                add_edge(chunk_node, answer_node, "cited_by", citation.get("citation_id", "引用"))
            elif file_node:
                add_edge(file_node, answer_node, "cited_by", citation.get("citation_id", "引用"))

    # Global assistant threads are personal.  Their derived lineage is useful
    # to the creator, but must not reveal another member's private page context,
    # attachment use or document-reading pattern merely because both members
    # can view the same scenario.
    current_tenant_id = tenant_service.current_tenant_id(db)
    current_user_id = permission_service.require_principal(db).user_id
    assistant_messages = db.execute(
        select(AssistantMessage)
        .join(AssistantThread, AssistantThread.id == AssistantMessage.thread_id)
        .where(
            AssistantThread.scenario_id == scenario_id,
            AssistantThread.tenant_id == current_tenant_id,
            AssistantThread.created_by_user_id == current_user_id,
            AssistantMessage.role == "assistant",
        )
        .order_by(AssistantMessage.created_at.desc())
        .limit(max_items + 1)
    ).scalars().all()
    if len(assistant_messages) > max_items:
        truncated = True
        assistant_messages = assistant_messages[:max_items]
    for message in assistant_messages:
        source_cards = message.attachments if isinstance(message.attachments, list) else []
        rag_sources = [card for card in source_cards if _is_assistant_rag_source(card)]
        # Only RAG-backed global answers participate in document lineage.  A
        # malformed/stale one invalidates the entire answer, matching history
        # redaction and preventing its generic node from becoming a side-channel.
        if not rag_sources:
            continue
        resolved_sources = [
            (card, _current_assistant_rag_source(db, scenario_id, card))
            for card in rag_sources
        ]
        if any(resolved is None for _card, resolved in resolved_sources):
            continue

        answer_node = add_node(
            "ai_answer",
            f"assistant:{message.id}",
            "全局助手回答",
            {
                "message_id": message.id,
                "assistant_thread_id": message.thread_id,
                "created_at": message.created_at.isoformat() if message.created_at else "",
                "source": "global_assistant",
            },
        )
        for card, resolved in resolved_sources:
            # ``resolved`` is non-None after the all() check above.
            source, bucket_file, chunk = resolved  # type: ignore[misc]
            source_node = add_node(
                "data_source",
                source.id,
                source.name,
                {"data_source_id": source.id, "type": source.type, "status": source.status or "unknown"},
            )
            file_node = add_node(
                "document",
                bucket_file.id,
                bucket_file.filename,
                {"file_id": bucket_file.id, "data_source_id": source.id},
            )
            add_edge(source_node, file_node, "contains")
            citation_label = str(card.get("citation_id") or card.get("id") or "引用")
            if chunk:
                chunk_node = add_node(
                    "document_chunk",
                    chunk.id,
                    f"片段 {chunk.ordinal}",
                    {
                        "chunk_id": chunk.id,
                        "file_id": bucket_file.id,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                    },
                )
                add_edge(file_node, chunk_node, "chunked_as")
                add_edge(chunk_node, answer_node, "cited_by", citation_label)
            else:
                add_edge(file_node, answer_node, "cited_by", citation_label)

    definition_cache: dict[tuple[str, str, str, str, str], Any | None] = {}
    # Runtime-derived lineage can contain execution summaries and is therefore
    # scoped to this deployment just like task and execution-log endpoints.
    # Governance/release history remains a separate cross-environment surface.
    deployment_environment = runtime_connector_service.runtime_environment()
    action_logs = db.execute(
        select(ActionExecutionLog)
        .where(
            ActionExecutionLog.scenario_id == scenario_id,
            ActionExecutionLog.environment == deployment_environment,
        )
        .order_by(ActionExecutionLog.created_at.desc())
        .limit(max_items + 1)
    ).scalars().all()
    if len(action_logs) > max_items:
        truncated = True
        action_logs = action_logs[:max_items]
    for log in action_logs:
        resource, definition = _resource_for_execution_log(
            db,
            log,
            definition_cache,
        )
        # A frozen record without a valid pinned resource is not rendered at
        # all.  Showing ``log.target_name`` here would silently substitute a
        # mutable or stale label for the definition that actually ran.
        if not resource:
            continue
        target_kind = str(log.target_type)
        target_name = str(resource.name or log.target_id)
        provenance = _definition_meta(log, definition)
        if target_kind == "workflow":
            target_meta = {
                "workflow_id": resource.id,
                "status": resource.status,
                **provenance,
            }
        elif target_kind == "action":
            target_meta = {
                "action_id": resource.id,
                "executor_type": resource.executor_type,
                **provenance,
            }
        else:
            target_meta = {
                "rule_id": resource.id,
                "severity": resource.severity,
                **provenance,
            }
        target_node = add_node(
            target_kind,
            _versioned_resource_id(log.target_id, definition),
            target_name,
            target_meta,
        )
        log_node = add_node(
            "action_execution",
            log.id,
            f"{target_name} · {log.status}",
            {
                "log_id": log.id,
                "status": log.status,
                "duration_ms": log.duration_ms,
                **provenance,
            },
        )
        add_edge(
            target_node,
            log_node,
            "executed_as",
            log.mode or "execute",
            provenance,
        )
        result_node = add_node(
            "external_result",
            log.id,
            "外部执行结果",
            {"log_id": log.id, "status": log.status, "has_error": bool(log.error)},
        )
        # The external-result edge is the most direct path users inspect in
        # the graph.  Carry the same immutable-definition evidence as the
        # execution edge so a result cannot be mistaken for a live definition.
        add_edge(log_node, result_node, "returned", log.status, provenance)

    workflow_runs = db.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.scenario_id == scenario_id,
            WorkflowRun.environment == deployment_environment,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(max_items + 1)
    ).scalars().all()
    if len(workflow_runs) > max_items:
        truncated = True
        workflow_runs = workflow_runs[:max_items]
    for run in workflow_runs:
        workflow, definition = _workflow_for_run_lineage(
            db,
            run,
            definition_cache,
        )
        if not workflow:
            continue
        provenance = _definition_meta(run, definition)
        workflow_node = add_node(
            "workflow",
            _versioned_resource_id(run.workflow_id, definition),
            str(workflow.name or run.workflow_id),
            {
                "workflow_id": workflow.id,
                "status": workflow.status,
                **provenance,
            },
        )
        run_node = add_node(
            "workflow_run",
            run.id,
            f"工作流运行 · {run.status}",
            {
                "workflow_run_id": run.id,
                "workflow_id": run.workflow_id,
                "status": run.status,
                **provenance,
            },
        )
        add_edge(workflow_node, run_node, "queued_as", "", provenance)
        for log_id in _walk_ids(run.result or {}, "log_id"):
            add_edge(
                run_node,
                _node_id("action_execution", log_id),
                "orchestrated",
                meta=provenance,
            )

    for message_id, log_ids in action_ids_by_message.items():
        answer_node = _node_id("ai_answer", message_id)
        for log_id in log_ids:
            add_edge(answer_node, _node_id("action_execution", log_id), "requested_action")

    for message_id, object_ids in object_ids_by_message.items():
        answer_node = _node_id("ai_answer", message_id)
        for object_id in object_ids:
            # search_ontology 的工具结果可能来自历史会话；只对当前仍可读取且属于
            # 当前图的对象建立血缘，避免变成对象 ACL 的侧信道。
            instance = visible_objects.get(object_id) or db.get(OntologyInstance, object_id)
            if not instance or instance.scenario_id != scenario_id:
                continue
            if not permission_service.check_object(db, instance, "read").allowed:
                continue
            object_node = _node_id("object", instance.id)
            if object_node not in nodes:
                object_node = add_node(
                    "object",
                    instance.id,
                    instance.name,
                    {"object_id": instance.id, "entity_id": instance.entity_id, "source": instance.source or "manual"},
                )
            add_edge(object_node, answer_node, "used_as_context")

    return {
        "scenario_id": scenario_id,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "truncated": truncated,
        "summary": {
            "data_sources": sum(1 for node in nodes.values() if node["kind"] == "data_source"),
            "objects": sum(1 for node in nodes.values() if node["kind"] == "object"),
            "ai_answers": sum(1 for node in nodes.values() if node["kind"] == "ai_answer"),
            "action_executions": sum(1 for node in nodes.values() if node["kind"] == "action_execution"),
        },
    }
