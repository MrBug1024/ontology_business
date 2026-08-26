"""Model-planned assistant routing with deterministic governance boundaries.

The model understands the user's goal and returns a validated semantic plan.
LangGraph makes the allowed transitions explicit; resource writes still happen
only in the existing proposal/apply services after their normal checks.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..models import LLMConfig
from . import llm_service


AssistantGoal = Literal[
    "answer",
    "clarify",
    "create",
    "update",
    "delete",
    "continue_work",
    "preview_action",
    "apply_change",
]
AssistantScope = Literal[
    "general",
    "scenario",
    "ontology",
    "mapping",
    "capabilities",
    "workflow",
    "scenario_model",
]
AssistantConfidence = Literal["high", "medium", "low"]

# Routing models may spend part of the completion budget on hidden reasoning
# before emitting the small decision object. Keep this bounded by the request
# routing lease while leaving enough room for structured-output fallbacks.
ROUTE_REQUEST_TIMEOUT_SECONDS = 20.0
ROUTE_MAX_COMPLETION_TOKENS = 2048


class AssistantSemanticDecision(BaseModel):
    """Narrow, provider-validated description of what the user is asking for."""

    goal: AssistantGoal = Field(
        description="The user's requested outcome, based on the full meaning of the message."
    )
    scope: AssistantScope = Field(
        description="The platform resource involved, or general when no authoring resource is requested."
    )
    confidence: AssistantConfidence = Field(
        description="Confidence in the goal and scope together."
    )
    reason: str = Field(
        description="One short user-safe explanation of the interpreted goal."
    )

    model_config = ConfigDict(extra="forbid")


class AssistantRoutePlan(BaseModel):
    intent: Literal[
        "chat",
        "explain",
        "scenario",
        "ontology",
        "mapping",
        "workflow",
        "scenario_model",
        "apply_guidance",
        "execute_guidance",
        "change_guidance",
    ]
    decision: AssistantSemanticDecision
    source: Literal["model", "no_model", "model_fallback"]
    policy_note: str = ""

    def public_context(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "goal": self.decision.goal,
            "scope": self.decision.scope,
            "confidence": self.decision.confidence,
            "source": self.source,
            "policy_note": self.policy_note,
        }


class _RouteState(TypedDict, total=False):
    message: str
    mode: str
    preferred_scope: str
    has_scenario: bool
    has_active_model_drafts: bool
    active_draft_scopes: list[str]
    decision_provider: Callable[[], AssistantSemanticDecision]
    decision: AssistantSemanticDecision
    source: Literal["model", "no_model", "model_fallback"]
    intent: str
    policy_note: str
    branch: Literal["answer", "draft", "preview", "guidance"]


def _fallback_decision() -> AssistantSemanticDecision:
    return AssistantSemanticDecision(
        goal="answer",
        scope="general",
        confidence="low",
        reason="无法可靠完成语义规划，已保守回退为只读回答。",
    )


def _classify(state: _RouteState) -> dict[str, Any]:
    provider = state.get("decision_provider")
    if provider is None:
        return {"decision": _fallback_decision(), "source": "no_model"}
    try:
        return {"decision": provider(), "source": "model"}
    except Exception:  # noqa: BLE001
        # Routing failure must never turn into an inferred write request.
        return {"decision": _fallback_decision(), "source": "model_fallback"}


def _govern(state: _RouteState) -> dict[str, Any]:
    decision = state["decision"]
    mode = str(state.get("mode") or "ask")

    # These are product safety boundaries, not natural-language routing.
    if mode == "explain":
        return {
            "intent": "explain",
            "branch": "answer",
            "policy_note": "本条消息被限制为只读回答。",
        }
    if mode == "apply" and (
        state.get("source") != "model"
        or decision.goal not in {"answer", "clarify"}
    ):
        return {
            "intent": "apply_guidance",
            "branch": "guidance",
            "policy_note": "聊天不能代替提案卡片上的显式确认。",
        }
    if mode == "execute":
        if state.get("source") != "model" or decision.goal == "preview_action":
            return {
                "intent": "execute_guidance",
                "branch": "preview",
                "policy_note": "本条消息被限制为安全预演，不直接执行操作。",
            }
        if decision.goal not in {"answer", "clarify"}:
            return {
                "intent": "chat",
                "branch": "answer",
                "policy_note": "安全预演模式不会创建或应用资源，请求已保持只读。",
            }

    if state.get("source") != "model":
        return {
            "intent": "chat",
            "branch": "answer",
            "policy_note": "语义规划模型不可用，本条请求已保守保持只读。",
        }

    if decision.goal in {"answer", "clarify"}:
        return {"intent": "chat", "branch": "answer"}
    if decision.goal == "apply_change":
        return {"intent": "apply_guidance", "branch": "guidance"}
    if decision.goal == "preview_action":
        return {"intent": "execute_guidance", "branch": "preview"}
    if decision.confidence != "high":
        return {
            "intent": "chat",
            "branch": "answer",
            "policy_note": "语义规划置信度不足，未生成变更。",
        }
    if decision.goal in {"update", "delete"}:
        return {
            "intent": "change_guidance",
            "branch": "guidance",
            "policy_note": "现有正式定义的修改或删除需要进入专用编辑与确认流程。",
        }
    if decision.goal == "continue_work":
        active_scopes = set(state.get("active_draft_scopes") or [])
        if (
            state.get("has_active_model_drafts")
            and decision.scope in active_scopes
        ):
            return {"intent": "scenario_model", "branch": "draft"}
        return {
            "intent": "chat",
            "branch": "answer",
            "policy_note": "没有与本条目标匹配的活动草稿，未推断新的创建任务。",
        }

    preferred_scope = str(state.get("preferred_scope") or "auto")
    if decision.goal == "create" and mode == "draft" and preferred_scope != "auto":
        compatible_scopes = {
            "scenario": {"scenario"},
            "ontology": {"ontology"},
            "mapping": {"mapping"},
            "workflow": {"workflow"},
            "scenario_model": {"scenario_model", "capabilities"},
        }.get(preferred_scope, set())
        if decision.scope not in compatible_scopes:
            return {
                "intent": "chat",
                "branch": "answer",
                "policy_note": "模型理解的建设范围与本条显式范围不一致，已保持只读并等待澄清。",
            }

    intent_by_scope = {
        "scenario": "scenario",
        "ontology": "ontology",
        "mapping": "mapping",
        "workflow": "workflow",
        "capabilities": "scenario_model",
        "scenario_model": "scenario_model",
    }
    intent = intent_by_scope.get(decision.scope)
    if decision.goal == "create" and intent:
        return {"intent": intent, "branch": "draft"}
    return {
        "intent": "chat",
        "branch": "answer",
        "policy_note": "请求缺少足够明确的创建目标，未生成变更。",
    }


def _finish_branch(_state: _RouteState) -> dict[str, Any]:
    return {}


def _branch(state: _RouteState) -> str:
    return state["branch"]


def _build_graph():
    builder = StateGraph(_RouteState)
    builder.add_node("classify", _classify)
    builder.add_node("govern", _govern)
    builder.add_node("answer", _finish_branch)
    builder.add_node("draft", _finish_branch)
    builder.add_node("preview", _finish_branch)
    builder.add_node("guidance", _finish_branch)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "govern")
    builder.add_conditional_edges(
        "govern",
        _branch,
        {
            "answer": "answer",
            "draft": "draft",
            "preview": "preview",
            "guidance": "guidance",
        },
    )
    for node in ("answer", "draft", "preview", "guidance"):
        builder.add_edge(node, END)
    return builder.compile()


_ROUTE_GRAPH = _build_graph()


def route_assistant_decision(
    decision: AssistantSemanticDecision,
    *,
    mode: str = "ask",
    preferred_scope: str = "auto",
    has_scenario: bool = True,
    has_active_model_drafts: bool = False,
    active_draft_scopes: list[str] | None = None,
) -> AssistantRoutePlan:
    """Apply the same graph policy to an already validated model decision."""
    state = _ROUTE_GRAPH.invoke({
        "message": "",
        "mode": mode,
        "preferred_scope": preferred_scope,
        "has_scenario": has_scenario,
        "has_active_model_drafts": has_active_model_drafts,
        "active_draft_scopes": list(
            active_draft_scopes
            if active_draft_scopes is not None
            else (["scenario_model"] if has_active_model_drafts else [])
        ),
        "decision_provider": lambda: decision,
    })
    return AssistantRoutePlan(
        intent=state["intent"],
        decision=state["decision"],
        source=state["source"],
        policy_note=str(state.get("policy_note") or ""),
    )


_ROUTER_SYSTEM_PROMPT = """你是业务本体平台的请求语义规划器。你的唯一任务是理解用户此刻想得到的结果，
并返回给定结构，不能回答业务问题，也不能生成业务模型。

判断原则：
- 依据整句话、最近对话和当前上下文理解语义，不使用孤立名词决定动作。
- 询问事实、数量、状态、原因、定义、建议、可行性、故障原因或能力边界，goal 必须是 answer。
- 只有用户明确要求新建或产出平台资源时，goal 才是 create。
- 征询“是否应该创建”仍是 answer，不是 create。
- update/delete 仅表示用户明确要求改变已有正式定义；continue_work 仅表示明确续作已有活动草稿。
- preview_action 表示用户明确要求检查一个操作的参数、权限和影响；apply_change 表示要求确认或应用已有提案。
- UI 偏好范围只帮助判断 scope，绝不证明用户要求创建。
- 无法可靠判断时选择 clarify；不要为了完成任务而猜测创建意图。

scope 只描述主题：capabilities 包含函数、操作、规则和事件；scenario_model 表示跨多个资源域的完整建模。
reason 必须是一句简短、可向用户展示且不包含隐藏推理的说明。"""


def _decision_messages(
    *,
    message: str,
    history: list[dict[str, str]],
    page: str,
    path: str,
    preferred_scope: str,
    mode: str,
    has_scenario: bool,
    has_attachments: bool,
    has_active_model_drafts: bool,
    active_draft_scopes: list[str],
    context_summary: str,
) -> list[dict[str, str]]:
    recent_history = [
        {"role": item.get("role", ""), "content": str(item.get("content") or "")[:2000]}
        for item in history[-6:]
        if item.get("role") in {"user", "assistant"}
    ]
    runtime = {
        "page": page,
        "path": path,
        "ui_mode": mode,
        "preferred_scope": preferred_scope or "auto",
        "has_scenario": has_scenario,
        "has_attachments": has_attachments,
        "has_active_model_drafts": has_active_model_drafts,
        "active_draft_scopes": active_draft_scopes,
        "context_summary": context_summary[:2000],
        "recent_history": recent_history,
        "current_message": message,
    }
    return [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "请规划本条请求：\n" + json.dumps(runtime, ensure_ascii=False),
        },
    ]


def plan_assistant_request(
    *,
    llm: LLMConfig | None,
    db: Session,
    message: str,
    history: list[dict[str, str]],
    page: str,
    path: str,
    mode: str,
    preferred_scope: str,
    has_scenario: bool,
    has_attachments: bool,
    has_active_model_drafts: bool,
    active_draft_scopes: list[str],
    context_summary: str,
) -> AssistantRoutePlan:
    provider: Callable[[], AssistantSemanticDecision] | None = None
    if llm is not None and mode != "explain":
        messages = _decision_messages(
            message=message,
            history=history,
            page=page,
            path=path,
            preferred_scope=preferred_scope,
            mode=mode,
            has_scenario=has_scenario,
            has_attachments=has_attachments,
            has_active_model_drafts=has_active_model_drafts,
            active_draft_scopes=active_draft_scopes,
            context_summary=context_summary,
        )

        def invoke_model() -> AssistantSemanticDecision:
            return llm_service.structured_chat(
                llm,
                messages,
                AssistantSemanticDecision,
                db=db,
                operation="assistant_route",
                request_timeout=ROUTE_REQUEST_TIMEOUT_SECONDS,
                max_tokens=ROUTE_MAX_COMPLETION_TOKENS,
                max_retries=0,
            )

        provider = invoke_model

    state = _ROUTE_GRAPH.invoke({
        "message": message,
        "mode": mode,
        "preferred_scope": preferred_scope,
        "has_scenario": has_scenario,
        "has_active_model_drafts": has_active_model_drafts,
        "active_draft_scopes": active_draft_scopes,
        "decision_provider": provider,
    })
    return AssistantRoutePlan(
        intent=state["intent"],
        decision=state["decision"],
        source=state["source"],
        policy_note=str(state.get("policy_note") or ""),
    )
