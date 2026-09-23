"""Bounded automatic discovery of authorized data relationships.

The service owns the mechanical work that should not be delegated to an LLM:
enumerating the current modeling sources, choosing representative tables and
fields, reading small samples, and ranking exact-value overlaps. It returns
candidate evidence only. Business meaning, lineage direction, and cardinality
remain explicit human/AI review decisions.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_sample_schemas import DatabaseSampleArguments
from ..distillation_schemas import DistillationDocument, Evidence
from ..models import BucketFile, DataSource
from . import (
    distillation_lineage_service,
    distillation_library_service,
    distillation_sample_service,
    library_database_service,
    library_sample_query,
    permission_service,
    release_service,
)
from .distillation_evidence_service import capture_evidence_identity


MAX_SOURCES = 5
MAX_TABLES_PER_SOURCE = 8
MAX_SAMPLES = 12
MAX_SAMPLE_ROWS = 30
MAX_SAMPLE_FIELDS = 12
MAX_FILES_PER_SOURCE = 2
MAX_RESULT_BYTES = 220_000
MAX_DISCOVERY_SECONDS = 90
MAX_SOURCE_SECONDS = 30
MAX_CONNECTOR_READ_SECONDS = 8
KEY_NAME_PATTERN = re.compile(r"(?:^|[_\- ])(?:id|key|code|no|number)$|(?:id|key|code|no)$", re.IGNORECASE)


class LandscapeRead:
    """A source-scoped read and its safe report, kept separate for receipts."""

    def __init__(self, evidence: Evidence, identity: dict, content: dict):
        self.evidence = evidence
        self.identity = identity
        self.content = content


@dataclass(frozen=True)
class SourceReference:
    """Detached source identity used after the authorization transaction ends."""

    id: str
    type: str
    name: str


def discover(
    db: Session,
    scenario_id: str | None,
    *,
    document: DistillationDocument | None = None,
    max_sources: int = MAX_SOURCES,
    max_tables_per_source: int = MAX_TABLES_PER_SOURCE,
    max_samples: int = MAX_SAMPLES,
) -> tuple[dict, list[LandscapeRead]]:
    """Discover relationships in the exact source scope visible to the caller."""
    limits = _limits(max_sources, max_tables_per_source, max_samples)
    source_roles = _source_roles(document)
    sources = db.scalars(
        select(DataSource)
        .where(*distillation_library_service.authorized_source_filters(
            db, scenario_id, include_shared=True))
        .order_by(DataSource.created_at.desc(), DataSource.id)
        .limit(limits["max_sources"] + 1)
    ).all()
    truncated_sources = len(sources) > limits["max_sources"]
    sources = sources[:limits["max_sources"]]
    # No connector or object-storage I/O may run while this transaction holds
    # authorization rows or ORM locks.
    snapshots = [(
        SourceReference(str(source.id), str(source.type), str(source.name)),
        library_database_service.snapshot(source)
        if source.type in library_database_service.DATABASE_TYPES else None,
    ) for source in sources]
    db.commit()

    reads: list[LandscapeRead] = []
    skipped_sources: list[dict[str, str]] = []
    if truncated_sources:
        skipped_sources.append({"source": "当前授权资料范围", "reason": "资料来源超过本轮上限，剩余来源留待下一轮自动探查"})
    deadline = time.monotonic() + MAX_DISCOVERY_SECONDS
    for source, snapshot in snapshots:
        if len(reads) >= distillation_library_service.MAX_LIBRARY_READS:
            skipped_sources.append({"source": _safe_label(source.name), "reason": "已达到本轮样本上限"})
            continue
        if time.monotonic() >= deadline:
            skipped_sources.append({"source": _safe_label(source.name), "reason": "自动探查达到总时限，剩余资料留待下一轮"})
            continue
        try:
            if source.type in library_database_service.DATABASE_TYPES:
                reads.extend(_discover_database_source(
                    db, scenario_id, source, snapshot, limits,
                    min(limits["max_samples"], distillation_library_service.MAX_LIBRARY_READS - len(reads)),
                    min(deadline, time.monotonic() + MAX_SOURCE_SECONDS), source_roles.get(source.id)))
            elif source.type == "file_bucket":
                reads.extend(_discover_file_source(
                    db, scenario_id, source, limits,
                    min(limits["max_samples"], distillation_library_service.MAX_LIBRARY_READS - len(reads)),
                    min(deadline, time.monotonic() + MAX_SOURCE_SECONDS), source_roles.get(source.id)))
            else:
                skipped_sources.append({
                    "source": _safe_label(source.name),
                    "reason": "该资料类型当前只提供文档或目录读取，不执行业务行探查",
                })
        except HTTPException:
            raise
        except Exception:
            # Connector details and object identities are never returned as
            # model-visible errors. The rest of the authorized scope remains
            # useful and is reported as partial discovery.
            skipped_sources.append({
                "source": _safe_label(source.name),
                "reason": "资料结构或样本读取失败、超时，或超过受控边界",
            })

    samples = [
        {"evidence_key": read.evidence.key, "content": read.content}
        for read in reads
        if read.content.get("kind") == "data_landscape_source"
    ]
    flat_samples = _flat_sample_reads(samples)
    lineage = _infer(flat_samples)
    source_reports = [read.content.get("source", {}) for read in reads]
    content = {
        "kind": "data_landscape",
        "engine": "bounded_schema_sample_relationships/v1",
        "sources": source_reports,
        "relationship_candidates": lineage["candidates"],
        "candidate_stats": lineage["stats"],
        "skipped_sources": skipped_sources + lineage["skipped_sources"],
        "limits": limits,
        "limitations": [
            "只探查当前场景和工作区共享范围内、当前账号可读的资料；未授权资料不会被发现。",
            "样本关联是候选证据，不证明业务含义、全量唯一性、流程先后或因果关系。",
            "字段名和主键形状只用于排序与解释；方向、实体含义、关系基数和转换规则仍需核对。",
            "资料过多、结构超限、连接失败或样本不足时返回部分结果，不把未读取内容写成事实。",
        ],
    }
    if len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT_BYTES:
        raise HTTPException(422, "自动数据探查结果超过本轮边界，请缩小资料范围后重试")
    return content, reads


def _limits(max_sources: int, max_tables_per_source: int, max_samples: int) -> dict[str, int]:
    values = {
        "max_sources": max(1, min(int(max_sources), MAX_SOURCES)),
        "max_tables_per_source": max(1, min(int(max_tables_per_source), MAX_TABLES_PER_SOURCE)),
        "max_samples": max(1, min(int(max_samples), MAX_SAMPLES)),
        "max_rows_per_sample": MAX_SAMPLE_ROWS,
        "max_fields_per_sample": MAX_SAMPLE_FIELDS,
    }
    return values


def _safe_label(value: str) -> str:
    safe = release_service.safe_snapshot_content({"label": value}).get("label")
    return safe[:200] if isinstance(safe, str) else "已隐藏敏感名称"


def _discover_database_source(
    db: Session,
    scenario_id: str | None,
    source: SourceReference,
    snapshot: DataSource,
    limits: dict[str, int],
    remaining: int,
    deadline: float,
    source_role: str | None,
) -> list[LandscapeRead]:
    schema = library_database_service.database_schema(
        snapshot, timeout_seconds=max(0.1, min(MAX_CONNECTOR_READ_SECONDS, deadline - time.monotonic())))
    if not isinstance(schema, list):
        raise ValueError("资料结构读取结果无效")
    tables = library_sample_query.catalog(schema)
    selected = _rank_tables(schema, tables, limits["max_tables_per_source"])
    samples: list[dict] = []
    sampled_table_names: list[str] = []
    for raw_table, table in selected[:remaining]:
        if time.monotonic() >= deadline:
            break
        field_keys = _rank_fields(raw_table, table, limits["max_fields_per_sample"])
        if not field_keys:
            continue
        args = DatabaseSampleArguments(
            data_source_id=source.id,
            table_key=table["table_key"],
            field_keys=field_keys,
            limit=limits["max_rows_per_sample"],
        )
        result = library_database_service.database_schema(
            snapshot, timeout_seconds=max(0.1, min(MAX_CONNECTOR_READ_SECONDS, deadline - time.monotonic())), sample=args)
        if isinstance(result, dict) and result.get("kind") == "database_sample":
            samples.append({**result, **({"role": source_role} if source_role else {})})
            sampled_table_names.append(str(table.get("name") or "未命名表"))
    return [_finish_source_read(db, scenario_id, source, {
        "kind": "data_landscape_source",
        "source": {
            "name": _safe_label(source.name),
            "type": source.type,
            **({"role": source_role} if source_role else {}),
            "sampled_tables": sampled_table_names,
            "tables": tables[:limits["max_tables_per_source"]],
            "sample_count": len(samples),
        },
        "samples": samples,
        "limitations": [
            f"自动选择了最多 {limits['max_tables_per_source']} 张代表性表和 {limits['max_rows_per_sample']} 行样本；未读取的表不代表不存在关系。",
            "数据库样本通过只读连接和服务端生成的结构引用读取，未接受 SQL、物理路径或凭据。",
        ],
    })]


def _discover_file_source(
    db: Session,
    scenario_id: str | None,
    source: SourceReference,
    limits: dict[str, int],
    remaining: int,
    deadline: float,
    source_role: str | None,
) -> list[LandscapeRead]:
    files = db.scalars(select(BucketFile).where(
        BucketFile.data_source_id == source.id,
        BucketFile.status.in_(("pending", "parsed")),
    ).order_by(BucketFile.id).limit(min(MAX_FILES_PER_SOURCE, remaining))).all()
    if not files:
        return []
    reads: list[LandscapeRead] = []
    for file in files:
        if time.monotonic() >= deadline:
            break
        read = distillation_sample_service.read_sample(db, scenario_id, DatabaseSampleArguments(
            data_source_id=source.id,
            bucket_file_id=file.id,
            limit=limits["max_rows_per_sample"],
        ))
        content = read.content
        if content.get("kind") == "database_catalog":
            tables = content.get("tables") or []
            if not tables:
                continue
            read = distillation_sample_service.read_sample(db, scenario_id, DatabaseSampleArguments(
                data_source_id=source.id,
                bucket_file_id=file.id,
                table_key=tables[0]["table_key"],
                limit=limits["max_rows_per_sample"],
            ))
            content = read.content
        if content.get("kind") != "database_sample":
            continue
        reads.append(_finish_source_read(db, scenario_id, source, {
            "kind": "data_landscape_source",
            "source": {
                "name": _safe_label(file.filename),
                "type": source.type,
                **({"role": source_role} if source_role else {}),
                "tables": content.get("tables", []),
                "sample_count": 1,
            },
            "samples": [{**content, **({"role": source_role} if source_role else {})}],
            "limitations": ["文件仅按工作表顺序读取一份有界样本；需核对表头、跨表关系和原文件覆盖范围。"],
        }, existing_read=read))
    return reads


def _finish_source_read(
    db: Session,
    scenario_id: str | None,
    source: SourceReference,
    content: dict,
    *,
    existing_read: object | None = None,
) -> LandscapeRead:
    if existing_read is not None:
        content = {**content, "source": {**content.get("source", {}), "evidence_key": existing_read.evidence.key}}
        return LandscapeRead(existing_read.evidence, existing_read.identity, content)
    evidence = Evidence(
        key="landscape_" + uuid.uuid4().hex[:20],
        title=_safe_label(source.name) + " · 自动数据探查",
        kind="material",
        data_source_id=source.id,
        coverage="服务端自动探查的受管结构和有界样本",
        limitations="抽样不代表全量；候选关系和血缘仍需业务核对。",
    )
    document = DistillationDocument(evidence=[evidence])
    identity = capture_evidence_identity(db, document, scenario_id)
    permission_service.refresh_request_authorization(db)
    if capture_evidence_identity(db, document, scenario_id) != identity:
        raise HTTPException(409, "读取期间资料配置或权限已变化，请重新调查")
    content = {**content, "source": {**content.get("source", {}), "evidence_key": evidence.key}}
    return LandscapeRead(evidence, identity, content)


def _source_roles(document: DistillationDocument | None) -> dict[str, str]:
    if document is None:
        return {}
    grouped: dict[str, set[str]] = {}
    for evidence in document.evidence:
        if evidence.data_source_id and evidence.role in {"input", "result"}:
            grouped.setdefault(evidence.data_source_id, set()).add(evidence.role)
    return {source_id: next(iter(roles)) for source_id, roles in grouped.items() if len(roles) == 1}


def _rank_tables(schema: list[dict], tables: list[dict], limit: int) -> list[tuple[dict, dict]]:
    raw_by_name = {str(item.get("name")): item for item in schema}
    ranked = []
    for table in tables:
        raw = raw_by_name.get(str(table.get("name")), {})
        columns = raw.get("columns") or []
        primary_count = sum(bool(column.get("pk")) for column in columns)
        keyish_count = sum(bool(KEY_NAME_PATTERN.search(str(column.get("name") or ""))) for column in columns)
        ranked.append((-(primary_count * 4 + keyish_count), str(table.get("name") or ""), raw, table))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [(item[2], item[3]) for item in ranked[:limit]]


def _rank_fields(raw_table: dict, table: dict, limit: int) -> list[str]:
    raw_columns = {str(item.get("name")): item for item in raw_table.get("columns") or []}
    fields = table.get("fields") or []
    ranked = []
    for index, field in enumerate(fields):
        raw = raw_columns.get(str(field.get("name")), {})
        keyish = bool(raw.get("pk")) or bool(KEY_NAME_PATTERN.search(str(field.get("name") or "")))
        ranked.append((0 if keyish else 1, index, str(field.get("field_key"))))
    ranked.sort()
    return [item[2] for item in ranked[:limit]]


def _flat_sample_reads(reads: Iterable[dict]) -> list[dict]:
    result = []
    for read in reads:
        content = read.get("content") if isinstance(read, dict) else None
        if not isinstance(content, dict):
            continue
        for index, sample in enumerate(content.get("samples") or []):
            if isinstance(sample, dict) and sample.get("kind") == "database_sample":
                result.append({
                    "evidence_key": read.get("evidence_key") or f"sample_{index}",
                    "content": sample,
                })
    return result


def _infer(reads: list[dict]) -> dict:
    if len(reads) < 2:
        return {
            "candidates": [],
            "skipped_sources": [{"evidence_key": "landscape", "reason": "当前只获得一份可用样本，暂不能比较跨表关系"}],
            "stats": {"source_count": len(reads), "compared_field_pairs": 0, "candidate_count": 0},
        }
    return distillation_lineage_service.infer_lineage_candidates(reads)


__all__ = ["LandscapeRead", "discover", "MAX_SOURCES", "MAX_TABLES_PER_SOURCE", "MAX_SAMPLES"]
