"""P1 端到端血缘图构建。

该服务不把业务原文、Action 输入参数或外部系统返回全文暴露给图接口。它只汇总
已经持久化的稳定标识与安全摘要，从而把数据源、映射、对象、AI 回答、Action 及
外部结果串成可导航的有向图。图本身是派生视图：即使运行记录来自旧版本，也能
安全地展示可推导的部分血缘。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    BucketFile,
    Conversation,
    DataMapping,
    DataSource,
    DocumentChunk,
    Message,
    OntologyAction,
    OntologyInstance,
    OntologyWorkflow,
    WorkflowRun,
)
from . import permission_service


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
        .limit(max_items)
    ).scalars().all()
    for instance in instances:
        # 对象级 ACL 必须在派生图中同样生效；否则只读血缘会成为绕过对象详情
        # 接口的侧信道。
        if not permission_service.check_object(db, instance, "read").allowed:
            continue
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
        .limit(max_items)
    ).scalars().all()
    citations_by_message: dict[str, list[dict[str, Any]]] = {}
    action_ids_by_message: dict[str, set[str]] = {}
    for message in messages:
        answer_node = add_node(
            "ai_answer",
            message.id,
            (message.content or "AI 回答").strip().replace("\n", " ")[:80] or "AI 回答",
            {"message_id": message.id, "created_at": message.created_at.isoformat() if message.created_at else ""},
        )
        citations = _dedupe(_citation_items(message), "chunk_id", "file_id", "char_start", "char_end")
        citations_by_message[message.id] = citations
        action_ids_by_message[message.id] = _walk_ids(message.tool_results or [], "log_id")
        for citation in citations:
            chunk_id = str(citation.get("chunk_id") or "")
            file_id = str(citation.get("file_id") or "")
            source_id = str(citation.get("data_source_id") or "")
            if source_id:
                source = db.get(DataSource, source_id)
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

    action_logs = db.execute(
        select(ActionExecutionLog)
        .where(ActionExecutionLog.scenario_id == scenario_id)
        .order_by(ActionExecutionLog.created_at.desc())
        .limit(max_items)
    ).scalars().all()
    for log in action_logs:
        action = db.get(OntologyAction, log.target_id) if log.target_type == "action" else None
        workflow = db.get(OntologyWorkflow, log.target_id) if log.target_type == "workflow" else None
        # Action / 工作流的执行日志可能泄露外部系统是否执行成功。沿用资源本身的
        # read 判定；未知历史 target 只会在已经通过场景读权限的当前租户中显示。
        if action and not permission_service.check_action(db, action, "read").allowed:
            continue
        if workflow and not permission_service.check_workflow(db, workflow, "read").allowed:
            continue
        target_kind = "workflow" if workflow else "action"
        target_name = workflow.name if workflow else (action.name if action else (log.target_name or log.target_id))
        target_meta = (
            {"workflow_id": workflow.id, "status": workflow.status}
            if workflow
            else {"action_id": log.target_id, "executor_type": action.executor_type if action else ""}
        )
        action_node = add_node(
            target_kind,
            log.target_id,
            target_name,
            target_meta,
        )
        log_node = add_node(
            "action_execution",
            log.id,
            f"{log.target_name or log.target_id} · {log.status}",
            {"log_id": log.id, "status": log.status, "duration_ms": log.duration_ms},
        )
        add_edge(action_node, log_node, "executed_as", log.mode or "execute")
        result_node = add_node(
            "external_result",
            log.id,
            "外部执行结果",
            {"log_id": log.id, "status": log.status, "has_error": bool(log.error)},
        )
        add_edge(log_node, result_node, "returned", log.status)

    workflow_runs = db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.scenario_id == scenario_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(max_items)
    ).scalars().all()
    for run in workflow_runs:
        workflow = run.workflow or db.get(OntologyWorkflow, run.workflow_id)
        if workflow and not permission_service.check_workflow(db, workflow, "read").allowed:
            continue
        run_node = add_node(
            "workflow_run",
            run.id,
            f"工作流运行 · {run.status}",
            {"workflow_run_id": run.id, "workflow_id": run.workflow_id, "status": run.status},
        )
        for log_id in _walk_ids(run.result or {}, "log_id"):
            add_edge(run_node, _node_id("action_execution", log_id), "orchestrated")

    for message_id, log_ids in action_ids_by_message.items():
        answer_node = _node_id("ai_answer", message_id)
        for log_id in log_ids:
            add_edge(answer_node, _node_id("action_execution", log_id), "requested_action")

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
