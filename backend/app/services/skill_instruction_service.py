"""Read the bounded instructions of an already authorized trusted skill.

Callers own tenant visibility and enabled-state checks. This reader grants no
script execution and never treats a database path as trusted on its own.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import SKILLS_DIR
from ..models import Skill
from . import release_service


MAX_SKILL_BYTES = 64_000
UNAVAILABLE_MESSAGE = "所选技能不是可用的受信方法包，请刷新后重新选择"


def read_instructions(skill: Skill) -> str:
    if skill.source != "builtin":
        raise ValueError(UNAVAILABLE_MESSAGE)
    root = SKILLS_DIR.resolve()
    directory = Path(skill.path).resolve()
    path = (directory / "SKILL.md").resolve()
    if directory == root or not directory.is_relative_to(root) or not path.is_relative_to(directory):
        raise ValueError(UNAVAILABLE_MESSAGE)
    try:
        with path.open("rb") as source:
            content = source.read(MAX_SKILL_BYTES + 1)
        if len(content) > MAX_SKILL_BYTES:
            raise ValueError(UNAVAILABLE_MESSAGE)
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(UNAVAILABLE_MESSAGE) from exc
    if not text.strip() or release_service.safe_snapshot_content({"instructions": text}) != {"instructions": text}:
        raise ValueError(UNAVAILABLE_MESSAGE)
    return text


def content_fingerprint(skill: Skill) -> str:
    return hashlib.sha256(read_instructions(skill).encode()).hexdigest()
