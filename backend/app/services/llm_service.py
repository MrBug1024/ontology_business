"""LLM 服务：OpenAI 兼容协议（支持工具调用）。"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from openai import OpenAI

from ..models import LLMConfig


def _client(cfg: LLMConfig) -> OpenAI:
    return OpenAI(
        base_url=cfg.base_url or None,
        api_key=cfg.api_key or "sk-placeholder",
        timeout=120.0,
    )


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """非流式对话，返回 {"content": str, "tool_calls": [...], "raw": ...}。"""
    client = _client(cfg)
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    # 推理模型（如 MiniMax-M2.7）的思考过程会占用 max_tokens，
    # 若因长度截断且无正文/工具调用，自动放大 token 预算重试一次。
    for attempt in range(2):
        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        tool_calls = []
        if choice.tool_calls:
            for tc in choice.tool_calls:
                try:
                    args = _loads(tc.function.arguments)
                except Exception:  # noqa: BLE001
                    args = {}
                tool_calls.append(
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": args}}
                )
        truncated = getattr(choice, "finish_reason", None) == "length"
        if attempt == 0 and truncated and not (choice.content or "").strip() and not tool_calls:
            kwargs["max_tokens"] = (kwargs.get("max_tokens") or 4096) * 2
            continue
        return {"content": choice.content or "", "tool_calls": tool_calls, "raw": resp}
    return {"content": choice.content or "", "tool_calls": tool_calls, "raw": resp}


def chat_stream(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Iterator[dict[str, Any]]:
    """流式对话，逐块 yield {"type": "token", "content": str}，最后 yield {"type": "tool_calls", "tool_calls": [...]}。"""
    client = _client(cfg)
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    stream = client.chat.completions.create(**kwargs)
    content_parts: list[str] = []
    tc_acc: dict[int, dict[str, Any]] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content_parts.append(delta.content)
            yield {"type": "token", "content": delta.content}
        if delta.tool_calls:
            for tc in delta.tool_calls:
                slot = tc_acc.setdefault(
                    tc.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["function"]["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["function"]["arguments"] += tc.function.arguments
    if tc_acc:
        tool_calls = []
        for idx in sorted(tc_acc):
            slot = tc_acc[idx]
            try:
                args = _loads(slot["function"]["arguments"])
            except Exception:  # noqa: BLE001
                args = {}
            tool_calls.append(
                {"id": slot["id"], "type": "function", "function": {"name": slot["function"]["name"], "arguments": args}}
            )
        yield {"type": "tool_calls", "tool_calls": tool_calls}


def _loads(s: str) -> dict[str, Any]:
    import json

    s = (s or "").strip()
    if not s:
        return {}
    return json.loads(s)


def test_connection(cfg: LLMConfig) -> tuple[bool, str]:
    try:
        r = chat(cfg, [{"role": "user", "content": "你好，请回复“ok”"}], max_tokens=16)
        return True, f"连接成功，模型响应: {r['content'][:50]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
