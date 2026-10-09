"""Authorize and freeze workspace-local coding extensions without exposing credentials."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from ..config import SKILLS_DIR
from ..models import AssistantRequestRun, LLMConfig, MCPConfig, Skill
from ..plugin_coding_schemas import CodingResourceCatalog, CodingResourceSelection
from . import llm_service, mcp_resource_service, permission_service, release_service, tenant_service
from .capability_contracts import canonical_hash
from .skill_instruction_service import read_instructions
from . import plugin_coding_tools
from .plugin_coding_validation import safe_text


AUTHORING_SKILL_VERSIONS = {
    "plugin-authoring": "1.0.0", "plugin-contract-review": "1.0.0",
    "plugin-codex-authoring": "1.0.0",
}
MAX_SKILL_CONTEXT_BYTES = 48_000
RESOURCE_ERROR = "编程扩展已失效或不适用，请刷新设置并重新安装"


def _safe(value):
    safe_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if release_service.safe_snapshot_content(value) != value:
        raise ValueError("编程扩展包含敏感内容，已拒绝加载")
    return value


def _skill_text(skill) -> str:
    expected = AUTHORING_SKILL_VERSIONS.get(skill.name)
    meta = skill.meta or {}
    metadata = meta.get('metadata', {})
    version = metadata.get('version') if isinstance(metadata, dict) and 'version' in metadata else meta.get('version', '')
    if (expected is None or str(version) != expected
            or Path(skill.path).resolve() != (SKILLS_DIR / skill.name).resolve()):
        raise ValueError(RESOURCE_ERROR)
    return read_instructions(skill)


def resource_catalog(db, scenario_id: str | None = None) -> dict:
    principal = permission_service.require_principal(db)
    if scenario_id:
        scenario, _ = release_service._scenario_for_manage(db, scenario_id)
        if scenario.tenant_id != principal.tenant_id:
            raise HTTPException(404, "编程资源不可用")
    else:
        permission_service.require_tenant_permission(db, "read")
    skills = db.scalars(select(Skill).where(Skill.enabled.is_(True), Skill.source == "builtin",
        Skill.name.in_(AUTHORING_SKILL_VERSIONS), tenant_service.visible_clause(Skill, db))
        .order_by(Skill.name).limit(10)).all()
    available = []
    for skill in skills:
        try:
            _skill_text(skill)
            option = _safe({"id": skill.id, "name": skill.name, "description": (skill.description or "")[:2000],
                           "version": AUTHORING_SKILL_VERSIONS[skill.name], "mode": "instructions"})
        except ValueError:
            continue
        available.append(option)
    mcps = db.scalars(select(MCPConfig).where(MCPConfig.tenant_id == principal.tenant_id,
        MCPConfig.enabled.is_(True), MCPConfig.transport.in_(mcp_resource_service.REMOTE_TRANSPORTS))
        .order_by(MCPConfig.name, MCPConfig.id).limit(200)).all()
    result = {"models": [{"id": cfg.id, "name": cfg.name, "model": cfg.model,
                          "supports_tools": llm_service.supports_capability(cfg, "tool")}
                         for cfg in llm_service.routable_configs(db, "chat")[:200]],
              "skills": available,
              "mcps": [{"id": cfg.id, "name": cfg.name,
                        "transport": "streamable_http" if cfg.transport == "http" else cfg.transport,
                        "mode": "read_only_resources"} for cfg in mcps],
              "base_tools": plugin_coding_tools.catalog()}
    return CodingResourceCatalog.model_validate(_safe(result)).model_dump(mode="json")


def selection(document: dict) -> dict:
    # Legacy projects used one explicit model and had no extensions. Missing
    # snapshots for an extension-bearing project must never become an empty set.
    value = document.get("resource_selection", {"llm_config_id": document["llm_config_id"],
                                              "skill_ids": [], "mcp_ids": []})
    configured = CodingResourceSelection.model_validate(value).model_dump(mode="json")
    if configured["llm_config_id"] != document["llm_config_id"]:
        raise ValueError(RESOURCE_ERROR)
    return configured


def freeze_selection(db, value: dict) -> tuple[dict, dict]:
    requested = CodingResourceSelection.model_validate(value)
    principal = permission_service.require_principal(db)
    cfg = tenant_service.get_visible(db, LLMConfig, requested.llm_config_id)
    if cfg is None or not llm_service.supports_capability(cfg, "chat"):
        raise HTTPException(409, "请配置当前工作区可用的编码对话模型")
    if requested.mcp_ids and not llm_service.supports_capability(cfg, "tool"):
        raise HTTPException(409, "安装 MCP 只读资源需要支持工具调用的编码模型")
    skills = []
    total = 0
    for resource_id in requested.skill_ids:
        skill = db.scalar(select(Skill).where(Skill.id == resource_id, Skill.enabled.is_(True),
            Skill.source == "builtin", tenant_service.visible_clause(Skill, db)))
        if skill is None:
            raise HTTPException(409, RESOURCE_ERROR)
        text = _skill_text(skill)
        total += len(text.encode("utf-8"))
        skills.append({"id": skill.id, "name": skill.name, "version": AUTHORING_SKILL_VERSIONS[skill.name],
                       "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    if total > MAX_SKILL_CONTEXT_BYTES:
        raise HTTPException(409, "已安装技能超过编码上下文预算，请减少技能数量")
    mcps = []
    for resource_id in requested.mcp_ids:
        cfg = db.scalar(select(MCPConfig).where(MCPConfig.id == resource_id,
            MCPConfig.tenant_id == principal.tenant_id, MCPConfig.enabled.is_(True),
            MCPConfig.transport.in_(mcp_resource_service.REMOTE_TRANSPORTS)))
        if cfg is None:
            raise HTTPException(409, RESOURCE_ERROR)
        mcps.append({"id": cfg.id, "name": cfg.name, "connector_revision": cfg.connector_revision})
    snapshot = _safe({"version": 1, "skills": skills, "mcps": mcps})
    return requested.model_dump(mode="json"), snapshot


def prepare_context(db, document: dict) -> dict:
    requested = selection(document)
    _, current = freeze_selection(db, requested)
    frozen = document.get("resource_snapshot")
    if frozen is None and not requested["skill_ids"] and not requested["mcp_ids"]:
        frozen = {"version": 1, "skills": [], "mcps": []}
    if frozen != current:
        raise ValueError(RESOURCE_ERROR)
    methods = []
    for item in current["skills"]:
        skill = db.scalar(select(Skill).where(Skill.id == item["id"], Skill.enabled.is_(True),
            tenant_service.visible_clause(Skill, db)))
        if skill is None:
            raise ValueError(RESOURCE_ERROR)
        text = _skill_text(skill)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != item["content_sha256"]:
            raise ValueError(RESOURCE_ERROR)
        methods.append({**item, "instructions": text})
    return {"authoring_skills": methods,
            "authoring_mcps": [{"id": item["id"], "name": item["name"], "mode": "read_only_resources"}
                               for item in current["mcps"]]}


def update_settings(db, workspace_id: str, request) -> dict:
    from .plugin_coding_workspace import append_event, owned_root, public_workspace

    row = owned_root(db, workspace_id, lock=True)
    release_service._scenario_for_manage(db, row.proposal["manifest"]["scenario"]["id"])
    document = deepcopy(row.proposal)
    fingerprint = canonical_hash(request.model_dump(mode="json"), domain="plugin-coding-settings-v1")
    applied = document.get("applied_settings", {})
    if request.request_id in applied:
        if applied[request.request_id] != fingerprint:
            raise HTTPException(409, "配置请求身份已用于不同编程设置")
        return public_workspace(db, row)
    if document["revision"] != request.expected_revision:
        raise HTTPException(409, "编程设置已变化，草稿已保留；请刷新后重试")
    active = db.get(AssistantRequestRun, document["active_run_id"]) if document.get("active_run_id") else None
    if active and active.status in {"queued", "waiting_upload", "running"}:
        raise HTTPException(409, "编码正在进行，请先停止本轮，再保存编程设置")
    value = {name: getattr(request, name) for name in ("llm_config_id", "skill_ids", "mcp_ids")}
    configured, frozen = freeze_selection(db, value)
    document.update(llm_config_id=configured["llm_config_id"], resource_selection=configured,
                    resource_snapshot=frozen, revision=document["revision"] + 1)
    document["applied_settings"] = dict(list({**applied, request.request_id: fingerprint}.items())[-50:])
    append_event(document, "settings", "编程模型与扩展已保存；下一轮使用新配置")
    row.proposal = document
    db.commit()
    return public_workspace(db, row)
