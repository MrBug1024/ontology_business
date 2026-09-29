"""Advisor discovery, provenance and reauthorization of immutable handoff material."""
from __future__ import annotations

import re
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_models import DistillationPublication
from ..models import DataSource
from . import distillation_service, permission_service
from .distillation_artifact_service import HANDOFF_GUIDANCE


CITATION_FIELDS = frozenset({"kind", "data_source_id", "publication_id", "file_content_hash"})
MAX_STRUCTURED_UNIT_CHARS = 8_000


def _compilation_units(content: str) -> list[str] | None:
    """Keep JSON values intact so field lists retain their owning object."""
    try:
        document = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    units: list[str] = []

    def append(path: list[str | int], value: object) -> bool:
        encoded = json.dumps({"source_path": path, "value": value},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) <= MAX_STRUCTURED_UNIT_CHARS:
            units.append(encoded)
            return True
        if isinstance(value, dict):
            return all(append([*path, key], child) for key, child in value.items())
        if isinstance(value, list):
            return all(append([*path, index], child) for index, child in enumerate(value))
        return False

    try:
        if all(append([key], value) for key, value in document.items()):
            return units
    except (ValueError, RecursionError):
        pass
    # Oversized scalar text still uses the lossless ordinary paragraph path.
    return None


def complete_compilation_documents(documents: list[dict], *, max_chars: int) -> list[dict]:
    """Read complete frozen handoffs, without applying conversational top-k retrieval."""
    result = []
    total = 0
    for document in documents:
        content = document.get("parsed_text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("业务蒸馏交接缺少完整结构化契约")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash != document.get("content_hash"):
            raise ValueError("业务蒸馏交接内容身份不一致")
        total += len(content)
        if total > max_chars:
            raise ValueError("业务蒸馏交接超过单次编译总量上限，请按业务域拆分")
        result.append({**document, "characters": len(content), "parsed_text_hash": content_hash,
            "compilation_units": _compilation_units(content),
            "chunk_count": 1, "retrieved_passage_count": 1, "retrieved_characters": len(content),
            "retrieval_complete": True, "passages": [{"text": content, "char_start": 0,
                                                       "char_end": len(content)}]})
    return result


def normalize_citation(source: dict) -> dict:
    normalized = {"kind": "distillation", "data_source_id": str(source.get("data_source_id") or ""),
        "publication_id": str(source.get("publication_id") or ""),
        "file_content_hash": str(source.get("file_content_hash") or "")}
    if source.get("kind") != "distillation" or any(
        not normalized[key] or len(normalized[key]) > 32 for key in ("data_source_id", "publication_id")
    ) or re.fullmatch(r"[a-f0-9]{64}", normalized["file_content_hash"]) is None:
        raise ValueError("business distillation source citation is incomplete")
    return normalized


def require_compilation_decision(documents: list[dict]) -> None:
    if any(item.get("business_decision") in {"stop", "undecided"} for item in documents):
        raise ValueError("业务蒸馏资料包含停止或未决结论，请先核对业务价值、修改人工决策并重新交接，才能建设场景能力")


def advisor_context(db: Session, scenario_id: str) -> tuple[str, list[dict]]:
    documents = distillation_service.modeling_documents(db, scenario_id)
    if not documents:
        return "", []
    parts = [HANDOFF_GUIDANCE]
    sources = []
    remaining = 24_000
    for document in documents:
        text = document["parsed_text"]
        excerpt = text[:remaining]
        if not excerpt:
            break
        parts.append(f"\n业务蒸馏资料：{document['filename']}\n{excerpt}")
        remaining -= len(excerpt)
        if len(text) > len(excerpt):
            parts.append("本次对话只读取该资料的有界节选；完整建设请使用场景模型编译并核对未决问题。")
        sources.append({"id": document["id"], "kind": "distillation", "filename": document["filename"],
            "data_source_id": document["data_source_id"], "publication_id": document["publication_id"],
            "file_content_hash": document["content_hash"], "status": "cited", "usage_plane": "modeling_material"})
    return "\n".join(parts), sources


def current_source(db: Session, scenario_id: str, source_meta: dict) -> DataSource | None:
    principal = permission_service.require_principal(db)
    source = db.scalar(select(DataSource).where(DataSource.id == source_meta.get("data_source_id"),
        DataSource.tenant_id == principal.tenant_id, DataSource.type == "distillation",
        DataSource.resource_scope == "modeling"))
    if source is None or source.scenario_id != scenario_id:
        return None
    try:
        distillation_service.authorize_scope(db, scenario_id)
        publication = db.scalar(select(DistillationPublication).where(
            DistillationPublication.id == source_meta.get("publication_id"),
            DistillationPublication.data_source_id == source.id,
            DistillationPublication.tenant_id == principal.tenant_id))
        if publication is None:
            return None
        if publication.document.get("decision") not in {"continue", "adjust"}:
            return None
        artifact = distillation_service.artifact_content(publication, "brief")
    except HTTPException:
        return None
    return source if artifact["sha256"] == source_meta.get("file_content_hash") else None
