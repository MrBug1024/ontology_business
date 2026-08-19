"""RAG 服务：对文件桶中已解析的文档做关键词检索，返回相关片段。

采用轻量级 TF 打分（无需外部向量库），对中文按字符 bigram 切分以提升召回。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BucketFile, DataSource

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    """切分为 token：英文/数字按词，中文按单字 + bigram。"""
    text = text.lower()
    toks: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        w = m.group(0)
        if re.match(r"[\u4e00-\u9fff]", w):
            toks.append(w)
            for i in range(len(w) - 1):
                toks.append(w[i : i + 2])
        else:
            toks.append(w)
    return toks


def _chunk(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def search(
    db: Session,
    data_source_ids: list[str],
    query: str,
    top_k: int = 5,
    max_chars: int = 4000,
) -> list[dict[str, Any]]:
    """在指定文件桶数据源中检索与 query 相关的文档片段。"""
    if not data_source_ids or not query.strip():
        return []
    q_tokens = _tokens(query)
    if not q_tokens:
        return []
    q_set = set(q_tokens)

    stmt = select(BucketFile).where(
        BucketFile.data_source_id.in_(data_source_ids),
        BucketFile.status == "parsed",
        BucketFile.parsed_text != "",
    )
    files = db.execute(stmt).scalars().all()

    scored: list[tuple[float, str, str, str]] = []  # (score, filename, source, chunk)
    for f in files:
        chunks = _chunk(f.parsed_text or "")
        for ch in chunks:
            c_tokens = _tokens(ch)
            if not c_tokens:
                continue
            c_set = set(c_tokens)
            inter = q_set & c_set
            if not inter:
                continue
            # 简单 TF-IDF 风格打分
            score = sum(1.0 / (1 + c_tokens.count(t)) for t in inter) * (len(inter) / len(q_set))
            scored.append((score, f.filename, f.data_source_id, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    results: list[dict[str, Any]] = []
    total = 0
    for score, filename, source_id, ch in scored[:top_k]:
        if total + len(ch) > max_chars:
            ch = ch[: max(0, max_chars - total)]
        total += len(ch)
        results.append({"filename": filename, "data_source_id": source_id, "score": round(score, 4), "text": ch})
        if total >= max_chars:
            break
    return results


def build_context(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    parts = ["以下是从业务数据文件中检索到的相关内容，可作为回答依据："]
    for i, r in enumerate(results, 1):
        parts.append(f"\n【资料 {i}】来源文件: {r['filename']}\n{r['text']}")
    return "\n".join(parts)
