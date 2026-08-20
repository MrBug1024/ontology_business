"""LLM 运行时：OpenAI 兼容调用、能力路由与脱敏调用追踪。"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LLMConfig, LLMInvocationTrace
from . import tenant_service


SUPPORTED_CAPABILITIES = frozenset({"chat", "embedding", "vision", "tool"})
_DEFAULT_CAPABILITIES = ("chat", "tool")
_SENSITIVE_KEY_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "token",
    "secret",
    "password",
    "credential",
    "bearer",
}
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(authorization\s*[=:]\s*(?:bearer\s+)?)([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
)


class LLMRuntimeError(RuntimeError):
    """模型能力或启用状态不满足真实调用要求。"""


@dataclass(frozen=True)
class TracePayload:
    tenant_id: str | None
    llm_config_id: str | None
    provider: str
    model: str
    capability: str
    operation: str
    status: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    tool_count: int
    error: str = ""


def _client(cfg: LLMConfig) -> OpenAI:
    return OpenAI(
        base_url=cfg.base_url or None,
        api_key=cfg.api_key or "sk-placeholder",
        timeout=120.0,
    )


def capabilities_of(cfg: LLMConfig) -> set[str]:
    """兼容旧数据的能力归一化；无效值不会变成可路由能力。"""
    raw = cfg.capabilities if isinstance(cfg.capabilities, list) else _DEFAULT_CAPABILITIES
    return {str(value).strip().lower() for value in raw if str(value).strip().lower() in SUPPORTED_CAPABILITIES}


def routable_configs(db: Session, capability: str) -> list[LLMConfig]:
    """返回当前租户可见、已启用且满足能力的模型，优先级越小越靠前。"""
    normalized = str(capability or "").strip().lower()
    if normalized not in SUPPORTED_CAPABILITIES:
        raise ValueError(f"不支持的模型能力: {capability}")
    # 路由 API 永远处于租户请求内；缺少上下文时拒绝而不是意外枚举全局模型。
    tenant_service.current_tenant_id(db)
    candidates = db.execute(
        select(LLMConfig)
        .where(
            LLMConfig.enabled.is_(True),
            tenant_service.visible_clause(LLMConfig, db),
        )
        .order_by(LLMConfig.routing_priority.asc(), LLMConfig.is_default.desc(), LLMConfig.name.asc())
    ).scalars().all()
    required = {"chat", "tool"} if normalized == "tool" else {normalized}
    return [cfg for cfg in candidates if required.issubset(capabilities_of(cfg))]


def sanitize_trace_text(value: Any, *, max_length: int = 600) -> str:
    """去除常见凭据并截断诊断文本，供 trace/error/评测摘要安全复用。"""
    text = " ".join(str(value or "").split())
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[:max_length]


def sanitize_trace_error(error: Any) -> str:
    """trace 只保留错误类别，绝不把 provider 可能回显的提示词写入数据库。"""
    if not error:
        return ""
    if isinstance(error, LLMRuntimeError):
        # 本地能力校验消息不包含用户内容，保留它有助于运营排障。
        return sanitize_trace_text(error, max_length=160)
    return f"{type(error).__name__}: provider 调用失败"


def sanitize_metrics(value: Any, *, depth: int = 0) -> Any:
    """评测元数据仅保留有限的非凭据结构，避免借 metrics 回显密钥。"""
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            normalized_key = key_text.strip().lower().replace("-", "_")
            if any(secret_name in normalized_key for secret_name in _SENSITIVE_KEY_NAMES):
                safe[key_text] = "[REDACTED]"
            else:
                safe[key_text] = sanitize_metrics(item, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [sanitize_metrics(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return sanitize_trace_text(value, max_length=500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_trace_text(value, max_length=500)


def _ensure_callable(cfg: LLMConfig, *, tools: bool = False) -> None:
    if not bool(cfg.enabled):
        raise LLMRuntimeError("该模型已停用")
    capabilities = capabilities_of(cfg)
    if "chat" not in capabilities:
        raise LLMRuntimeError("该模型未启用 chat 能力")
    if tools and "tool" not in capabilities:
        raise LLMRuntimeError("该模型未启用 tool 能力，不能执行工具调用")


def _message_text(messages: list[dict[str, Any]]) -> str:
    """仅在内存中计算 token 估算，绝不落库。"""
    try:
        return json.dumps(messages, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return " ".join(str(message.get("content", "")) for message in messages)


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = str(text or "")
    if not text:
        return 0
    # 中文字符平均更接近 1~2 token，英文/JSON 平均约 4 字符；取保守折中。
    han_count = sum("\u4e00" <= char <= "\u9fff" for char in text)
    other_count = len(text) - han_count
    return max(1, int(math.ceil(han_count / 1.5 + other_count / 4)))


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usage_counts(
    usage: Any,
    *,
    fallback_input: int,
    fallback_output: int,
) -> tuple[int, int, int]:
    input_tokens = _usage_value(usage, "prompt_tokens")
    output_tokens = _usage_value(usage, "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    input_tokens = fallback_input if input_tokens is None else input_tokens
    output_tokens = fallback_output if output_tokens is None else output_tokens
    total_tokens = input_tokens + output_tokens if total_tokens is None else total_tokens
    return input_tokens, output_tokens, total_tokens


def _estimated_cost(cfg: LLMConfig, input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * max(0.0, float(cfg.input_cost_per_million or 0))
        + (output_tokens / 1_000_000) * max(0.0, float(cfg.output_cost_per_million or 0)),
        8,
    )


def _tenant_for_trace(cfg: LLMConfig, db: Session | None) -> str | None:
    if db is not None and db.info.get("tenant_id"):
        return str(db.info["tenant_id"])
    return cfg.tenant_id


def _trace_model(payload: TracePayload) -> LLMInvocationTrace:
    return LLMInvocationTrace(
        tenant_id=payload.tenant_id,
        llm_config_id=payload.llm_config_id,
        provider=payload.provider,
        model=payload.model,
        capability=payload.capability,
        operation=payload.operation,
        status=payload.status,
        latency_ms=max(0, payload.latency_ms),
        input_tokens=max(0, payload.input_tokens),
        output_tokens=max(0, payload.output_tokens),
        total_tokens=max(0, payload.total_tokens),
        estimated_cost=max(0.0, payload.estimated_cost),
        currency=payload.currency or "USD",
        tool_count=max(0, payload.tool_count),
        error=sanitize_trace_error(payload.error),
    )


def _persist_trace(payload: TracePayload, *, db: Session | None = None) -> None:
    """优先独立提交 trace；SQLite 写锁冲突时退回当前业务事务。"""
    try:
        from ..database import SessionLocal

        trace_db = SessionLocal()
        try:
            trace_db.add(_trace_model(payload))
            trace_db.commit()
        finally:
            trace_db.close()
    except Exception:  # noqa: BLE001
        # worker 内部可能正持有 SQLite 写锁；同一事务的最终 commit 仍可保存 trace。
        if db is not None:
            try:
                db.add(_trace_model(payload))
                db.flush()
            except Exception:  # noqa: BLE001
                # 可观测性故障不能让实际业务调用失败。
                return


def _record_trace(
    cfg: LLMConfig,
    *,
    db: Session | None,
    capability: str,
    operation: str,
    status: str,
    started_at: float,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    tool_count: int = 0,
    error: Any = "",
) -> None:
    _persist_trace(
        TracePayload(
            tenant_id=_tenant_for_trace(cfg, db),
            llm_config_id=cfg.id,
            provider=cfg.provider or "",
            model=cfg.model or "",
            capability=capability,
            operation=operation,
            status=status,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost=_estimated_cost(cfg, input_tokens, output_tokens),
            currency=cfg.cost_currency or "USD",
            tool_count=tool_count,
            error=error,
        ),
        db=db,
    )


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    *,
    db: Session | None = None,
    operation: str = "chat",
) -> dict[str, Any]:
    """非流式对话，并为每次真实 provider 调用持久化脱敏 trace。"""
    started_at = time.perf_counter()
    input_estimate = _estimate_tokens(_message_text(messages))
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    tool_count = 0
    trace_capability = "tool" if tools else "chat"
    try:
        _ensure_callable(cfg, tools=bool(tools))
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
        # 推理模型的思考过程会占用 max_tokens；若首轮仅长度截断则放大一次。
        for attempt in range(2):
            resp = client.chat.completions.create(**kwargs)
            response_choice = resp.choices[0]
            choice = response_choice.message
            tool_calls = []
            for tc in getattr(choice, "tool_calls", None) or []:
                try:
                    args = _loads(tc.function.arguments)
                except Exception:  # noqa: BLE001
                    args = {}
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": args},
                    }
                )
            content = getattr(choice, "content", "") or ""
            # 工具参数同样会消耗输出 token；只在内存里用于没有 provider usage
            # 时的估算，绝不把其内容写入 trace。
            fallback_output = _estimate_tokens(
                content + json.dumps(tool_calls, ensure_ascii=False, default=str)
            )
            usage = getattr(resp, "usage", None)
            attempt_input, attempt_output, attempt_total = _usage_counts(
                usage,
                fallback_input=input_estimate,
                fallback_output=fallback_output,
            )
            input_tokens += attempt_input
            output_tokens += attempt_output
            total_tokens += attempt_total
            tool_count = len(tool_calls)
            # OpenAI 将 finish_reason 放在 choice 层；兼容少数供应商仍放在 message
            # 层的实现。
            truncated = (
                getattr(response_choice, "finish_reason", None)
                or getattr(choice, "finish_reason", None)
            ) == "length"
            if attempt == 0 and truncated and not content.strip() and not tool_calls:
                kwargs["max_tokens"] = (kwargs.get("max_tokens") or 4096) * 2
                continue
            _record_trace(
                cfg,
                db=db,
                capability=trace_capability,
                operation=operation,
                status="succeeded",
                started_at=started_at,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                tool_count=tool_count,
            )
            return {"content": content, "tool_calls": tool_calls, "raw": resp}
        # 防御性回退；循环正常应在前面 return。
        _record_trace(
            cfg,
            db=db,
            capability=trace_capability,
            operation=operation,
            status="succeeded",
            started_at=started_at,
            input_tokens=input_tokens or input_estimate,
            output_tokens=output_tokens,
            total_tokens=total_tokens or (input_tokens or input_estimate) + output_tokens,
            tool_count=tool_count,
        )
        return {"content": content, "tool_calls": tool_calls, "raw": resp}
    except Exception as exc:
        failed_input = input_tokens or input_estimate
        _record_trace(
            cfg,
            db=db,
            capability=trace_capability,
            operation=operation,
            status="failed",
            started_at=started_at,
            input_tokens=failed_input,
            output_tokens=output_tokens,
            total_tokens=total_tokens or failed_input + output_tokens,
            tool_count=tool_count,
            error=exc,
        )
        raise


def chat_stream(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    *,
    db: Session | None = None,
) -> Iterator[dict[str, Any]]:
    """流式对话；在完整结束、异常或取消后都写入一次脱敏 trace。"""
    started_at = time.perf_counter()
    input_estimate = _estimate_tokens(_message_text(messages))
    content_parts: list[str] = []
    tc_acc: dict[int, dict[str, Any]] = {}
    usage: Any = None
    status = "succeeded"
    error: Any = ""
    trace_capability = "tool" if tools else "chat"
    try:
        _ensure_callable(cfg, tools=bool(tools))
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
        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "token", "content": delta.content}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tc_acc.setdefault(
                        tc.index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["function"]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["function"]["arguments"] += tc.function.arguments
        if tc_acc:
            tool_calls = []
            for index in sorted(tc_acc):
                slot = tc_acc[index]
                try:
                    args = _loads(slot["function"]["arguments"])
                except Exception:  # noqa: BLE001
                    args = {}
                tool_calls.append(
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {"name": slot["function"]["name"], "arguments": args},
                    }
                )
            yield {"type": "tool_calls", "tool_calls": tool_calls}
    except GeneratorExit:
        status = "cancelled"
        raise
    except Exception as exc:
        status = "failed"
        error = exc
        raise
    finally:
        output_text = "".join(content_parts)
        if tc_acc:
            # 仅用于估算工具调用输出 token，不会进入 trace 持久化字段。
            output_text += json.dumps(tc_acc, ensure_ascii=False, default=str)
        input_tokens, output_tokens, total_tokens = _usage_counts(
            usage,
            fallback_input=input_estimate,
            fallback_output=_estimate_tokens(output_text),
        )
        _record_trace(
            cfg,
            db=db,
            capability=trace_capability,
            operation="chat_stream",
            status=status,
            started_at=started_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            tool_count=len(tc_acc),
            error=error,
        )


def _loads(s: str) -> dict[str, Any]:
    s = (s or "").strip()
    if not s:
        return {}
    return json.loads(s)


def test_connection(cfg: LLMConfig, *, db: Session | None = None) -> tuple[bool, str]:
    try:
        result = chat(
            cfg,
            [{"role": "user", "content": "你好，请回复“ok”"}],
            max_tokens=16,
            db=db,
            operation="test",
        )
        return True, f"连接成功，模型响应: {sanitize_trace_text(result['content'], max_length=50)}"
    except Exception as exc:  # noqa: BLE001
        return False, sanitize_trace_error(exc)
