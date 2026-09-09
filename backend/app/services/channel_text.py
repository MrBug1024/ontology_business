"""Plain text rendering for channels without rich text support."""
from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token


_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


def _inline(tokens: list[Token]) -> str:
    parts: list[str] = []
    links: list[str] = []
    for token in tokens:
        if token.type in {"text", "code_inline", "html_inline"}:
            parts.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.type == "image":
            parts.append(token.content)
        elif token.type == "link_open":
            links.append(token.attrGet("href") or "")
        elif token.type == "link_close" and links:
            target = links.pop()
            if target.startswith(("https://", "http://")):
                parts.append(f" ({target})")
    return "".join(parts)


def plain_text(value: str) -> str:
    parts: list[str] = []
    for token in _MARKDOWN.parse(str(value)[:200_000]):
        if token.type == "inline":
            parts.append(_inline(token.children or []))
        elif token.type in {"fence", "code_block"}:
            parts.extend([token.content.rstrip(), "\n"])
        elif token.type == "list_item_open":
            parts.append("- ")
        elif token.type in {"paragraph_close", "heading_close", "list_item_close", "tr_close"}:
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
        elif token.type in {"td_close", "th_close"}:
            parts.append(" | ")
    return "".join(parts).strip()
