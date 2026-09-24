"""Trusted capability adapters used by distillation and the modeling advisor."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_conversation_schemas import (
    JevDecisionArguments,
    JevDecisionReceipt,
    JevDecisionResult,
)
from ..models import MCPConfig, normalize_mcp_name_key
from . import capability_contracts, mcp_service, permission_service, release_service, tenant_service
from .mcp_resource_service import REMOTE_TRANSPORTS


JEV_MCP_NAME = "jev_decide"
JEV_TOOL_NAME = "jev_decide"
MAX_ADVISOR_SITUATION = 12_000
TOOL_KEYS = frozenset({JEV_TOOL_NAME})


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object, *, domain: str) -> str:
    return capability_contracts.canonical_hash(value, domain=domain)


def _safe_situation(value: str) -> str:
    situation = str(value or "").strip()
    if not situation or len(situation) > MAX_ADVISOR_SITUATION:
        raise ValueError("Jev 决策上下文为空或超过边界")
    if release_service.safe_snapshot_content({"situation": situation}) != {"situation": situation}:
        raise ValueError("Jev 决策上下文包含敏感连接信息")
    return situation


def _visible_jev_configs(db: Session) -> list[MCPConfig]:
    permission_service.require_principal(db)
    rows = list(db.scalars(select(MCPConfig).where(
        MCPConfig.name_key == normalize_mcp_name_key(JEV_MCP_NAME),
        MCPConfig.enabled.is_(True),
        tenant_service.visible_clause(MCPConfig, db),
    )))
    tenant_id = str(db.info.get("tenant_id") or "")
    rows.sort(key=lambda row: (str(row.tenant_id or "") != tenant_id, str(row.id)))
    return [row for row in rows if row.transport in REMOTE_TRANSPORTS]


def resolve_jev_config(db: Session) -> MCPConfig | None:
    """Resolve the tenant-owned provider identity without exposing credentials."""
    return next(iter(_visible_jev_configs(db)), None)


def _snapshot_mcps(snapshot: dict) -> list[dict]:
    if not isinstance(snapshot, dict) or int(snapshot.get("version", 0) or 0) < 4:
        return []
    values = snapshot.get("capability_mcps")
    return values if isinstance(values, list) else []


def is_jev_snapshot(snapshot: dict) -> bool:
    return any(
        isinstance(item, dict) and item.get("name_key") == normalize_mcp_name_key(JEV_MCP_NAME)
        for item in _snapshot_mcps(snapshot)
    )


def effective_keys(snapshot: dict) -> tuple[str, ...]:
    return (JEV_TOOL_NAME,) if is_jev_snapshot(snapshot) else ()


def tool_title(name: str) -> str:
    if name != JEV_TOOL_NAME:
        raise ValueError("不支持的能力工具")
    return "Jev 决策判断"


def definitions(snapshot: dict) -> list[dict]:
    if not is_jev_snapshot(snapshot):
        return []
    from .distillation_tool_schema import portable_schema

    return [{
        "type": "function",
        "function": {
            "name": JEV_TOOL_NAME,
            "description": "调用已治理的 Jev 决策模型做结构化判断。它只提供快速判断信号，不读取业务资料，不证明业务事实，也不执行副作用。",
            "parameters": portable_schema(JevDecisionArguments.model_json_schema()),
        },
    }]


def require_allowed(name: str, snapshot: dict) -> None:
    if name not in TOOL_KEYS or name not in effective_keys(snapshot):
        raise ValueError("本轮未启用该决策能力")


def _selected_config(db: Session, turn) -> MCPConfig:
    snapshot = turn.context.get("resource_selection", {}) if turn is not None else {}
    selected = next((item for item in _snapshot_mcps(snapshot)
                     if item.get("name_key") == normalize_mcp_name_key(JEV_MCP_NAME)), None)
    if not selected or not isinstance(selected.get("id"), str):
        raise ValueError("本轮没有冻结 Jev 决策能力")
    row = db.scalar(select(MCPConfig).where(
        MCPConfig.id == selected["id"],
        MCPConfig.enabled.is_(True),
        tenant_service.visible_clause(MCPConfig, db),
    ))
    if row is None or row.transport not in REMOTE_TRANSPORTS or row.connector_revision != selected.get("connector_revision"):
        raise ValueError("Jev 决策连接已变化，请刷新后重新发送")
    if row.name_key != normalize_mcp_name_key(JEV_MCP_NAME):
        raise ValueError("冻结的 MCP 不是受信 Jev 决策能力")
    return row


def _finite_probability(value: object) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise ValueError("Jev 返回了无效概率")
    return result


def _parse_response(payload: JevDecisionArguments, response: dict[str, Any]) -> tuple[str, list[JevDecisionResult]]:
    if response.get("status") != "success":
        raise ValueError("Jev 决策服务返回失败")
    try:
        raw = json.loads(str(response.get("text") or ""), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Jev 决策响应不是合法 JSON") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("model"), str) or not isinstance(raw.get("results"), list):
        raise ValueError("Jev 决策响应缺少模型或结果")
    if len(raw["results"]) != len(payload.questions):
        raise ValueError("Jev 决策结果数量与问题数量不一致")
    normalized: list[JevDecisionResult] = []
    for question, item in zip(payload.questions, raw["results"], strict=True):
        if not isinstance(item, dict):
            raise ValueError("Jev 决策结果格式无效")
        confidence = _finite_probability(item.get("confidence"))
        if question.type in {"choice", "score"}:
            probabilities = item.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != set(question.options or ()):
                raise ValueError("Jev 概率结果与输入选项不一致")
            probabilities = {str(key): _finite_probability(value) for key, value in probabilities.items()}
        else:
            probabilities = {}
        if question.type == "choice":
            choice = item.get("choice")
            if not isinstance(choice, str) or choice not in (question.options or ()):
                raise ValueError("Jev 选择结果不在输入选项中")
            normalized.append(JevDecisionResult(type="choice", choice=choice,
                choice_index=(question.options or []).index(choice), probabilities=probabilities,
                confidence=confidence))
        elif question.type == "score":
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or abs(float(score)) > 1_000_000:
                raise ValueError("Jev 评分结果无效")
            normalized.append(JevDecisionResult(type="score", score=float(score), probabilities=probabilities,
                confidence=confidence))
        else:
            if "noul" not in item:
                raise ValueError("Jev 判断结果缺少 noul 概率")
            normalized.append(JevDecisionResult(type="yes_no", true_probability=_finite_probability(item["noul"]),
                confidence=confidence))
    return raw["model"][:200], normalized


def _receipt(cfg: MCPConfig, *, payload: JevDecisionArguments, model: str, results: list[JevDecisionResult], status: str, output: object) -> JevDecisionReceipt:
    return JevDecisionReceipt(
        mcp_id=str(cfg.id),
        connector_revision=int(cfg.connector_revision),
        model=model[:200] or "unknown",
        input_sha256=_hash(payload.model_dump(mode="json"), domain="jev-decision-input-v1"),
        output_sha256=_hash(output, domain="jev-decision-output-v1"),
        result_count=len(results),
        results=results,
        executed_at=datetime.now(timezone.utc),
        status=status,
    )


def execute(db: Session, arguments: dict, turn):
    from .distillation_conversation_tools import ToolResult

    payload = JevDecisionArguments.model_validate(arguments)
    cfg = _selected_config(db, turn)
    db.expunge(cfg)
    response = mcp_service.call_tool(cfg, JEV_TOOL_NAME, payload.model_dump(mode="json"),
        execution_key=f"distillation:{turn.id}:jev_decide")
    try:
        model, results = _parse_response(payload, response)
    except ValueError as exc:
        receipt = _receipt(cfg, payload=payload, model="unknown", results=[], status="failed", output={"error": str(exc)})
        return ToolResult({"status": "blocked", "capability": JEV_TOOL_NAME,
            "reason": "Jev 决策没有返回可验证的结构化结果，请人工复核或稍后重试。"},
            "Jev 决策未形成可验证回执，未作为业务事实使用。", capability_receipt=receipt)
    receipt = _receipt(cfg, payload=payload, model=model, results=results, status="succeeded",
        output=[item.model_dump(mode="json") for item in results])
    return ToolResult({"status": "success", "capability": JEV_TOOL_NAME, "model": model,
        "results": [item.model_dump(mode="json") for item in results],
        "score_semantics": "score 是有序选项上的连续模型分数，不是业务状态或持久化枚举。"},
        "已取得 Jev 结构化决策信号；该信号仅供 AI 分析，未写入业务证据。", capability_receipt=receipt)


def advisor_signal(db: Session, message: str, scenario_summary: str = "") -> JevDecisionReceipt | None:
    """Use Jev as a bounded signal for the advisor, never as a source citation."""
    try:
        situation = _safe_situation((scenario_summary + "\n" if scenario_summary else "") + message[:MAX_ADVISOR_SITUATION])
        cfg = resolve_jev_config(db)
        if cfg is None:
            return None
        payload = JevDecisionArguments(
            situation=situation,
            questions=[
                {"type": "choice", "question": "当前请求最需要哪一种下一步？",
                 "options": ["澄清业务价值与边界", "核对事实和业务资料", "生成业务模型草稿", "先人工确认取舍"]},
                {"type": "yes_no", "question": "当前请求是否涉及需要人工确认的业务取舍或潜在副作用？"},
            ],
        )
        db.expunge(cfg)
        response = mcp_service.call_tool(cfg, JEV_TOOL_NAME, payload.model_dump(mode="json"),
            execution_key=f"assistant:{_hash(payload.model_dump(mode='json'), domain='jev-advisor-execution-v1')}")
        model, results = _parse_response(payload, response)
        return _receipt(cfg, payload=payload, model=model, results=results, status="succeeded",
            output=[item.model_dump(mode="json") for item in results])
    except Exception:  # noqa: BLE001
        return None


def advisor_prompt(receipt: JevDecisionReceipt | None) -> str:
    if receipt is None:
        return "\n\nJev 决策能力本轮没有可用回执。请继续使用现有受管业务资料和人工澄清，不要猜测 Jev 结果。"
    return (
        "\n\n【Jev 决策能力回执】\n"
        "以下是受信 Jev 模型对当前请求给出的结构化判断信号，不是业务资料、证据或事实。"
        "请结合真实业务资料重新解释；低置信度、冲突或涉及取舍时向人工澄清，不能据此自动写入或执行。\n"
        + _canonical(receipt.model_dump(mode="json"))
    )
