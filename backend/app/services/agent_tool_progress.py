"""Public tool lifecycle metadata, without arguments, outputs or model reasoning."""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentToolProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=100)
    phase: Literal["started", "finished"]
    status: Literal[
        "running", "returned", "succeeded", "failed", "pending", "queued",
        "awaiting_confirmation", "awaiting_approval", "indeterminate", "cancelled",
        "rejected", "timed_out",
    ]
    invocation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    error_code: str | None = Field(default=None, max_length=80)


def public_tool_progress(event: Mapping[str, Any]) -> AgentToolProgress | None:
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    call_id, name = data.get("id"), data.get("name")
    if not (isinstance(call_id, str) and re.fullmatch(r"[\w.:-]{1,160}", call_id, re.ASCII)
            and isinstance(name, str) and re.fullmatch(r"[\w.-]{1,100}", name, re.ASCII)):
        return None
    if event.get("type") == "tool_call":
        return AgentToolProgress(call_id=call_id, name=name, phase="started", status="running")
    value = data.get("result")
    if isinstance(value, str) and len(value) <= 1_000_000:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            value = None
    result = value if isinstance(value, Mapping) else {}
    fields: dict[str, Any] = dict(call_id=call_id, name=name, phase="finished", status="returned")
    status = result.get("status")
    if isinstance(status, str) and status in {"running", "succeeded", "failed", "pending", "queued", "awaiting_confirmation",
                  "awaiting_approval", "indeterminate", "cancelled", "rejected", "timed_out"}:
        fields["status"] = status
    error = result.get("error")
    if (error or result.get("ok") is False or result.get("success") is False) and fields["status"] in {"returned", "running", "succeeded"}:
        fields["status"] = "failed"
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", code):
            fields["error_code"] = code
    invocation_id = result.get("invocation_id")
    if isinstance(invocation_id, str) and re.fullmatch(r"[a-f0-9]{32}", invocation_id):
        fields["invocation_id"] = invocation_id
    return AgentToolProgress(**fields)
