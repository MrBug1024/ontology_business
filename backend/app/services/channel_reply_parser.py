"""Exact business replies; model prose is never an authorization signal."""
from __future__ import annotations

from dataclasses import dataclass
import re


_REPLY = re.compile(r"^(确认执行|确认|同意|批准|驳回|拒绝|取消)(?:\s+([CA]-[a-f0-9]{10}))?$", re.IGNORECASE)
_ACTIONS = {"确认执行": "confirm", "确认": "confirm", "同意": "approve", "批准": "approve", "驳回": "reject", "拒绝": "reject", "取消": "cancel"}


@dataclass(frozen=True)
class ParsedReply:
    action: str
    code: str | None
    comment: str


def parse_reply(text: str) -> ParsedReply | None:
    first, _, comment = text.strip().partition("\n")
    matched = _REPLY.fullmatch(first.strip())
    if matched is None:
        return None
    code = matched.group(2)
    return ParsedReply(_ACTIONS[matched.group(1)], code[0].upper() + code[1:].lower() if code else None, comment.strip()[:4000])
