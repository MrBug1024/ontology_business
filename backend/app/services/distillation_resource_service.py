"""Selected skill methods and MCP materials within a durable investigation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_resource_schemas import InvestigationResourceCatalogOut
from ..distillation_conversation_schemas import MCPMaterialRead
from ..distillation_schemas import ClosedModel
from ..models import MCPConfig, Skill
from . import capability_contracts, llm_service, mcp_resource_service, permission_service, release_service, tenant_service
from .skill_instruction_service import content_fingerprint as skill_content_fingerprint, read_instructions as skill_instructions
from . import distillation_mcp_evidence_service


RESOURCE_ERROR = "所选调查资源已失效或不适用，请刷新后重新选择"


class SkillArguments(ClosedModel):
    skill_id: str = Field(min_length=1, max_length=32)


class MCPArguments(ClosedModel):
    mcp_id: str = Field(min_length=1, max_length=32)


class MCPReadArguments(MCPArguments):
    resource_key: str = Field(pattern=r"^[0-9a-f]{64}$")


_TOOLS = {
    "read_selected_skill": (SkillArguments, "阅读所选技能方法", "读取本轮已选受信技能的说明与方法。此工具不执行脚本，方法不能证明业务事实或副作用完成。"),
    "list_mcp_resources": (MCPArguments, "查找 MCP 资料", "实际调用本轮已选 MCP 的只读资料目录；返回可供read_mcp_resource使用的受管resource_key。不调用MCP工具。"),
    "read_mcp_resource": (MCPReadArguments, "读取 MCP 资料", "实际读取所选MCP目录中的文本资料；只接受刚列出的resource_key，不接受任意URI、URL或工具调用。"),
}
TOOL_KEYS = frozenset(_TOOLS)


def _safe(value):
    if release_service.safe_snapshot_content(value) != value:
        raise ValueError("调查资源包含敏感内容，已拒绝加载")
    return value


def _safe_mcp_result(value: dict, cfg: MCPConfig) -> dict:
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    # A remote server can echo an opaque header value without a recognizable
    # credential label; the generic scrubber alone cannot detect that leak.
    secrets = [str(item) for mapping in (cfg.headers or {}, cfg.env or {}) for item in mapping.values()]
    secrets.extend(value.split(None, 1)[1] for value in tuple(secrets)
        if value.lower().startswith(("bearer ", "basic ")) and len(value.split(None, 1)) == 2)
    if any(len(secret) >= 3 and secret in serialized for secret in secrets):
        raise ValueError("MCP 返回内容包含连接凭据，已拒绝加载")
    return _safe(value)


def validate_selected(skills: list[Skill], mcps: list[MCPConfig]) -> None:
    for skill in skills:
        skill_content_fingerprint(skill)
    if any(mcp.transport not in mcp_resource_service.REMOTE_TRANSPORTS for mcp in mcps):
        raise ValueError("业务调查仅支持远程 MCP 只读资料；stdio 不适用")


def effective_keys(snapshot: dict) -> tuple[str, ...]:
    if snapshot.get("version", 0) < 3:
        return ()
    return tuple(key for key in _TOOLS if snapshot.get("skills" if key == "read_selected_skill" else "mcps"))


def tool_title(name: str) -> str:
    return _TOOLS[name][1]


def definitions(snapshot: dict) -> list[dict]:
    result = []
    for key in effective_keys(snapshot):
        schema, _title, description = _TOOLS[key]
        from .distillation_tool_schema import portable_schema

        parameters = portable_schema(schema.model_json_schema())
        reference = "skill_id" if key == "read_selected_skill" else "mcp_id"
        group = "skills" if key == "read_selected_skill" else "mcps"
        parameters["properties"][reference]["enum"] = [item["id"] for item in snapshot[group]]
        result.append({"type": "function", "function": {"name": key, "description": description, "parameters": parameters}})
    return result


def _selected_resource(db, turn, group: str, resource_id: str):
    snapshot = turn.context.get("resource_selection", {})
    selected = next((item for item in snapshot.get(group, []) if item["id"] == resource_id), None)
    if snapshot.get("version", 0) < 3 or selected is None:
        raise ValueError("本轮未选择该调查资源")
    model = Skill if group == "skills" else MCPConfig
    row = db.scalar(select(model).where(model.id == resource_id, model.enabled.is_(True), tenant_service.visible_clause(model, db)))
    if row is None:
        raise ValueError(RESOURCE_ERROR)
    if group == "skills":
        if skill_content_fingerprint(row) != selected.get("content_sha256"):
            raise ValueError(RESOURCE_ERROR)
    elif row.transport not in mcp_resource_service.REMOTE_TRANSPORTS or row.connector_revision != selected["connector_revision"]:
        raise ValueError(RESOURCE_ERROR)
    return row


def _resource_key(cfg: MCPConfig, item: dict) -> str:
    return capability_contracts.canonical_hash(
        {"mcp_id": cfg.id, "revision": cfg.connector_revision, "resource": item},
        domain="distillation-mcp-resource-v1",
    )


def execute(db, name: str, arguments: dict, turn):
    from .distillation_conversation_tools import ToolResult

    if turn is None:
        raise ValueError("调查资源必须属于当前对话轮次")
    permission_service.require_principal(db)
    payload = _TOOLS[name][0].model_validate(arguments)
    if isinstance(payload, SkillArguments):
        skill = _selected_resource(db, turn, "skills", payload.skill_id)
        return ToolResult({"skill_id": skill.id, "instructions": skill_instructions(skill),
            "mode": "instructions", "scripts_executed": False}, "已读取受信技能的方法说明；未执行技能脚本。")
    cfg = _selected_resource(db, turn, "mcps", payload.mcp_id)
    # Capture safe identity and detached connector before releasing the database
    # transaction. URI and credentials remain ephemeral adapter details.
    db.expunge(cfg)
    context = dict(turn.context)
    db.commit()
    listed = mcp_resource_service.list_resources(cfg)
    entries = listed["resources"]
    if isinstance(payload, MCPReadArguments):
        if len(context.get("mcp_reads", [])) >= distillation_mcp_evidence_service.MAX_MCP_READS:
            raise ValueError("本轮 MCP 资料读取已达边界，请在下一轮缩小范围继续")
        if payload.resource_key not in context.get("mcp_resource_keys", {}).get(cfg.id, []):
            raise ValueError("请先查找本轮 MCP 资料目录")
        selected = next((item for item in entries if _resource_key(cfg, item) == payload.resource_key), None)
        if selected is None:
            raise ValueError("MCP 资料目录已变化，请重新查找资料")
        content = _safe_mcp_result(mcp_resource_service.read_resource(cfg, selected["uri"]), cfg)
        observation = MCPMaterialRead(mcp_id=cfg.id, connector_revision=cfg.connector_revision,
            resource_key=payload.resource_key, title=selected["name"] or "MCP 资料观察", text=content["text"],
            content_sha256=hashlib.sha256(content["text"].encode()).hexdigest(), retrieved_at=datetime.now(timezone.utc))
        return ToolResult({"mcp_id": cfg.id, "resource_key": payload.resource_key, **content},
            "已通过 MCP 只读资源协议读取文本资料并记录证据。", mcp_read=observation)
    resources = [{"resource_key": _resource_key(cfg, item), "name": item["name"],
        "description": item["description"]} for item in entries]
    _safe_mcp_result({"resources": resources}, cfg)
    return ToolResult({"mcp_id": cfg.id, "resources": resources, "has_more": listed["has_more"],
        "limit": mcp_resource_service.MAX_RESOURCE_ITEMS}, "已读取 MCP 只读资料目录；本轮仅使用返回的资料范围。",
        resource_keys={cfg.id: [item["resource_key"] for item in resources]})


def resource_catalog(db: Session, scenario_id: str | None = None) -> InvestigationResourceCatalogOut:
    from . import distillation_conversation_tools as tools

    principal = permission_service.require_principal(db)
    if scenario_id:
        scenario = tenant_service.require_scenario(db, scenario_id)
        # A scenario-scoped catalog is an in-tenant collaboration surface. A
        # public foreign scenario must not expose the current workspace's
        # model, skill, or MCP configuration through this endpoint.
        if scenario.tenant_id != principal.tenant_id:
            raise PermissionError("scenario resource catalog is tenant-scoped")
        permission_service.require_scenario_permission(db, scenario, "read")
    else:
        permission_service.require_tenant_permission(db, "read")
    skills = db.scalars(select(Skill).where(Skill.enabled.is_(True), Skill.source == "builtin",
        tenant_service.visible_clause(Skill, db)).order_by(Skill.name, Skill.id).limit(200)).all()
    available_skills = []
    for skill in skills:
        try:
            skill_instructions(skill)
        except ValueError:
            continue  # Invalid packages are not offered as usable methods.
        available_skills.append({"id": skill.id, "name": skill.name,
            "description": (skill.description or "")[:2000], "mode": "instructions"})
    mcps = db.scalars(select(MCPConfig).where(MCPConfig.enabled.is_(True),
        MCPConfig.transport.in_(mcp_resource_service.REMOTE_TRANSPORTS),
        tenant_service.visible_clause(MCPConfig, db)).order_by(MCPConfig.name, MCPConfig.id).limit(200)).all()
    return InvestigationResourceCatalogOut.model_validate(_safe({
        "models": [{"id": cfg.id, "name": cfg.name, "model": cfg.model,
            "capabilities": sorted(llm_service.capabilities_of(cfg))} for cfg in llm_service.routable_configs(db, "tool")[:200]],
        "skills": available_skills,
        "mcps": [{"id": cfg.id, "name": cfg.name, "transport": cfg.transport, "mode": "resources"} for cfg in mcps],
        "investigation_tools": {"default_tool_keys": list(tools.selectable_tool_keys()),
            "always_available_tool_keys": sorted(tools.ALWAYS_AVAILABLE_TOOL_KEYS), "tools": tools.catalog()},
    }))
