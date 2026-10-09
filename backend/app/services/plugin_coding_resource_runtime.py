"""Fenced coding reads with opaque MCP resource keys and durable operation budgets."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from pydantic import Field
from sqlalchemy import select

from ..models import MCPConfig
from . import mcp_resource_service, permission_service, plugin_coding_tools
from .capability_contracts import canonical_hash
from .plugin_coding_resources import RESOURCE_ERROR, _safe
from .plugin_coding_tools import EmptyArguments


MAX_TOOL_OPERATIONS = 24
MAX_MCP_READS = 6
MAX_TOOL_RESULT_BYTES = 256 * 1024


class MCPArguments(EmptyArguments):
    mcp_id: str = Field(min_length=1, max_length=32)


class MCPReadArguments(MCPArguments):
    resource_key: str = Field(pattern=r"^[a-f0-9]{64}$")


MCP_TOOLS = {
    "list_coding_mcp_resources": (MCPArguments, "查找 MCP 编程参考", "调用已安装 MCP 的 resources/list。返回本轮受管 resource_key，不返回 URI，不调用外部业务工具。"),
    "read_coding_mcp_resource": (MCPReadArguments, "读取 MCP 编程参考", "通过 resources/read 读取刚列出的受管 resource_key 对应文本；不接受 URI、URL、脚本或任意工具调用。"),
}


def definitions(document: dict) -> list[dict]:
    result = plugin_coding_tools.definitions()
    installed = document.get("resource_snapshot", {}).get("mcps", [])
    if installed:
        for key, (schema, _title, description) in MCP_TOOLS.items():
            parameters = schema.model_json_schema()
            parameters["properties"]["mcp_id"]["enum"] = [item["id"] for item in installed]
            result.append({"type": "function", "function": {"name": key, "description": description,
                           "parameters": parameters}})
    return result


def reserve_operation(document: dict, name: str, run_id: str) -> None:
    if document.get("active_run_id") != run_id or document["phase"] != "generating":
        raise ValueError("旧编码轮次已失效")
    if name not in plugin_coding_tools.BASE_TOOLS and name not in MCP_TOOLS:
        raise ValueError("模型请求了不可用的编程工具")
    budget = dict(document.get("coding_tool_budget", {}))
    if budget.get("run_id") != run_id:
        budget = {"run_id": run_id, "operations": 0, "mcp_reads": 0, "result_bytes": 0}
    if budget["operations"] >= MAX_TOOL_OPERATIONS:
        raise ValueError("本轮编程工具操作已达预算，请缩小目标后继续")
    if name == "read_coding_mcp_resource":
        if budget["mcp_reads"] >= MAX_MCP_READS:
            raise ValueError("本轮 MCP 资料读取已达预算")
        budget["mcp_reads"] += 1
    budget["operations"] += 1
    document["coding_tool_budget"] = budget


def _safe_mcp_result(value: dict, cfg) -> dict:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    secrets = [str(item) for mapping in (cfg.headers or {}, cfg.env or {}) for item in mapping.values()]
    secrets.extend(secret.split(None, 1)[1] for secret in tuple(secrets)
                   if secret.lower().startswith(("bearer ", "basic ")) and len(secret.split(None, 1)) == 2)
    if any(secret and secret in encoded for secret in secrets):
        raise ValueError("MCP 返回内容包含连接凭据，已拒绝加载")
    return _safe(value)


def _resource_key(cfg, item: dict) -> str:
    return canonical_hash({"mcp_id": cfg.id, "revision": cfg.connector_revision, "resource": item},
                          domain="plugin-coding-mcp-resource-v1")


def execute(db, document: dict, name: str, arguments: dict, *, deadline: float | None = None) -> tuple[dict, dict]:
    if name in plugin_coding_tools.BASE_TOOLS:
        try:
            content = plugin_coding_tools.execute(document, name, arguments)
        except plugin_coding_tools.CodingToolInputError as error:
            return error.result(), {'title': plugin_coding_tools.BASE_TOOLS[name][1],
                                    'rejected_code': error.code, 'message': error.message}
        return content, {"title": plugin_coding_tools.BASE_TOOLS[name][1]}
    if name not in MCP_TOOLS:
        raise ValueError("模型请求了不可用的编程工具")
    payload = MCP_TOOLS[name][0].model_validate(arguments)
    installed = next((item for item in document.get("resource_snapshot", {}).get("mcps", [])
                      if item["id"] == payload.mcp_id), None)
    if installed is None:
        raise ValueError("本项目未安装该 MCP 编程连接")
    principal = permission_service.require_principal(db)
    cfg = db.scalar(select(MCPConfig).where(MCPConfig.id == payload.mcp_id,
        MCPConfig.tenant_id == principal.tenant_id, MCPConfig.enabled.is_(True)))
    if (cfg is None or cfg.transport not in mcp_resource_service.REMOTE_TRANSPORTS
            or cfg.connector_revision != installed["connector_revision"]):
        raise ValueError(RESOURCE_ERROR)
    if isinstance(payload, MCPReadArguments):
        if payload.resource_key not in document.get("coding_resource_keys", {}).get(cfg.id, []):
            raise ValueError("请先查找本轮 MCP 编程参考目录")
    db.expunge(cfg)
    db.rollback()  # Never hold a database transaction across remote I/O.
    def remaining():
        if deadline is None:
            return None
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ValueError("编程工具时间预算已耗尽")
        return seconds
    listed = mcp_resource_service.list_resources(cfg, timeout_seconds=remaining())
    if isinstance(payload, MCPReadArguments):
        selected = next((item for item in listed["resources"] if _resource_key(cfg, item) == payload.resource_key), None)
        if selected is None:
            raise ValueError("MCP 编程参考目录已变化，请重新查找")
        content = _safe_mcp_result(mcp_resource_service.read_resource(cfg, selected["uri"], timeout_seconds=remaining()), cfg)
        return {"mcp_id": cfg.id, "resource_key": payload.resource_key, **content}, {
            "title": MCP_TOOLS[name][1],
            "content_sha256": hashlib.sha256(content["text"].encode("utf-8")).hexdigest()}
    resources = [{"resource_key": _resource_key(cfg, item), "name": item["name"],
                  "description": item["description"]} for item in listed["resources"]]
    content = _safe_mcp_result({"mcp_id": cfg.id, "resources": resources,
                                "has_more": listed["has_more"], "read_only": True}, cfg)
    return content, {"title": MCP_TOOLS[name][1],
                     "resource_keys": {cfg.id: [item["resource_key"] for item in resources]}}


def record_result(document: dict, *, name: str, content: dict, record: dict, run_id: str) -> dict:
    from .plugin_coding_workspace import append_event

    budget = dict(document["coding_tool_budget"])
    if document.get("active_run_id") != run_id or budget["run_id"] != run_id:
        raise ValueError("旧编码轮次已失效")
    encoded = json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")
    budget["result_bytes"] += len(encoded)
    if budget["result_bytes"] > MAX_TOOL_RESULT_BYTES:
        raise ValueError("本轮编程工具结果超过预算")
    document["coding_tool_budget"] = budget
    if record.get('rejected_code'):
        messages = {
            'unknown_project_path': '所选文件不在项目中，正在按目录重新选择',
            'duplicate_project_paths': '文件选择重复，正在重新选择',
            'invalid_tool_arguments': '参数未通过校验，正在修正',
        }
        append_event(document, 'tool_rejected', messages[record['rejected_code']])
        return content
    if record.get("resource_keys"):
        document["coding_resource_keys"] = {**document.get("coding_resource_keys", {}), **record["resource_keys"]}
    # Persist safe receipts and hashes, never reference content, URIs or credentials.
    receipt = {"tool": name, "title": record["title"], "read_only": True, "run_id": run_id,
               "content_sha256": record.get("content_sha256"), "retrieved_at": datetime.now(timezone.utc).isoformat()}
    _safe(receipt)
    document["resource_receipts"] = [*document.get("resource_receipts", []), receipt][-30:]
    reference = "讨论" if document.get("round_mode") == "discuss" else "编码"
    append_event(document, "tool", record["title"] + "已完成，只读结果供本轮" + reference + "参考")
    return content
