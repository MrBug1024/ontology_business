"""Thin Agent adapter over the protocol-neutral capability runtime.

The browser Agent is a reference client. It may summarize the resolved ontology
for presentation, but every model-visible tool invocation is delegated to the
same ``CapabilityAgentRuntime`` used to reach ``CapabilityInvoker``. No legacy
SQL, RAG, Function, Rule, Action, Workflow, or Provider-specific executor lives
in this module.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Iterator

from sqlalchemy.orm import Session

from ..models import Agent, LLMConfig
from . import ontology_service, permission_service


class AgentRuntimeContextError(RuntimeError):
    """The supported Agent entry point did not receive its capability runtime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ontology_summary_for(scenario: Any, *, db: Session | None = None) -> str:
    """Serialize the visible authoring ontology for conversation presentation."""

    if not scenario or not scenario.entities:
        return ""
    lines: list[str] = []
    for entity in scenario.entities:
        properties = ", ".join(
            f"{prop.name}:{prop.data_type}"
            + ("(主键)" if prop.is_key else "")
            + ("(标题)" if getattr(prop, "is_title", False) else "")
            for prop in entity.properties
            if db is None or permission_service.can_read_property(db, prop)
        )
        lines.append(f"- 实体「{entity.name}」: {properties or '无属性'}")
        if entity.description:
            lines.append(f"  说明: {entity.description}")

    for relation in scenario.relations:
        source_name = next(
            (
                entity.name
                for entity in scenario.entities
                if entity.id == relation.source_entity_id
            ),
            "?",
        )
        target_name = next(
            (
                entity.name
                for entity in scenario.entities
                if entity.id == relation.target_entity_id
            ),
            "?",
        )
        constraints = ontology_service.normalize_relation_constraints(
            getattr(relation, "constraints", {}) or {},
            relation_type=relation.relation_type,
        )
        constraint_names = [
            label
            for key, label in (
                ("symmetric", "对称"),
                ("transitive", "传递"),
                ("irreflexive", "反自反"),
                ("asymmetric", "非对称"),
                ("antisymmetric", "反对称"),
                ("acyclic", "无环"),
            )
            if constraints.get(key)
        ]
        suffix = f"；约束：{'、'.join(constraint_names)}" if constraint_names else ""
        if constraints.get("inverse_relation_id"):
            inverse = next(
                (
                    item
                    for item in scenario.relations
                    if item.id == constraints["inverse_relation_id"]
                ),
                None,
            )
            suffix += (
                "；逆关系："
                + str(
                    getattr(
                        inverse,
                        "name",
                        constraints["inverse_relation_id"],
                    )
                )
            )
        lines.append(
            f"- 关系: {source_name} --[{relation.name}]--"
            f"({relation.relation_type})--> {target_name}{suffix}"
        )
        if (
            constraints.get("symmetric")
            or constraints.get("transitive")
            or constraints.get("inverse_relation_id")
        ):
            lines.append(
                "  查询语义：推理边只在查询时解释，不会自动创建反向边、"
                "逆关系实例或传递闭包。"
            )

    executable_functions = [
        function
        for function in getattr(scenario, "function_definitions", ())
        if function.runtime_kind != "contract"
    ]
    if executable_functions:
        lines.append("\n【业务函数（Functions）】")
        for function in executable_functions:
            lines.append(
                f"- 函数「{function.name}」({function.runtime_kind}): "
                f"{function.description[:60]}"
            )

    if getattr(scenario, "actions", None):
        lines.append("\n【操作】")
        for action in scenario.actions:
            if db is not None and not permission_service.check_action(
                db, action, "read"
            ).allowed:
                continue
            entity_name = next(
                (
                    entity.name
                    for entity in scenario.entities
                    if entity.id == action.entity_id
                ),
                "?",
            )
            lines.append(
                f"- 操作「{action.name}」(实体:{entity_name}, 执行:{action.executor_type}, "
                f"{'已启用' if action.enabled else '已停用'}): {action.description[:60]}"
            )

    if getattr(scenario, "rules", None):
        lines.append("\n【规则（Rules）】")
        for rule in scenario.rules:
            lines.append(f"- 规则「{rule.name}」({rule.severity}): {rule.description[:60]}")

    if getattr(scenario, "events", None):
        lines.append("\n【事件（Events）】")
        for event in scenario.events:
            lines.append(
                f"- 事件「{event.name}」({'已启用' if event.enabled else '已停用'}): "
                f"{event.description[:60]}"
            )

    if getattr(scenario, "workflows", None):
        lines.append("\n【工作流（Workflows）】")
        for workflow in scenario.workflows:
            if db is not None and not permission_service.check_workflow(
                db, workflow, "read"
            ).allowed:
                continue
            lines.append(
                f"- 工作流「{workflow.name}」({workflow.trigger_type}, "
                f"{workflow.status}, "
                f"{len(workflow.nodes or []) or len(workflow.steps or [])}节点): "
                f"{workflow.description[:60]}"
            )
    return "\n".join(lines)


def run_agent(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    history: list[dict[str, Any]],
    user_message: str,
    scenario_name: str,
    ontology_summary: str,
    *,
    trace_context: dict[str, Any] | None = None,
    runtime_context: Any | None = None,
    before_llm_call: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Run one Agent turn exclusively through a resolved capability runtime."""

    capability_loop = getattr(runtime_context, "run_agent", None)
    if (
        getattr(runtime_context, "runtime_path", None) != "capability"
        or not callable(capability_loop)
    ):
        raise AgentRuntimeContextError(
            "capability_runtime_required",
            "Agent execution requires a resolved capability-only runtime context",
        )
    if (
        getattr(runtime_context, "db", None) is not db
        or getattr(getattr(runtime_context, "agent", None), "id", None) != agent.id
    ):
        raise AgentRuntimeContextError(
            "capability_runtime_mismatch",
            "Agent capability runtime context does not match the current request",
        )

    previous_trace = db.info.get("llm_trace_context")
    previous_action_audit = db.info.get("action_audit_context")
    context = dict(trace_context or {})
    context.setdefault("agent_id", agent.id)
    context.setdefault("scenario_id", agent.scenario_id or "")
    if context:
        db.info["llm_trace_context"] = context
    db.info["action_audit_context"] = {
        "agent_id": agent.id,
        "llm_config_id": llm.id,
        "model_name": llm.model or "",
    }
    try:
        if before_llm_call is None:
            yield from capability_loop(history, user_message)
        else:
            yield from capability_loop(
                history,
                user_message,
                before_llm_call=before_llm_call,
            )
    finally:
        if previous_trace is None:
            db.info.pop("llm_trace_context", None)
        else:
            db.info["llm_trace_context"] = previous_trace
        if previous_action_audit is None:
            db.info.pop("action_audit_context", None)
        else:
            db.info["action_audit_context"] = previous_action_audit
