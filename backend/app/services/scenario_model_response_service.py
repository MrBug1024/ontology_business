"""Collect one bounded compiler response through the shared streaming runtime."""
from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
import traceback
from typing import Any

from sqlalchemy.orm import Session

from ..models import LLMConfig
from . import llm_service


def extract_model_output(content: str) -> dict[str, Any]:
    """Read one final definition, excluding only explicit leading reasoning blocks."""
    text = content.strip()
    while text.lower().startswith('<think>'):
        close = re.search(r'<(?:/|\\)think>', text, flags=re.I)
        if close is None:
            raise ValueError('模型的前导推理块未闭合，不能作为完整定义')
        text = text[close.end():].lstrip()
    if text.startswith('```'):
        fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\s*```', text, flags=re.S | re.I)
        if fenced is None:
            raise ValueError('模型 JSON 代码块未完整结束')
        text = fenced.group(1)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('模型 JSON 含有重复字段')
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError('模型 JSON 含有非有限数字')

    def finite_float(raw):
        number = float(raw)
        if not math.isfinite(number):
            reject_constant(raw)
        return number

    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant, parse_float=finite_float)
    if not isinstance(value, dict):
        raise ValueError('模型必须返回单个 JSON 对象')
    return value


def log_failure(logger, error: Exception, *, job_id: str) -> None:
    """Keep code locations, never exception values, model content or local variables."""
    chain = []
    current = error
    for _ in range(4):
        frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(current.__traceback__)[-12:]]
        chain.append((type(current).__name__, frames))
        if current.__cause__ is None:
            break
        current = current.__cause__
    logger.error('Compiler failed job=%s failures=%s', job_id, chain)


def chat(cfg: LLMConfig, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int,
         request_timeout: float, max_retries: int, db: Session | None,
         before_provider_call: Callable[[], None] | None) -> dict[str, Any]:
    parts = []
    finish_reason = None
    response = llm_service.chat_stream(cfg, messages, temperature=temperature, max_tokens=max_tokens,
        request_timeout=request_timeout, max_retries=max_retries, db=db,
        before_provider_call=before_provider_call, operation="scenario_model_compile",
        include_finish_reason=True, total_timeout=request_timeout, max_output_chars=max_tokens * 12)
    try:
        for item in response:
            if item["type"] == "token":
                parts.append(item["content"])
            elif item["type"] == "finish":
                finish_reason = item["finish_reason"]
    finally:
        response.close()
    if finish_reason not in {"stop", "length"}:
        raise llm_service.LLMRuntimeError("模型未返回完整可校验的结构结果")
    return {"content": "".join(parts), "raw": {"choices": [{"finish_reason": finish_reason}]}}
