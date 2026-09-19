"""Fetch only bounded file projections for the discovery prompt."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..distillation_schemas import Evidence
from ..models import BucketFile
from . import release_service


MAX_FILE_EXCERPT_CHARS = 12_000
MAX_FILE_EVIDENCE_CHARS = 48_000


def file_excerpts(db: Session, evidence: Evidence, remaining: int) -> tuple[list[dict], list[str], int]:
    if remaining <= 0:
        return [], [f"证据 {evidence.key} 未读取：本次资料正文额度已用完，请缩小范围后重试。"], 0
    length = min(MAX_FILE_EXCERPT_CHARS, remaining)
    query = select(BucketFile.id, BucketFile.filename, BucketFile.status, BucketFile.content_sha256,
        func.char_length(BucketFile.parsed_text).label("characters"),
        func.substr(BucketFile.parsed_text, 1, length).label("excerpt"),
    ).where(BucketFile.data_source_id == evidence.data_source_id).order_by(BucketFile.id)
    if evidence.bucket_file_id:
        query = query.where(BucketFile.id == evidence.bucket_file_id)
    files = db.execute(query.limit(6)).mappings().all()
    limitations = []
    if len(files) > 5:
        limitations.append(f"证据 {evidence.key} 文件超过 5 个，仅分析前 5 个，请选择具体文件缩小范围。")
    snippets, consumed = [], 0
    for file in files[:5]:
        if consumed >= remaining:
            limitations.append(f"证据 {evidence.key} 存在未读取文件：本次资料正文额度已用完。")
            break
        if file["status"] != "parsed":
            limitations.append(f"证据 {evidence.key} 存在尚未成功解析的文件，未读取其内容。")
            continue
        raw = str(file["excerpt"] or "")[:remaining - consumed]
        sanitized = release_service.safe_snapshot_content({"content": raw}).get("content")
        if not isinstance(sanitized, str):
            limitations.append(f"证据 {evidence.key} 包含疑似凭据，内容已排除，请先上传脱敏版本。")
            continue
        consumed += len(sanitized)
        complete = int(file["characters"] or 0) <= len(raw)
        if not complete:
            limitations.append(f"证据 {evidence.key} 的内容超过本次分析界限，只读取有界节选。")
        filename = release_service.safe_snapshot_content({"filename": file["filename"]}).get("filename")
        snippets.append({"filename": filename if isinstance(filename, str) else "已隐藏敏感文件名", "content": sanitized,
                         "content_sha256": file["content_sha256"], "complete": complete})
    return snippets, limitations, consumed
