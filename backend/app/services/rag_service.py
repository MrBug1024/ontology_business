"""P1 文档检索：持久化分块、向量检索、来源引用与增量索引。

默认实现不依赖外部向量数据库：把稳定的本地语义哈希嵌入保存到
``document_chunks.embedding``，以便开发环境和离线部署也能完成可验证闭环。
服务边界保持在这里，后续接入托管 Embedding / 向量库时不会改变 API 契约。
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BucketFile, DataSource, DocumentChunk, DocumentIndexJob, LLMConfig
from . import llm_service, tenant_service


EMBEDDING_MODEL = "local-semantic-hash-192-v1"
EMBEDDING_DIMENSIONS = 192
INDEX_VERSION = "rag-chunks-v1"
CHUNK_SIZE = 760
CHUNK_OVERLAP = 140
MAX_CHUNKS_PER_FILE = 5_000
DOCUMENT_JOB_MAX_ATTEMPTS = 3
DOCUMENT_JOB_TIMEOUT_SECONDS = 300
DOCUMENT_JOB_RETRY_SECONDS = 5
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")

# 本地回退嵌入的有限语义扩展，不替代后续可配置的专用 Embedding 模型。
# 仅覆盖常见的通用业务同义概念，避免依赖某一个行业的数据字典。
_SYNONYM_GROUPS = (
    ("成本", "费用", "开支", "支出"),
    ("客户", "用户", "消费者", "买方"),
    ("供应商", "供方", "厂商", "vendor"),
    ("订单", "采购单", "销售单", "交易单"),
    ("风险", "隐患", "风险点", "异常"),
    ("审批", "审核", "核准", "批准"),
    ("合同", "协议", "约定"),
)
_SYNONYM_MAP = {term.lower(): group for group in _SYNONYM_GROUPS for term in group}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _document_active_key(file_id: str) -> str:
    """同一租户下一个文件只能有一个活跃解析/索引任务。"""
    return str(file_id)


def _tokens(text: str) -> list[str]:
    """以英文词、中文 1/2/3-gram 和少量同义扩展构建检索特征。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer((text or "").lower()):
        word = match.group(0)
        if not word:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            length = len(word)
            for width in (1, 2, 3):
                if length >= width:
                    tokens.extend(word[index : index + width] for index in range(length - width + 1))
        else:
            tokens.append(word)
        # 对完整词/短语做同义扩展；重复是有意的，向量权重会保留原词主导性。
        synonyms = _SYNONYM_MAP.get(word, ())
        if synonyms:
            # 同一组始终投射到相同特征，避免二元同义词仅生成彼此相反的扩展。
            tokens.append(f"syn:{synonyms[0]}")
        for synonym in synonyms:  # 英文词也可以被映射。
            if synonym != word:
                tokens.append(f"syn:{synonym}")
    return tokens


def _feature_slot(feature: str) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    slot = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
    sign = 1.0 if digest[4] & 1 else -1.0
    return slot, sign


def embed(text: str) -> list[float]:
    """生成 L2 归一化的本地嵌入向量，适合作为离线默认实现。"""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    counts: dict[str, int] = {}
    for token in _tokens(text):
        counts[token] = counts.get(token, 0) + 1
    for token, count in counts.items():
        slot, sign = _feature_slot(token)
        # 子线索权重较低，避免扩展词压过用户输入本身。
        weight = 0.58 if token.startswith("syn:") else 1.0
        vector[slot] += sign * weight * (1.0 + math.log(count))
    norm = math.sqrt(sum(value * value for value in vector))
    return [round(value / norm, 8) for value in vector] if norm else vector


def _cosine(left: Iterable[float] | None, right: Iterable[float] | None) -> float:
    left_values = list(left or [])
    right_values = list(right or [])
    if not left_values or len(left_values) != len(right_values):
        return 0.0
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left_values))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return max(
        0.0,
        sum(float(a) * float(b) for a, b in zip(left_values, right_values)) / (left_norm * right_norm),
    )


def _runtime_embedding_marker(config: LLMConfig) -> str:
    """保存配置 ID 而非展示名称，避免同名模型或改名后混用向量空间。"""
    return f"llm:{config.id}"


def _embedding_config_for_marker(db: Session, marker: str) -> LLMConfig | None:
    if not str(marker or "").startswith("llm:"):
        return None
    config_id = str(marker).split(":", 1)[1]
    config = tenant_service.get_visible(db, LLMConfig, config_id)
    if not config or not llm_service.supports_capability(config, "embedding"):
        return None
    return config


def _embed_for_index(db: Session, texts: list[str]) -> tuple[list[list[float]], str]:
    """优先使用当前租户路由的专用 Embedding；未配置时保留离线回退。"""
    candidates = llm_service.routable_configs(db, "embedding")
    if not candidates:
        return [embed(text) for text in texts], EMBEDDING_MODEL
    config = candidates[0]
    return llm_service.embed(config, texts, db=db, operation="rag_index"), _runtime_embedding_marker(config)


def _query_embedding_for_marker(db: Session, marker: str, query: str) -> list[float] | None:
    if marker == EMBEDDING_MODEL:
        return embed(query)
    config = _embedding_config_for_marker(db, marker)
    if not config:
        return None
    return llm_service.embed(config, [query], db=db, operation="rag_search")[0]


def _keyword_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    chunk_tokens = _tokens(text)
    if not chunk_tokens:
        return 0.0
    chunk_set = set(chunk_tokens)
    overlap = query_tokens & chunk_set
    if not overlap:
        return 0.0
    # 轻量 BM25 风格校正，防止非常长的分块因重复词虚高。
    tf = sum(1.0 / (1.0 + chunk_tokens.count(token)) for token in overlap)
    return min(1.0, (len(overlap) / len(query_tokens)) * tf)


def chunk_spans(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int, str]]:
    """按自然段/句末优先切分，并保留原文字符偏移供引用使用。"""
    source = text or ""
    if not source.strip():
        return []
    first = len(source) - len(source.lstrip())
    last = len(source.rstrip())
    spans: list[tuple[int, int, str]] = []
    start = first
    while start < last:
        ceiling = min(last, start + size)
        end = ceiling
        if ceiling < last:
            # 优先在段落、句末或空白处切开，避免给引用卡展示半句话。
            boundary = max(
                source.rfind("\n\n", start + max(80, size // 2), ceiling),
                source.rfind("。", start + max(80, size // 2), ceiling),
                source.rfind("！", start + max(80, size // 2), ceiling),
                source.rfind("？", start + max(80, size // 2), ceiling),
                source.rfind(". ", start + max(80, size // 2), ceiling),
                source.rfind(" ", start + max(80, size // 2), ceiling),
            )
            if boundary > start:
                end = min(last, boundary + 1)
        raw = source[start:end]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        if trailing > leading:
            actual_start = start + leading
            actual_end = start + trailing
            spans.append((actual_start, actual_end, source[actual_start:actual_end]))
        if end >= last:
            break
        next_start = max(start + 1, end - overlap)
        while next_start < last and source[next_start].isspace() and next_start < end:
            next_start += 1
        start = next_start
    return spans


def _require_owned_file(db: Session, file: BucketFile) -> DataSource:
    """索引会改写分块和状态，因此只能由资料库所属租户发起。"""
    tenant_id = tenant_service.current_tenant_id(db)
    source = file.data_source or db.get(DataSource, file.data_source_id)
    if not source or source.tenant_id != tenant_id:
        raise PermissionError("只有资料库所属租户可以建立或更新检索索引")
    return source


def _index_is_current(file: BucketFile) -> bool:
    return bool(
        file.index_status in {"indexed", "partial"}
        and file.index_version == INDEX_VERSION
        and file.indexed_content_hash == _content_hash(file.parsed_text or "")
    )


def index_file(db: Session, file: BucketFile, *, force: bool = False) -> dict[str, Any]:
    """按内容哈希增量建立一个文件的分块向量索引。

    不提交事务，由文档 worker 或显式管理入口决定何时提交。
    """
    _require_owned_file(db, file)
    text = file.parsed_text or ""
    if file.status != "parsed" or not text.strip():
        file.index_status = "pending" if file.status != "error" else "error"
        file.index_error = "文件尚未成功解析，无法建立检索索引"
        file.chunk_count = 0
        return {"file_id": file.id, "status": file.index_status, "indexed": False, "chunk_count": 0}

    source_hash = _content_hash(text)
    current = not force and _index_is_current(file)
    if current:
        return {"file_id": file.id, "status": "indexed", "indexed": False, "chunk_count": file.chunk_count}

    try:
        spans = chunk_spans(text)
        partial = len(spans) > MAX_CHUNKS_PER_FILE
        spans = spans[:MAX_CHUNKS_PER_FILE]
        vectors, embedding_model = _embed_for_index(
            db,
            [chunk_text for _start, _end, chunk_text in spans],
        )
        if len(vectors) != len(spans):
            raise RuntimeError("Embedding 返回数量与文档分块不一致")
        db.execute(delete(DocumentChunk).where(DocumentChunk.bucket_file_id == file.id))
        for ordinal, ((char_start, char_end, chunk_text), vector) in enumerate(zip(spans, vectors)):
            db.add(
                DocumentChunk(
                    bucket_file_id=file.id,
                    data_source_id=file.data_source_id,
                    ordinal=ordinal,
                    char_start=char_start,
                    char_end=char_end,
                    text=chunk_text,
                    content_hash=_content_hash(chunk_text),
                    embedding=vector,
                    embedding_model=embedding_model,
                )
            )
        file.index_status = "partial" if partial else "indexed"
        file.index_error = (
            f"文档过大，仅索引前 {MAX_CHUNKS_PER_FILE} 个片段；可拆分文件后获得完整覆盖。"
            if partial else ""
        )
        file.index_version = INDEX_VERSION
        file.indexed_content_hash = source_hash
        file.indexed_at = utc_now()
        file.chunk_count = len(spans)
        db.flush()
        return {
            "file_id": file.id,
            "status": file.index_status,
            "indexed": True,
            "chunk_count": len(spans),
            "partial": partial,
        }
    except Exception as exc:  # noqa: BLE001
        file.index_status = "error"
        file.index_error = str(exc)
        file.chunk_count = 0
        db.flush()
        return {"file_id": file.id, "status": "error", "indexed": False, "chunk_count": 0, "error": str(exc)}


def reindex_data_source(db: Session, source: DataSource, *, force: bool = True) -> dict[str, Any]:
    """重新索引资料库中的已解析文件，返回可直接展示的汇总。"""
    tenant_id = tenant_service.current_tenant_id(db)
    if source.tenant_id != tenant_id:
        raise PermissionError("只有资料库所属租户可以重建检索索引")
    if source.type != "file_bucket":
        raise ValueError("只有文件桶数据源可以建立检索索引")
    files = db.execute(
        select(BucketFile).where(BucketFile.data_source_id == source.id).order_by(BucketFile.created_at)
    ).scalars().all()
    items = [index_file(db, file, force=force) for file in files]
    db.commit()
    return {
        "data_source_id": source.id,
        "files_total": len(files),
        "files_indexed": sum(1 for item in items if item["status"] in {"indexed", "partial"}),
        "chunks_total": sum(int(item.get("chunk_count") or 0) for item in items),
        "items": items,
    }


def enqueue_document_index(
    db: Session,
    file: BucketFile,
    *,
    parse_document: bool,
    force: bool = False,
) -> tuple[DocumentIndexJob, bool]:
    """将一个文件的解析/索引持久化入队；相同文件只保留一个活跃任务。"""
    source = _require_owned_file(db, file)
    tenant_id = tenant_service.current_tenant_id(db)
    active_key = _document_active_key(file.id)
    active = db.execute(
        select(DocumentIndexJob)
        .where(
            DocumentIndexJob.tenant_id == tenant_id,
            DocumentIndexJob.active_key == active_key,
        )
        .order_by(DocumentIndexJob.created_at.desc())
        .limit(1)
    ).scalars().first()
    if active:
        active.parse_document = active.parse_document or parse_document
        active.force = active.force or force
        file.index_status = "queued"
        file.index_error = ""
        return active, False

    job = DocumentIndexJob(
        tenant_id=tenant_id,
        data_source_id=source.id,
        bucket_file_id=file.id,
        parse_document=parse_document,
        force=force,
        active_key=active_key,
        status="queued",
        max_attempts=DOCUMENT_JOB_MAX_ATTEMPTS,
        timeout_seconds=DOCUMENT_JOB_TIMEOUT_SECONDS,
        available_at=utc_now(),
    )
    file.index_status = "queued"
    file.index_error = ""
    try:
        # 活跃键有唯一约束。savepoint 让并发请求输掉竞争后仍可继续读取赢家，
        # 不会把上层 HTTP / worker 事务一并标记为 rollback-only。
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        active = db.execute(
            select(DocumentIndexJob)
            .where(
                DocumentIndexJob.tenant_id == tenant_id,
                DocumentIndexJob.active_key == active_key,
            )
            .order_by(DocumentIndexJob.created_at.desc())
            .limit(1)
        ).scalars().first()
        if active is None:
            raise
        active.parse_document = active.parse_document or parse_document
        active.force = active.force or force
        return active, False
    return job, True


def enqueue_data_source_reindex(db: Session, source: DataSource, *, force: bool = True) -> dict[str, Any]:
    """为已解析历史文件排队重建索引，不在 HTTP 请求中计算 embedding。"""
    tenant_id = tenant_service.current_tenant_id(db)
    if source.tenant_id != tenant_id:
        raise PermissionError("只有资料库所属租户可以重建检索索引")
    if source.type != "file_bucket":
        raise ValueError("只有文件桶数据源可以建立检索索引")
    files = db.execute(
        select(BucketFile)
        .where(BucketFile.data_source_id == source.id, BucketFile.status == "parsed")
        .order_by(BucketFile.created_at)
    ).scalars().all()
    jobs = [
        enqueue_document_index(db, file, parse_document=False, force=force)
        for file in files
    ]
    return {
        "data_source_id": source.id,
        "files_total": len(files),
        "files_indexed": 0,
        "chunks_total": 0,
        "jobs_queued": sum(1 for _job, created in jobs if created),
        "jobs_existing": sum(1 for _job, created in jobs if not created),
        "items": [
            {
                "file_id": file.id,
                "status": "queued",
                "indexed": False,
                "chunk_count": file.chunk_count,
                "job_id": job.id,
            }
            for file, (job, _created) in zip(files, jobs)
        ],
    }


def _retry_document_job(
    db: Session,
    job: DocumentIndexJob,
    file: BucketFile | None,
    *,
    status: str,
    error: str,
    now: datetime,
) -> None:
    """按有限指数退避重试文件处理；终态同时反映到文件索引状态。"""
    job.error = error
    if job.attempt < job.max_attempts:
        delay = min(DOCUMENT_JOB_RETRY_SECONDS * (2 ** max(0, job.attempt - 1)), 300)
        job.status = "retry_waiting"
        job.available_at = now + timedelta(seconds=delay)
        job.next_retry_at = job.available_at
        job.completed_at = None
        if file:
            file.index_status = "queued"
            file.index_error = ""
    else:
        job.status = status
        job.active_key = None
        job.completed_at = now
        job.next_retry_at = None
        if file:
            file.index_status = "error"
            file.index_error = error
    db.commit()


def expire_stale_document_index_jobs(db: Session, *, now: datetime | None = None) -> None:
    """应用重启或 worker 异常后，回收长期 running 的文档任务。"""
    now = now or utc_now()
    jobs = db.execute(
        select(DocumentIndexJob).where(DocumentIndexJob.status == "running")
    ).scalars().all()
    for job in jobs:
        started_at = _aware(job.started_at)
        if started_at and now > started_at + timedelta(seconds=job.timeout_seconds):
            original_tenant_id = db.info.get("tenant_id")
            db.info["tenant_id"] = job.tenant_id
            try:
                _retry_document_job(
                    db,
                    job,
                    db.get(BucketFile, job.bucket_file_id),
                    status="timed_out",
                    error="文档处理超过配置的超时限制",
                    now=now,
                )
            finally:
                if original_tenant_id is None:
                    db.info.pop("tenant_id", None)
                else:
                    db.info["tenant_id"] = original_tenant_id


def process_document_index_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 4,
) -> list[DocumentIndexJob]:
    """领取并处理少量文档任务；外部文件解析不持有数据库事务。"""
    now = now or utc_now()
    job_ids = db.execute(
        select(DocumentIndexJob.id)
        .where(
            DocumentIndexJob.status.in_(("queued", "retry_waiting")),
            DocumentIndexJob.available_at <= now,
        )
        .order_by(DocumentIndexJob.available_at.asc(), DocumentIndexJob.created_at.asc())
        .limit(max(1, min(limit, 16)))
    ).scalars().all()
    processed: list[DocumentIndexJob] = []
    for job_id in job_ids:
        claimed_at = utc_now()
        claimed = db.execute(
            update(DocumentIndexJob)
            .where(
                DocumentIndexJob.id == job_id,
                DocumentIndexJob.status.in_(("queued", "retry_waiting")),
                DocumentIndexJob.available_at <= now,
            )
            .values(
                status="running",
                attempt=DocumentIndexJob.attempt + 1,
                started_at=claimed_at,
                next_retry_at=None,
                error="",
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            db.rollback()
            continue
        db.commit()
        db.expire_all()
        job = db.get(DocumentIndexJob, job_id)
        if not job:
            continue
        original_tenant_id = db.info.get("tenant_id")
        db.info["tenant_id"] = job.tenant_id
        file: BucketFile | None = None
        try:
            file = db.get(BucketFile, job.bucket_file_id)
            if not file or file.data_source_id != job.data_source_id:
                raise RuntimeError("待处理文件不存在或不属于目标资料库")
            stored_path, filename = file.stored_path, file.filename
            # 完成读取后立即释放事务，再执行可能耗时的文件解析。
            db.commit()

            parsed: dict[str, Any] | None = None
            if job.parse_document:
                from . import doc_parser

                parsed = doc_parser.parse_file(stored_path, filename)

            file = db.get(BucketFile, job.bucket_file_id)
            if not file:
                raise RuntimeError("待处理文件已删除")
            if parsed is not None:
                file.status = "parsed" if parsed.get("status") == "success" else "error"
                file.parsed_text = parsed.get("text", "") or ""
                file.error = "" if file.status == "parsed" else str(parsed.get("message") or "文档解析失败")
            if file.status != "parsed" or not (file.parsed_text or "").strip():
                raise RuntimeError(file.error or "文件未能解析为可检索文本")

            result = index_file(db, file, force=job.force)
            if result.get("status") not in {"indexed", "partial"}:
                raise RuntimeError(str(result.get("error") or "建立检索索引失败"))
            finished_at = utc_now()
            started_at = _aware(job.started_at) or claimed_at
            if finished_at > started_at + timedelta(seconds=job.timeout_seconds):
                _retry_document_job(
                    db,
                    job,
                    file,
                    status="timed_out",
                    error="文档处理超过配置的超时限制",
                    now=finished_at,
                )
            else:
                job.status = "succeeded"
                job.active_key = None
                job.error = ""
                job.completed_at = finished_at
                job.next_retry_at = None
                db.commit()
        except Exception as exc:  # noqa: BLE001
            _retry_document_job(
                db,
                job,
                file,
                status="failed",
                error=str(exc),
                now=utc_now(),
            )
        finally:
            if original_tenant_id is None:
                db.info.pop("tenant_id", None)
            else:
                db.info["tenant_id"] = original_tenant_id
        db.expire_all()
        current = db.get(DocumentIndexJob, job_id)
        if current:
            processed.append(current)
    return processed


def _visible_files(db: Session, data_source_ids: list[str]) -> list[tuple[BucketFile, DataSource]]:
    """先按当前租户可见性过滤，再返回文件及其资料库归属。

    检索服务没有“无上下文即管理员”的模式；后台任务应在创建 Session 时
    显式写入其发起租户，避免新调用路径意外绕过隔离。
    """
    tenant_service.current_tenant_id(db)
    if not data_source_ids:
        return []
    stmt = (
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.data_source_id.in_(data_source_ids),
            DataSource.type == "file_bucket",
            BucketFile.status == "parsed",
            BucketFile.parsed_text != "",
        )
    )
    # 重要：先按资源可见性过滤文件，再读取任何分块或计算排序。
    stmt = stmt.where(tenant_service.visible_clause(DataSource, db))
    return list(db.execute(stmt).all())


def search(
    db: Session,
    data_source_ids: list[str],
    query: str,
    top_k: int = 5,
    max_chars: int = 4_000,
) -> list[dict[str, Any]]:
    """混合向量/关键词检索，返回带稳定引用定位的资料片段。

    ``data_source_ids`` 即使由客户端传入也不会越过租户可见性 SQL 条件；
    结果只含已经通过同一资料库权限检查的文档分块。
    """
    query = (query or "").strip()
    if not data_source_ids or not query:
        return []
    tenant_service.current_tenant_id(db)
    file_sources = _visible_files(db, data_source_ids)
    if not file_sources:
        return []

    # 搜索严格只读：上传、重解析和显式重建索引都会入队，由 worker 更新状态。
    # 非所有者也只能读取内容哈希和版本都一致的公开索引。
    allowed_file_ids = [
        file.id for file, _source in file_sources if _index_is_current(file)
    ]
    if not allowed_file_ids:
        return []
    stmt = (
        select(DocumentChunk, BucketFile, DataSource)
        .join(BucketFile, BucketFile.id == DocumentChunk.bucket_file_id)
        .join(DataSource, DataSource.id == DocumentChunk.data_source_id)
        .where(
            DocumentChunk.bucket_file_id.in_(allowed_file_ids),
            DataSource.type == "file_bucket",
        )
    )
    stmt = stmt.where(tenant_service.visible_clause(DataSource, db))

    query_tokens = set(_tokens(query))
    scored: list[tuple[float, float, float, DocumentChunk, BucketFile, DataSource]] = []
    query_embeddings: dict[str, list[float] | None] = {}
    for chunk, file, source in db.execute(stmt).all():
        marker = str(chunk.embedding_model or EMBEDDING_MODEL)
        if marker not in query_embeddings:
            try:
                query_embeddings[marker] = _query_embedding_for_marker(db, marker, query)
            except Exception:  # noqa: BLE001
                # 已有索引仍可用关键词检索；把 provider 失败降级为 0 向量分，
                # 不能因此跨租户回退到其他模型或悄悄重建索引。
                query_embeddings[marker] = None
        vector_score = _cosine(query_embeddings[marker], chunk.embedding)
        keyword_score = _keyword_score(query_tokens, chunk.text)
        # 局部哈希碰撞造成的极低相似会被阈值排除，精确关键词仍占混合分的 24%。
        score = vector_score * 0.76 + keyword_score * 0.24
        if score <= 0.015:
            continue
        scored.append((score, vector_score, keyword_score, chunk, file, source))

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    results: list[dict[str, Any]] = []
    total_chars = 0
    for rank, (score, vector_score, keyword_score, chunk, file, source) in enumerate(scored[: max(1, min(top_k, 20))], 1):
        excerpt = chunk.text
        remaining = max_chars - total_chars
        if remaining <= 0:
            break
        if len(excerpt) > remaining:
            excerpt = excerpt[:remaining].rstrip() + "…"
        total_chars += len(excerpt)
        results.append(
            {
                "citation_id": f"C{rank}",
                "chunk_id": chunk.id,
                "file_id": file.id,
                "filename": file.filename,
                "data_source_id": source.id,
                "data_source_name": source.name,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "chunk_ordinal": chunk.ordinal,
                "content_hash": chunk.content_hash,
                "file_content_hash": file.indexed_content_hash,
                "embedding_model": chunk.embedding_model,
                "index_version": file.index_version,
                "score": round(score, 4),
                "vector_score": round(vector_score, 4),
                "keyword_score": round(keyword_score, 4),
                "text": excerpt,
            }
        )
    return results


def build_context(results: list[dict[str, Any]]) -> str:
    """把资料片段编入 Agent 上下文，并强制使用稳定引用编号。"""
    if not results:
        return ""
    parts = ["以下为已授权资料库的检索依据。回答涉及事实时必须标注对应【C#】；缺少依据时明确说明不确定："]
    for result in results:
        parts.append(
            f"\n【{result['citation_id']}】{result['filename']}"
            f"（{result['data_source_name']}，字符 {result['char_start']}-{result['char_end']}）\n"
            f"{result['text']}"
        )
    return "\n".join(parts)
