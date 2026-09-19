"""Static built-in investigation method; tenant paths/code cannot be loaded."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def discovery_skill() -> str:
    path = Path(__file__).resolve().parents[2] / "skills" / "business-discovery" / "SKILL.md"
    content = path.read_text(encoding="utf-8")
    if len(content) > 12_000:
        raise ValueError("内置业务调查技能超过上下文边界")
    return content
