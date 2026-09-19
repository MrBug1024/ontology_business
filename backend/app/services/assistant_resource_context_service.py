"""Read selected advisor methods and MCP declarations without executing them."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MCPConfig, Skill
from . import mcp_bounded_transport_service, modeling_reference_contract, permission_service, release_service, skill_instruction_service, tenant_service

MAX_SKILL_SELECTION = 5
MAX_MCP_SELECTION = 5
MAX_CATALOG_TOOLS = 40
MAX_CATALOG_BYTES = 32_000
MAX_TOTAL_READ_SECONDS = 20.0
MAX_MCP_RESPONSE_BYTES = 128_000
REMOTE_TRANSPORTS = frozenset({"sse", "streamable_http", "http"})
SUPPORTED_INTENTS = frozenset({"chat", "explain", "scenario", "ontology", "mapping", "workflow", "scenario_model"})
UNAVAILABLE = "所选参考资源当前不可用，请刷新后重新选择"


class AssistantResourceUnavailable(ValueError):
    """Safe, actionable failure; external errors never enter this message."""


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    id: str
    name: str
    source: str
    path: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpSnapshot:
    id: str
    name: str
    transport: str
    revision: int
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    protected_values: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SelectedResources:
    skills: tuple[SkillSnapshot, ...] = ()
    mcps: tuple[McpSnapshot, ...] = ()


def _rows(db: Session, model, ids: list[str]):
    if not ids:
        return []
    rows = db.execute(select(model).where(model.id.in_(ids), model.enabled.is_(True),
        tenant_service.visible_clause(model, db)).order_by(model.id).execution_options(populate_existing=True)).scalars().all()
    if {row.id for row in rows} != set(ids):
        raise AssistantResourceUnavailable(UNAVAILABLE)
    return rows


def _protected_values(row: MCPConfig) -> tuple[str, ...]:
    values = {str(value) for value in [row.url, *(row.headers or {}).values(), *(row.env or {}).values()] if value}
    for value in tuple(values):
        scheme, separator, token = value.partition(" ")
        if separator and scheme.lower() in {"bearer", "basic"} and token.strip():
            values.add(token.strip())
    return tuple(sorted(values))


def resolve(db: Session, skill_ids: list[str], mcp_ids: list[str]) -> SelectedResources:
    if not skill_ids and not mcp_ids:
        return SelectedResources()
    if len(skill_ids) > MAX_SKILL_SELECTION or len(mcp_ids) > MAX_MCP_SELECTION:
        raise AssistantResourceUnavailable("每轮最多选择 5 个技能和 5 个 MCP，请减少所选资源")
    if len(set(skill_ids)) != len(skill_ids) or len(set(mcp_ids)) != len(mcp_ids):
        raise AssistantResourceUnavailable("所选建模参考不能重复")
    permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "read")
    skills, mcps = _rows(db, Skill, skill_ids), _rows(db, MCPConfig, mcp_ids)
    if any(skill.source != "builtin" for skill in skills):
        raise AssistantResourceUnavailable("建模顾问仅支持受信内置技能方法，请重新选择")
    if any(mcp.transport not in REMOTE_TRANSPORTS for mcp in mcps):
        raise AssistantResourceUnavailable("建模顾问仅支持远程 MCP 工具契约目录，请重新选择")
    return SelectedResources(
        tuple(SkillSnapshot(row.id, row.name, row.source, row.path) for row in skills),
        tuple(McpSnapshot(row.id, row.name, row.transport, int(row.connector_revision), row.url,
            tuple(sorted((str(key), str(value)) for key, value in (row.headers or {}).items())),
            _protected_values(row))
            for row in mcps),
    )


def revalidate(db: Session, selected: SelectedResources) -> None:
    permission_service.refresh_request_authorization(db)
    current = resolve(db, [item.id for item in selected.skills], [item.id for item in selected.mcps])
    if current != selected:
        raise AssistantResourceUnavailable("建模参考配置在读取期间已变化，请重新发送")


def _safe(value: object, protected: tuple[str, ...] = ()) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise AssistantResourceUnavailable("建模参考内容格式无效") from None
    wrapped = {"reference": value}
    if release_service.safe_snapshot_content(wrapped) != wrapped or any(
        secret in encoded or json.dumps(secret, ensure_ascii=False)[1:-1] in encoded for secret in protected
    ):
        raise AssistantResourceUnavailable("建模参考含连接信息或敏感内容，已拒绝加载")
    return encoded


def _catalog(tools: object, selected: McpSnapshot) -> str:
    if not isinstance(tools, list) or len(tools) > MAX_CATALOG_TOOLS:
        raise AssistantResourceUnavailable("MCP 工具目录超过 40 项读取上限，请使用范围更小的连接")
    records = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"] or len(tool["name"]) > 200:
            raise AssistantResourceUnavailable("MCP 工具目录缺少有效名称")
        description = tool.get("description", "")
        schema = tool.get("input_schema", {})
        if not isinstance(description, str) or len(description) > 4_000 or not isinstance(schema, dict):
            raise AssistantResourceUnavailable("MCP 工具契约格式或大小不适用")
        records.append({"name": tool["name"], "description": description, "input_schema": schema})
    if len({item["name"] for item in records}) != len(records):
        raise AssistantResourceUnavailable("MCP 工具目录包含重复名称，无法确定契约")
    records.sort(key=lambda item: item["name"])
    encoded = _safe(records, selected.protected_values)
    if len(encoded.encode("utf-8")) > MAX_CATALOG_BYTES:
        raise AssistantResourceUnavailable("MCP 工具契约超过 32KB 读取上限，请缩小目录")
    return encoded


async def _read(selected: SelectedResources) -> dict:
    references = []
    for item in selected.skills:
        skill = Skill(id=item.id, name=item.name, source=item.source, path=item.path)
        instructions = await asyncio.to_thread(skill_instruction_service.read_instructions, skill)
        _safe({"name": item.name, "instructions": instructions})
        references.append(modeling_reference_contract.ModelingReference(kind="skill_method", resource_id=item.id,
            name=item.name, content=instructions, content_hash=hashlib.sha256(instructions.encode("utf-8")).hexdigest()))
    for item in selected.mcps:
        config = MCPConfig(id=item.id, name=item.name, transport=item.transport, url=item.url, headers=dict(item.headers))
        tools = await _list_mcp_tools(config)
        content = _catalog(tools, item)
        _safe({"name": item.name}, item.protected_values)
        references.append(modeling_reference_contract.ModelingReference(kind="mcp_tool_catalog", resource_id=item.id,
            name=item.name, revision=item.revision, content=content, content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()))
    return modeling_reference_contract.freeze(references)


async def _list_mcp_tools(config: MCPConfig) -> list[dict]:
    async with mcp_bounded_transport_service.bounded_session(config, timeout_seconds=MAX_TOTAL_READ_SECONDS,
        max_response_bytes=MAX_MCP_RESPONSE_BYTES, max_sse_frame_bytes=MAX_MCP_RESPONSE_BYTES) as session:
        result = await session.list_tools()
        if result.nextCursor or len(result.tools) > MAX_CATALOG_TOOLS:
            raise AssistantResourceUnavailable("MCP 工具目录超过单轮范围，请使用更小的契约目录")
        return [{"name": item.name, "description": item.description or "", "input_schema": item.inputSchema or {}}
            for item in result.tools]


def read(selected: SelectedResources) -> dict:
    if not selected.skills and not selected.mcps:
        return {}
    try:
        return asyncio.run(asyncio.wait_for(_read(selected), MAX_TOTAL_READ_SECONDS))
    except AssistantResourceUnavailable:
        raise
    except Exception:
        raise AssistantResourceUnavailable("所选技能方法或 MCP 工具契约读取失败或超时，请检查平台设置后重试") from None


def sources(document: dict) -> list[dict]:
    verified = modeling_reference_contract.normalize(document)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    return [{"id": item["resource_id"], "kind": item["kind"], "title": item["name"], "filename": item["name"],
        "snippet": "已读取技能方法，未执行脚本" if item["kind"] == "skill_method" else "已读取 MCP 工具契约目录，未调用工具",
        "content_hash": item["content_hash"], "revision": item["revision"], "retrieved_at": retrieved_at,
        "usage_plane": "modeling_reference", "formalization_allowed": False}
        for item in verified.get("references", [])]
