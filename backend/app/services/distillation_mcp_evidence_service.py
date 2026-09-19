"""Durable, authorized observations of MCP reads without remote locators."""
from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_conversation_schemas import MCPMaterialRead, MCPReadReceipt
from ..distillation_schemas import Evidence, MCPReadReference
from . import capability_contracts, permission_service


MAX_MCP_READS = 12


def identity_hash(observation: MCPMaterialRead) -> str:
    if hashlib.sha256(observation.text.encode()).hexdigest() != observation.content_sha256:
        raise HTTPException(422, "MCP 资料观察内容与回执不一致")
    return capability_contracts.canonical_hash(observation.model_dump(mode="json"), domain="distillation-mcp-observation-v1")


def record_read(turn_id: str, step_id: str, observation: MCPMaterialRead) -> tuple[dict, dict]:
    identity = identity_hash(observation)
    evidence = Evidence(key="mcp_" + step_id[:20], title=observation.title, kind="system_export", role="reference",
        summary="MCP 资料观察摘录：" + observation.text[:800],
        coverage="本次只读取得的文本快照。",
        limitations="仅证明当时读取到这些内容；来源内容可能不完整或已变化，不证明业务事实或副作用完成。",
        mcp_read=MCPReadReference(turn_id=turn_id, step_id=step_id, identity_sha256=identity))
    receipt = MCPReadReceipt(mcp_id=observation.mcp_id, evidence_key=evidence.key, title=evidence.title,
        summary=evidence.summary, content_sha256=observation.content_sha256, identity_sha256=identity,
        retrieved_at=observation.retrieved_at).model_dump(mode="json")
    record = {"step_id": step_id, "evidence": evidence.model_dump(),
        "observation": observation.model_dump(mode="json")}
    return record, receipt


def observations(context: dict, steps: list[dict]) -> list[Evidence]:
    completed = {step["id"] for step in steps if step["status"] == "succeeded" and step.get("mcp")}
    return [Evidence.model_validate(item["evidence"]) for item in context.get("mcp_reads", [])
        if item["step_id"] in completed]


def resolve_read(db: Session, evidence: Evidence, scenario_id: str | None) -> dict:
    from ..distillation_conversation_models import DistillationConversationTurn
    from ..distillation_models import DistillationProject
    from . import distillation_service

    reference = evidence.mcp_read
    if reference is None:
        raise HTTPException(422, "缺少 MCP 实际资料回执")
    principal = permission_service.require_principal(db)
    row = db.scalar(select(DistillationConversationTurn).where(
        DistillationConversationTurn.id == reference.turn_id,
        DistillationConversationTurn.tenant_id == principal.tenant_id))
    project = db.get(DistillationProject, row.project_id) if row else None
    if project is None or (project.scenario_id is not None and project.scenario_id != scenario_id):
        raise HTTPException(404, "MCP 资料回执不存在")
    distillation_service.authorize_scope(db, project.scenario_id)
    record = next((item for item in row.context.get("mcp_reads", []) if item["step_id"] == reference.step_id), None)
    step = next((item for item in row.steps if item["id"] == reference.step_id
        and item["status"] == "succeeded" and item.get("mcp")), None)
    if record is None or step is None:
        raise HTTPException(422, "MCP 资料缺少成功读取回执")
    observation = MCPMaterialRead.model_validate(record["observation"])
    original = Evidence.model_validate(record["evidence"])
    if (identity_hash(observation) != reference.identity_sha256 or original.key != evidence.key
            or original.mcp_read != reference or step["mcp"]["identity_sha256"] != reference.identity_sha256):
        raise HTTPException(422, "MCP 资料引用与实际读取回执不一致")
    # This is an immutable historical observation, not a claim that a live
    # connector still contains the same data. Project ACL guards its reuse.
    return {"receipt": step["mcp"], "observation": observation.model_dump(mode="json")}
