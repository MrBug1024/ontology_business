"""Explicit model/tool loop for business investigation turns.

The graph is deliberately in-memory. Durable checkpoints and lease fencing
remain in PostgreSQL so a worker restart resumes from a committed boundary.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class InvestigationGraphState(TypedDict, total=False):
    messages: list[dict]
    visible_prefix: str
    response: dict[str, Any]
    terminal: bool
    model_call: Callable[[list[dict], str], dict[str, Any]]
    execute_round: Callable[[dict[str, Any]], dict[str, Any]]


def _model_node(state: InvestigationGraphState) -> dict[str, Any]:
    response = state["model_call"](state["messages"], state.get("visible_prefix", ""))
    return {"response": response}


def _model_route(state: InvestigationGraphState) -> str:
    return "tools" if state["response"].get("tool_calls") else "finish"


def _tools_node(state: InvestigationGraphState) -> dict[str, Any]:
    return state["execute_round"](state)


def _tools_route(state: InvestigationGraphState) -> str:
    return "finish" if state.get("terminal") else "model"


def build_investigation_graph():
    builder = StateGraph(InvestigationGraphState)
    builder.add_node("model", _model_node)
    builder.add_node("tools", _tools_node)
    builder.add_node("finish", lambda _state: {})
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", _model_route, {"tools": "tools", "finish": "finish"})
    builder.add_conditional_edges("tools", _tools_route, {"model": "model", "finish": "finish"})
    builder.add_edge("finish", END)
    return builder.compile()


INVESTIGATION_GRAPH = build_investigation_graph()
