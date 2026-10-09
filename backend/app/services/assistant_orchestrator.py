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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from ..models import LLMConfig
from . import llm_service


AssistantGoal = Literal[
    "answer",
    "clarify",
    "research",
    "create",
    "update",
    "delete",
    "continue_work",
    "preview_action",
    "apply_change",
]
AssistantScope = Literal[
    "general",
    "research",
    "scenario",
    "ontology",
    "mapping",
    "capabilities",
    "rules",
    "workflow",
    "scenario_model",
]
AssistantConfidence = Literal["high", "medium", "low"]
AssistantCapability = Literal[
    "answer_question",
    "research_domain",
    "draft_scenario",
    "draft_ontology",
    "draft_mapping",
    "draft_workflow",
    "compile_scenario_model",
    "preview_governed_action",
    "explain_change_boundary",
]


_CAPABILITY_BY_INTENT: dict[str, AssistantCapability] = {
    "chat": "answer_question",
    "research": "research_domain",
    "explain": "answer_question",
    "scenario": "draft_scenario",
    "ontology": "draft_ontology",
    "mapping": "draft_mapping",
    "workflow": "draft_workflow",
    "scenario_model": "compile_scenario_model",
    "execute_guidance": "preview_governed_action",
    "apply_guidance": "explain_change_boundary",
    "change_guidance": "explain_change_boundary",
}

_CAPABILITY_LABELS: dict[AssistantCapability, str] = {
    "answer_question": "上下文问答",
    "research_domain": "行业知识检索",
    "draft_scenario": "业务场景草拟",
    "draft_ontology": "本体模型草拟",
    "draft_mapping": "数据映射草拟",
    "draft_workflow": "工作流草拟",
    "compile_scenario_model": "附件理解与场景建模",
    "preview_governed_action": "受控操作预演",
    "explain_change_boundary": "变更边界说明",
}

_CAPABILITY_TOOL_CONFIG: dict[AssistantCapability, dict[str, Any]] = {
    "answer_question": {
        "scope": "general",
        "goals": ["answer", "clarify"],
        "description": "回答、解释或澄清问题；不会创建、修改、应用或执行平台资源。",
    },
    "research_domain": {
        "scope": "research",
        "goals": ["research", "clarify"],
        "description": "在受治理的公开来源范围内检索行业知识，返回带时间和来源的只读证据；不会自动写入模型。",
    },
    "draft_scenario": {
        "scope": "scenario",
        "goals": ["create"],
        "description": "根据明确要求草拟一个新的业务场景。",
    },
    "draft_ontology": {
        "scope": "ontology",
        "goals": ["create"],
        "description": "根据明确要求生成对象、属性、关系或约束草稿。",
    },
    "draft_mapping": {
        "scope": "mapping",
        "goals": ["create"],
        "description": "根据明确要求生成数据映射草稿。",
    },
    "draft_workflow": {
        "scope": "workflow",
        "goals": ["create"],
        "description": "根据明确要求生成工作流草稿。",
    },
    "compile_scenario_model": {
        "scope": "scenario_model",
        "scopes": ["ontology", "mapping", "capabilities", "rules", "workflow", "scenario_model"],
        "goals": ["create", "continue_work"],
        "description": "理解附件和上下文，按用户本轮明确的主题生成或继续场景模型草稿；函数/操作选 capabilities，规则/事件选 rules；明确多个主题时选 scenario_model 并用 task_ids 列出需要的任务。背景资料不扩大范围。",
    },
    "preview_governed_action": {
        "scope": "capabilities",
        "goals": ["preview_action"],
        "description": "检查一个已配置操作的参数、权限和影响，只做安全预演。",
    },
    "explain_change_boundary": {
        "scope": "general",
        "goals": ["apply_change", "update", "delete"],
        "description": "说明现有资源的应用、修改或删除边界，并引导到受控流程。",
    },
}


def _capability_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": config["description"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        **({"scope": {"type": "string", "enum": config["scopes"],
                            "description": "本轮明确要求产出的主题。只建函数选 capabilities，规则选 rules；多主题用 scenario_model 加 task_ids。"},
                            "task_ids": {"type": "array", "maxItems": 6, "uniqueItems": True,
                                "items": {"type": "string", "enum": ["ontology", "instances", "mapping", "capabilities", "rules", "workflows"]},
                                "description": "只列本轮明确需要建设的任务；例如规则与工作流为 rules、workflows；完整全阶段可省略。"}}
                           if "scopes" in config else {}),
                        "goal": {
                            "type": "string",
                            "enum": config["goals"],
                            "description": "本轮用户明确要求的结果。",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "一句可向用户展示的选择原因，不包含隐藏推理。",
                        },
                    },
                    "required": ["goal", "confidence", "reason"] + (["scope"] if "scopes" in config else []),
                    "additionalProperties": False,
                },
            },
        }
        for name, config in _CAPABILITY_TOOL_CONFIG.items()
        # Keep historical calls parseable; new authoring uses one scoped tool.
        if name not in {"draft_ontology", "draft_mapping", "draft_workflow"}
    ]


_ASSISTANT_CAPABILITY_TOOLS = _capability_tools()


def _decision_from_capability_call(response: dict[str, Any]) -> AssistantSemanticDecision:
    calls = response.get("tool_calls") if isinstance(response, dict) else None
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("语义规划模型必须且只能选择一个内部能力")
    function = calls[0].get("function") if isinstance(calls[0], dict) else None
    name = str((function or {}).get("name") or "")
    if name not in _CAPABILITY_TOOL_CONFIG:
        raise ValueError("语义规划模型选择了未注册的内部能力")
    config = _CAPABILITY_TOOL_CONFIG[name]
    arguments = (function or {}).get("arguments")
    # A few OpenAI-compatible providers reliably select the registered tool
    # but omit its optional explanatory arguments.  The tool name is the
    # actual routing contract; scope and allowed goals are server-owned.  Keep
    # that valid semantic selection instead of discarding it and accidentally
    # turning an explicit modelling request into ordinary chat.
    arguments = arguments if isinstance(arguments, dict) else {}
    scope = config["scope"]
    if "scopes" in config:
        scope = arguments.get("scope")
        if scope not in config["scopes"]:
            raise ValueError("语义规划模型必须选择有效的建模主题")
    goal = str(arguments.get("goal") or config["goals"][0])
    if goal not in config["goals"]:
        goal = config["goals"][0]
    confidence = str(arguments.get("confidence") or "high")
    if confidence not in {"high", "medium", "low"}:
        confidence = "high"
    return AssistantSemanticDecision(
        goal=goal,
        scope=scope,
        task_ids=arguments.get('task_ids', []),
        confidence=confidence,
        reason=str(arguments.get("reason") or f"已选择{_CAPABILITY_LABELS[name]}能力。")[:500],
    )

# Routing models may spend part of the completion budget on hidden reasoning
# before emitting the small decision object. Keep this bounded by the request
# routing lease while leaving enough room for structured-output fallbacks.
ROUTE_REQUEST_TIMEOUT_SECONDS = 30.0
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
    task_ids: list[Literal['ontology', 'instances', 'mapping', 'capabilities', 'rules', 'workflows']] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def validate_tasks(self):
        from .model_task_scope import requested_tasks
        self.task_ids = requested_tasks({'scope': self.scope, 'task_ids': self.task_ids})
        return self

    model_config = ConfigDict(extra="forbid")


class AssistantRoutePlan(BaseModel):
    intent: Literal[
        "chat",
        "research",
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
    capability: AssistantCapability | None = None
    policy_note: str = ""

    @model_validator(mode="after")
    def resolve_capability(self) -> "AssistantRoutePlan":
        self.capability = self.capability or _CAPABILITY_BY_INTENT.get(
            self.intent, "answer_question"
        )
        return self

    @property
    def capability_label(self) -> str:
        return _CAPABILITY_LABELS[self.capability or "answer_question"]

    def public_context(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "goal": self.decision.goal,
            "scope": self.decision.scope,
            "task_ids": self.decision.task_ids,
            "confidence": self.decision.confidence,
            "source": self.source,
            "recovered": self.source == "model_fallback",
            "capability": self.capability,
            "capability_label": self.capability_label,
            "policy_note": self.policy_note,
        }


class _RouteState(TypedDict, total=False):
    message: str
    mode: str
    preferred_scope: str
    has_scenario: bool
    has_attachments: bool
    has_active_model_drafts: bool
    active_draft_scopes: list[str]
    decision_provider: Callable[[], AssistantSemanticDecision]
    decision: AssistantSemanticDecision
    source: Literal["model", "no_model", "model_fallback"]
    intent: str
    policy_note: str
    branch: Literal["answer", "draft", "preview", "guidance"]


def unexecuted_authoring_notice(plan: AssistantRoutePlan) -> str:
    """A blocked authoring route must not narrate an imaginary construction."""
    if plan.intent == "chat" and plan.decision.goal in {"create", "continue_work", "update", "delete"}:
        return (
            "本轮尚未启动建模，也没有生成或保存新的候选。"
            f"{plan.policy_note}"
            "请明确本轮要生成或修正的场景草稿范围，再继续建设。"
        )
    return ""


def initial_model_task_scope(plan: AssistantRoutePlan) -> str:
    """Route each authoring scope through the same durable compiler."""
    if plan.decision.task_ids:
        return plan.decision.task_ids[0]
    return {"ontology": "ontology", "mapping": "mapping", "rules": "rules", "workflow": "workflows",
            "capabilities": "capabilities"}.get(plan.decision.scope, "ontology")


def _fallback_decision(state: _RouteState | None = None) -> AssistantSemanticDecision:
    del state
    return AssistantSemanticDecision(
        goal="answer",
        scope="general",
        confidence="low",
        reason="语义规划服务暂时不可用；为避免误调用建模或写入能力，已恢复普通回答流程。",
    )


def _classify(state: _RouteState) -> dict[str, Any]:
    provider = state.get("decision_provider")
    if provider is None:
        return {"decision": _fallback_decision(state), "source": "no_model"}
    try:
        return {"decision": provider(), "source": "model"}
    except Exception:  # noqa: BLE001
        # The recovery decision only uses request context already supplied to
        # the graph. It never inspects message keywords or invents a resource
        # scope from prose after the semantic provider has failed.
        return {"decision": _fallback_decision(state), "source": "model_fallback"}


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

    if state.get("source") == "model_fallback":
        return {
            "intent": "chat",
            "branch": "answer",
            "policy_note": "语义规划服务暂时不可用，已保持只读并恢复普通回答流程；附件不会自动触发建模。",
        }

    if state.get("source") != "model":
        return {
            "intent": "chat",
            "branch": "answer",
            "policy_note": "语义规划模型不可用，本条请求已保守保持只读。",
        }

    if decision.goal == "research":
        return {"intent": "research", "branch": "answer"}
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
            "scenario_model": set(_CAPABILITY_TOOL_CONFIG["compile_scenario_model"]["scopes"]),
        }.get(preferred_scope, set())
        if decision.scope not in compatible_scopes:
            return {
                "intent": "chat",
                "branch": "answer",
                "policy_note": "模型理解的建设范围与本条显式范围不一致，已保持只读并等待澄清。",
            }

    intent_by_scope = {
        "scenario": "scenario",
        "ontology": "scenario_model",
        "mapping": "scenario_model",
        "workflow": "scenario_model",
        "capabilities": "scenario_model",
        "rules": "scenario_model",
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
        "has_attachments": False,
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
并且必须调用且只能调用一个已注册的内部能力；不能直接回答业务问题，也不能生成业务模型。

平台会把你的结构化决策映射为内部能力：上下文问答、业务场景草拟、本体模型草拟、数据映射草拟、
工作流草拟、附件理解与场景建模、受控操作预演或变更边界说明。你只负责依据完整语义选择目标和范围，
不能因为当前页面、存在附件或历史草稿就擅自调用建设能力。

判断原则：
- 依据整句话、最近对话和当前上下文理解语义，不使用孤立名词决定动作。
- 询问事实、数量、状态、原因、定义、建议、可行性、故障原因或能力边界，goal 必须是 answer。
- 询问发布前需要什么、如何发布、应遵循什么流程或应做哪些准备时，若没有明确要求立即创建、修改或发布资源，goal 必须是 answer；已有草稿、当前场景或历史建模任务都不能把这类咨询改为 continue_work。
- 用户明确要求“根据文档完成建模”“建设完整业务模型”“生成对象、关系、映射、规则或工作流”等产出平台资源时，goal 是 create；即使同时要求先列可见计划、逐项确认、遇到歧义先询问或暂不直接写入正式场景，这些是执行策略，不改变建设意图。
- 征询“是否应该创建”仍是 answer，不是 create。
- update/delete 仅表示用户明确要求改变已有正式定义；continue_work 仅表示明确续作已有活动草稿。
- preview_action 表示用户明确要求检查一个操作的参数、权限和影响；apply_change 表示要求确认或应用已有提案。
- UI 偏好范围只帮助判断 scope，绝不证明用户要求创建。
- 无法可靠判断时选择 clarify；不要为了完成任务而猜测创建意图。
- confidence 只衡量是否理解用户的目标和范围，不衡量资料完备程度、实现难度或上轮模型是否失败。明确要求生成或修正活动候选并指明范围时应选 high；具体业务缺口交给建模校验处理。

scope 只描述主题：capabilities 包含函数和操作；rules 包含规则和事件；多个主题选择 scenario_model，并用 task_ids 列出明确要求的任务。
规则加工作流必须保留 rules 与 workflows 两项，不能仅选择 workflow 丢失规则；不要把背景中出现的其他主题加入任务。
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
            response = llm_service.chat(
                llm,
                messages,
                tools=_ASSISTANT_CAPABILITY_TOOLS,
                db=db,
                operation="assistant_route",
                request_timeout=ROUTE_REQUEST_TIMEOUT_SECONDS,
                max_tokens=ROUTE_MAX_COMPLETION_TOKENS,
                max_retries=0,
                retry_on_length=False,
                tool_choice="required",
            )
            return _decision_from_capability_call(response)

        provider = invoke_model

    state = _ROUTE_GRAPH.invoke({
        "message": message,
        "mode": mode,
        "preferred_scope": preferred_scope,
        "has_scenario": has_scenario,
        "has_attachments": has_attachments,
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
