"""Persistent single-flight ledger for compound assistant compilations.

Hashes and version identifiers remain the replay identity.  Exact restart input
is stored only in the owner-bound job row and can be loaded by that owner or the
current fenced lease.  A unique request fingerprint gives one worker ownership;
duplicates observe the existing terminal/running state and never launch a
second provider call chain.  Thread status APIs rediscover the shared job while
public status/result projections omit both restart input and lease capability.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AssistantCompilationJob


ERROR_COMPILATION_FAILED = "compilation_failed"
ERROR_BASELINE_CHANGED = "scenario_baseline_changed"
ERROR_OUTPUT_TRUNCATED = "compiler_output_truncated"
ERROR_OUTPUT_INVALID = "compiler_output_invalid"
ERROR_BUDGET_EXHAUSTED = "compiler_call_budget_exhausted"
ERROR_CLIENT_DISCONNECTED = "client_disconnected"
ERROR_PROVIDER_UNAVAILABLE = "provider_temporarily_unavailable"
DEFAULT_LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 3_600
TERMINAL_JOB_STATUSES = {"succeeded", "failed"}


class CompilationBaselineChanged(RuntimeError):
    """The scenario changed while a proposal-only compilation was running."""


class CompilationLeaseLost(RuntimeError):
    """The worker no longer owns the fenced right to mutate a running job."""


@dataclass(frozen=True)
class PublicCompilationError:
    code: str
    message: str


@dataclass(frozen=True)
class CompilationLease:
    job_id: str
    token: str
    attempt: int
    expires_at: datetime


def public_compilation_error(error: BaseException | str) -> PublicCompilationError:
    """Map internal failures to stable, non-sensitive client messages.

    Provider responses, parser offsets and raw JSON fragments belong in the
    server-side job ledger only.  Recovery/status APIs and assistant streams
    must never echo them back to the browser.
    """
    name = error.__class__.__name__ if isinstance(error, BaseException) else ""
    detail = str(error or "")
    lowered = detail.casefold()
    if isinstance(error, CompilationBaselineChanged) or (
        "场景" in detail and "基线" in detail and "变化" in detail
    ):
        return PublicCompilationError(
            ERROR_BASELINE_CHANGED,
            "编译期间业务场景已发生变化。系统已保持零写入，请基于最新场景显式重试。",
        )
    if name == "CompilationCallBudgetExceeded" or "调用预算" in detail:
        return PublicCompilationError(
            ERROR_BUDGET_EXHAUSTED,
            "本次结构化编译已达到模型调用上限。系统已保持零写入，请显式重试。",
        )
    if name == "_CompilerOutputTruncated" or any(
        token in lowered
        for token in ("finish_reason=length", "output truncated", "输出截断", "末尾截断")
    ):
        return PublicCompilationError(
            ERROR_OUTPUT_TRUNCATED,
            "模型输出过长而被截断。系统已保持零写入，将在显式重试时继续采用拆分编译。",
        )
    if name == "JSONDecodeError" or any(
        token in lowered
        for token in (
            "jsondecodeerror",
            "invalid json",
            "parse json",
            "json parse",
            "解析 json",
            "合法 json",
            "输出无效",
            "编译失败",
        )
    ):
        return PublicCompilationError(
            ERROR_OUTPUT_INVALID,
            "模型返回的结构化结果不完整或无效。系统已保持零写入，将在显式重试时拆分处理。",
        )
    if "客户端" in detail and "断开" in detail:
        return PublicCompilationError(
            ERROR_CLIENT_DISCONNECTED,
            "客户端连接已中断，任务未完成且系统保持零写入。请显式重试。",
        )
    if name == "CompilerProviderUnavailable" or any(
        token in lowered
        for token in (
            "connection error",
            "temporarily unavailable",
            "service unavailable",
            "模型服务连接连续",
        )
    ):
        return PublicCompilationError(
            ERROR_PROVIDER_UNAVAILABLE,
            "模型服务暂时不可用。系统已在任务预算内重试并保持零写入，请稍后显式重试。",
        )
    return PublicCompilationError(
        ERROR_COMPILATION_FAILED,
        "完整业务模型编译未完成。系统已保持零写入，请显式重试；服务端已保留诊断记录。",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256(canonical)


def normalize_execution_input(value: dict[str, Any] | None) -> dict[str, Any]:
    """Detach one JSON-safe restart input without exposing it in progress data."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("编译任务 execution_input 必须是 JSON 对象")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def execution_policy_fingerprint(value: dict[str, Any]) -> str:
    """Hash the exact output-affecting policy persisted with a durable job."""
    if not isinstance(value, dict):
        raise ValueError("编译任务执行策略必须是 JSON 对象")
    return _canonical_hash(value)


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_message(message: str) -> str:
    """Normalize encoding/newlines while preserving paragraph semantics."""
    return unicodedata.normalize("NFC", str(message or "")).replace(
        "\r\n", "\n"
    ).replace("\r", "\n")


def _attachment_parts(attachments: Iterable[Any]) -> list[dict[str, str]]:
    parts: list[dict[str, str]] = []
    for attachment in attachments:
        parsed_text = str(_field(attachment, "parsed_text", "") or "")
        parsed_hash = _sha256(parsed_text)
        raw_hash = str(_field(attachment, "content_hash", "") or parsed_hash)
        parts.append({
            "content_hash": raw_hash,
            "parsed_text_hash": parsed_hash,
            "filename": unicodedata.normalize(
                "NFC", str(_field(attachment, "filename", "") or "")
            ),
            "mime": str(_field(attachment, "mime", "") or "").lower(),
            "status": str(_field(attachment, "status", "") or ""),
            "error_hash": _sha256(str(_field(attachment, "error", "") or "")),
            # Kept out of fingerprint descriptors. A caller may persist the
            # exact text separately in the owner-private execution_input.
            "_parsed_text": parsed_text,
            "_error": str(_field(attachment, "error", "") or ""),
        })
    parts.sort(key=lambda item: json.dumps(
        {key: value for key, value in item.items() if not key.startswith("_")},
        ensure_ascii=False,
        sort_keys=True,
    ))
    return parts


def attachment_content_fingerprint(attachments: Iterable[Any]) -> str:
    """Hash exact compiler attachment inputs without persisting their text.

    Fresh uploads provide a hash of the original bytes.  The parsed-text hash
    remains part of the identity because parser output is what the compiler
    actually consumes.  Legacy rows without a byte hash safely fall back to
    their parsed-text hash.
    """
    persisted_parts = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in _attachment_parts(attachments)
    ]
    return _canonical_hash(persisted_parts)


def canonical_compiler_documents(attachments: Iterable[Any]) -> list[dict[str, str]]:
    """Build stable content-addressed provenance inputs for the compiler.

    Assistant attachment row IDs and ORM query order are request-local.  Using
    them as evidence IDs would make content-level replay point at an older
    upload.  These IDs derive from the exact content descriptor instead and
    remain stable across threads; duplicate identical inputs receive a stable
    ordinal.
    """
    documents: list[dict[str, str]] = []
    ordinals: dict[str, int] = {}
    for part in _attachment_parts(attachments):
        persisted = {
            key: value for key, value in part.items() if not key.startswith("_")
        }
        descriptor_hash = _canonical_hash(persisted)
        ordinals[descriptor_hash] = ordinals.get(descriptor_hash, 0) + 1
        documents.append({
            "id": f"content-{descriptor_hash[:52]}-{ordinals[descriptor_hash]:02d}",
            "filename": part["filename"],
            "status": part["status"],
            "error": part["_error"],
            "text": part["_parsed_text"],
        })
    return documents


def llm_config_fingerprint(llm: Any) -> str:
    """Hash output-affecting deployment configuration, never the API key."""
    if llm is None:
        return _canonical_hash({"configured": False})
    return _canonical_hash({
        "id": str(_field(llm, "id", "") or ""),
        "connector_revision": int(_field(llm, "connector_revision", 0) or 0),
        "provider": str(_field(llm, "provider", "") or "").lower(),
        "base_url": str(_field(llm, "base_url", "") or "").rstrip("/"),
        "model": str(_field(llm, "model", "") or ""),
        "temperature": float(_field(llm, "temperature", 0.0) or 0.0),
        "max_tokens": int(_field(llm, "max_tokens", 0) or 0),
        "capabilities": sorted(
            str(item) for item in (_field(llm, "capabilities", []) or [])
        ),
        "enabled": bool(_field(llm, "enabled", False)),
    })


@dataclass(frozen=True)
class CompilationIdentity:
    request_fingerprint: str
    message_hash: str
    attachment_content_hash: str
    llm_config_fingerprint: str
    mapping_context_fingerprint: str
    execution_policy_fingerprint: str


def build_compilation_identity(
    *,
    tenant_id: str,
    user_id: str,
    scenario_id: str,
    message: str,
    attachments: Iterable[Any],
    llm: Any,
    compiler_version: str,
    scenario_baseline: str,
    request_id: str = "",
    mapping_context_fingerprint: str = "",
    execution_policy: dict[str, Any] | None = None,
) -> CompilationIdentity:
    normalized_message = normalize_message(message)
    message_hash = _sha256(normalized_message)
    attachments_hash = attachment_content_fingerprint(attachments)
    llm_hash = llm_config_fingerprint(llm)
    policy_hash = execution_policy_fingerprint(execution_policy or {})
    fingerprint = _canonical_hash({
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "scenario_id": str(scenario_id),
        "request_id": str(request_id or ""),
        "message_hash": message_hash,
        "attachment_content_hash": attachments_hash,
        "llm_config_fingerprint": llm_hash,
        "compiler_version": str(compiler_version),
        "scenario_baseline": str(scenario_baseline),
        "mapping_context_fingerprint": str(mapping_context_fingerprint),
        "execution_policy_fingerprint": policy_hash,
    })
    return CompilationIdentity(
        request_fingerprint=fingerprint,
        message_hash=message_hash,
        attachment_content_hash=attachments_hash,
        llm_config_fingerprint=llm_hash,
        mapping_context_fingerprint=str(mapping_context_fingerprint),
        execution_policy_fingerprint=policy_hash,
    )


def claim_compilation(
    db: Session,
    *,
    identity: CompilationIdentity,
    tenant_id: str,
    user_id: str,
    scenario_id: str,
    thread_id: str,
    message_id: str,
    compiler_version: str,
    scenario_baseline: str,
    llm_call_budget: int,
    plan: list[dict[str, Any]] | None = None,
    execution_input: dict[str, Any] | None = None,
) -> tuple[AssistantCompilationJob, bool]:
    """Atomically create the owner row or return the existing exact request."""
    budget = int(llm_call_budget)
    if budget < 1:
        raise ValueError("完整业务模型的 LLM 总调用预算必须至少为 1")
    normalized_plan = normalize_progress_steps(plan or [])
    job = AssistantCompilationJob(
        request_fingerprint=identity.request_fingerprint,
        tenant_id=tenant_id,
        created_by_user_id=user_id,
        scenario_id=scenario_id,
        thread_id=thread_id,
        message_id=message_id,
        message_hash=identity.message_hash,
        attachment_content_hash=identity.attachment_content_hash,
        llm_config_fingerprint=identity.llm_config_fingerprint,
        mapping_context_fingerprint=identity.mapping_context_fingerprint,
        execution_policy_fingerprint=identity.execution_policy_fingerprint,
        compiler_version=compiler_version,
        scenario_baseline=scenario_baseline,
        execution_input=normalize_execution_input(execution_input),
        status="running",
        progress={
            "phase": "queued",
            "detail": "已取得唯一编译执行权，任务正在排队。",
            "steps": normalized_plan,
            "current_step": next(
                (item["id"] for item in normalized_plan if item["status"] == "pending"),
                "source",
            ),
            "calls_used": 0,
            "call_budget": budget,
        },
        llm_call_budget=budget,
        llm_calls_used=0,
    )
    db.add(job)
    try:
        # Commit is deliberate: provider work must not begin until every other
        # worker can observe the unique ownership row.
        db.commit()
        db.refresh(job)
        return job, True
    except IntegrityError:
        db.rollback()
    existing = db.execute(
        select(AssistantCompilationJob).where(
            AssistantCompilationJob.request_fingerprint
            == identity.request_fingerprint,
            AssistantCompilationJob.tenant_id == tenant_id,
            AssistantCompilationJob.created_by_user_id == user_id,
            AssistantCompilationJob.scenario_id == scenario_id,
        )
    ).scalars().first()
    if existing is None:
        raise RuntimeError("编译任务唯一执行权冲突，但未找到已持久化任务")
    return existing, False


def _lease_duration(seconds: int) -> int:
    duration = int(seconds)
    if duration < 1 or duration > MAX_LEASE_SECONDS:
        raise ValueError(f"编译任务租约时长必须在 1 到 {MAX_LEASE_SECONDS} 秒之间")
    return duration


def _lease_available(as_of: datetime):
    return or_(
        AssistantCompilationJob.lease_token.is_(None),
        AssistantCompilationJob.lease_token == "",
        AssistantCompilationJob.lease_expires_at.is_(None),
        AssistantCompilationJob.lease_expires_at <= as_of,
    )


def _owner_predicates(*, tenant_id: str, created_by_user_id: str | None) -> list[Any]:
    owner_filter = (
        AssistantCompilationJob.created_by_user_id.is_(None)
        if created_by_user_id is None
        else AssistantCompilationJob.created_by_user_id == created_by_user_id
    )
    return [
        AssistantCompilationJob.tenant_id == tenant_id,
        owner_filter,
    ]


def acquire_compilation_lease(
    db: Session,
    job_id: str,
    *,
    tenant_id: str,
    created_by_user_id: str | None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    token: str | None = None,
    as_of: datetime | None = None,
) -> CompilationLease | None:
    """Atomically fence one available running job; this is a commit boundary."""
    now = as_of or _now()
    duration = _lease_duration(lease_seconds)
    lease_token = str(token or secrets.token_hex(32)).strip()
    if not lease_token or len(lease_token) > 64:
        raise ValueError("编译任务租约 token 必须为 1 到 64 个字符")
    expires_at = now + timedelta(seconds=duration)
    changed = db.execute(
        update(AssistantCompilationJob)
        .where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.status == "running",
            *_owner_predicates(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
            ),
            _lease_available(now),
        )
        .values(
            lease_token=lease_token,
            lease_expires_at=expires_at,
            lease_attempt=AssistantCompilationJob.lease_attempt + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if changed != 1:
        return None
    row = db.execute(
        select(
            AssistantCompilationJob.lease_attempt,
            AssistantCompilationJob.lease_expires_at,
        ).where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.lease_token == lease_token,
        )
    ).one()
    return CompilationLease(
        job_id=str(job_id),
        token=lease_token,
        attempt=int(row.lease_attempt),
        expires_at=row.lease_expires_at,
    )


def renew_compilation_lease(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    as_of: datetime | None = None,
) -> CompilationLease | None:
    """Extend only the current, unexpired fenced lease."""
    now = as_of or _now()
    expires_at = now + timedelta(seconds=_lease_duration(lease_seconds))
    changed = db.execute(
        update(AssistantCompilationJob)
        .where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.status == "running",
            AssistantCompilationJob.lease_token == str(token),
            AssistantCompilationJob.lease_attempt == int(attempt),
            AssistantCompilationJob.lease_expires_at.is_not(None),
            AssistantCompilationJob.lease_expires_at > now,
        )
        .values(lease_expires_at=expires_at, updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if changed != 1:
        return None
    return CompilationLease(
        job_id=str(job_id),
        token=str(token),
        attempt=int(attempt),
        expires_at=expires_at,
    )


def release_compilation_lease(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime | None = None,
) -> bool:
    """Release only the exact fenced lease; stale workers cannot release a takeover."""
    now = as_of or _now()
    changed = db.execute(
        update(AssistantCompilationJob)
        .where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.status == "running",
            AssistantCompilationJob.lease_token == str(token),
            AssistantCompilationJob.lease_attempt == int(attempt),
        )
        .values(lease_token="", lease_expires_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return changed == 1


def expired_running_job_ids(
    db: Session,
    *,
    as_of: datetime | None = None,
    limit: int = 100,
    tenant_id: str | None = None,
    created_by_user_id: str | None = None,
) -> list[str]:
    """List restart candidates without returning their private execution input."""
    bounded_limit = max(1, min(int(limit), 1_000))
    filters: list[Any] = [
        AssistantCompilationJob.status == "running",
        _lease_available(as_of or _now()),
    ]
    if tenant_id is not None:
        filters.append(AssistantCompilationJob.tenant_id == tenant_id)
    if created_by_user_id is not None:
        filters.append(
            AssistantCompilationJob.created_by_user_id == created_by_user_id
        )
    return [
        str(value)
        for value in db.scalars(
            select(AssistantCompilationJob.id)
            .where(*filters)
            .order_by(
                AssistantCompilationJob.started_at,
                AssistantCompilationJob.id,
            )
            .limit(bounded_limit)
        ).all()
    ]


def claim_expired_running_jobs(
    db: Session,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    as_of: datetime | None = None,
    limit: int = 10,
) -> list[CompilationLease]:
    """Claim up to ``limit`` restart candidates despite concurrent scanners."""
    now = as_of or _now()
    bounded_limit = max(1, min(int(limit), 100))
    candidates = list(db.execute(
        select(
            AssistantCompilationJob.id,
            AssistantCompilationJob.tenant_id,
            AssistantCompilationJob.created_by_user_id,
        )
        .where(
            AssistantCompilationJob.status == "running",
            _lease_available(now),
        )
        .order_by(
            AssistantCompilationJob.started_at,
            AssistantCompilationJob.id,
        )
        .limit(bounded_limit * 4)
    ).all())
    leases: list[CompilationLease] = []
    for candidate in candidates:
        lease = acquire_compilation_lease(
            db,
            str(candidate.id),
            tenant_id=str(candidate.tenant_id),
            created_by_user_id=(
                str(candidate.created_by_user_id)
                if candidate.created_by_user_id is not None
                else None
            ),
            lease_seconds=lease_seconds,
            as_of=now,
        )
        if lease is not None:
            leases.append(lease)
        if len(leases) >= bounded_limit:
            break
    return leases


def load_owner_execution_input(
    db: Session,
    job_id: str,
    *,
    tenant_id: str,
    created_by_user_id: str,
) -> dict[str, Any]:
    """Load restart input through the same tenant-and-owner boundary as the job."""
    value = db.scalar(
        select(AssistantCompilationJob.execution_input).where(
            AssistantCompilationJob.id == job_id,
            *_owner_predicates(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
            ),
        )
    )
    if value is None:
        raise LookupError("编译任务不存在或不属于当前用户")
    return copy.deepcopy(value if isinstance(value, dict) else {})


def load_leased_execution_input(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Load private restart input only for the current unexpired lease capability."""
    value = db.scalar(
        select(AssistantCompilationJob.execution_input).where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.status == "running",
            AssistantCompilationJob.lease_token == str(token),
            AssistantCompilationJob.lease_attempt == int(attempt),
            AssistantCompilationJob.lease_expires_at.is_not(None),
            AssistantCompilationJob.lease_expires_at > (as_of or _now()),
        )
    )
    if value is None:
        raise CompilationLeaseLost("编译任务租约已失效，拒绝读取私有执行输入")
    return copy.deepcopy(value if isinstance(value, dict) else {})


def normalize_progress_steps(value: Any) -> list[dict[str, Any]]:
    """Keep the persisted execution plan small, stable and safe to expose."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_statuses = {"pending", "running", "done", "error"}
    for raw in value[:20]:
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or "").strip()[:80]
        title = str(raw.get("title") or "").strip()[:160]
        if not step_id or not title or step_id in seen:
            continue
        seen.add(step_id)
        status = str(raw.get("status") or "pending").strip().lower()
        result.append({
            "id": step_id,
            "title": title,
            "detail": str(raw.get("detail") or "待开始").strip()[:500],
            "status": status if status in allowed_statuses else "pending",
        })
    return result


def compilation_plan(
    *,
    document_count: int,
    source_count: int,
    total_characters: int,
) -> list[dict[str, Any]]:
    """Return the user-facing work plan for one compound modelling request."""
    source_label = (
        f"{document_count} 个附件、{source_count} 个来源段落"
        if document_count
        else f"{source_count} 个来源段落"
    )
    return [
        {
            "id": "analyze",
            "title": "分析业务资料",
            "detail": f"读取 {source_label}（约 {total_characters:,} 字符），建立可追溯来源。",
            "status": "pending",
        },
        {
            "id": "plan",
            "title": "制定建模任务",
            "detail": "拆解为本体、实例、映射、业务能力、规则事件和工作流任务。",
            "status": "pending",
        },
        {
            "id": "ontology",
            "title": "建立本体与实例草稿",
            "detail": "识别对象、属性、关系和对象实例。",
            "status": "pending",
        },
        {
            "id": "mapping",
            "title": "整理数据映射",
            "detail": "对照当前场景中已检查的数据源表和字段。",
            "status": "pending",
        },
        {
            "id": "rules",
            "title": "校验规则、事件与工作流",
            "detail": "检查引用、约束、触发条件、节点和分支。",
            "status": "pending",
        },
        {
            "id": "review",
            "title": "生成待审核变更清单",
            "detail": "汇总来源覆盖、冲突和可安全应用的变更。",
            "status": "pending",
        },
        {
            "id": "result",
            "title": "汇总执行结果",
            "detail": "将阶段性成果整理为可核对、可回溯的最终草稿。",
            "status": "pending",
        },
    ]


def normalize_progress_results(value: Any) -> list[dict[str, Any]]:
    """Normalize short stage results; never persist raw model output here."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[-12:]:
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("step_id") or "").strip()[:80]
        summary = str(raw.get("summary") or "").strip()[:500]
        if not step_id or not summary:
            continue
        result.append({"step_id": step_id, "summary": summary})
    return result


def _fresh_job(db: Session, job_id: str) -> AssistantCompilationJob | None:
    return db.scalars(
        select(AssistantCompilationJob)
        .where(AssistantCompilationJob.id == job_id)
        .execution_options(populate_existing=True)
    ).first()


def _lease_mutation_filters(
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime,
) -> list[Any]:
    return [
        AssistantCompilationJob.id == job_id,
        AssistantCompilationJob.status == "running",
        AssistantCompilationJob.lease_token == token,
        AssistantCompilationJob.lease_attempt == attempt,
        AssistantCompilationJob.lease_expires_at.is_not(None),
        AssistantCompilationJob.lease_expires_at > as_of,
    ]


def _uses_validated_lease(
    job: AssistantCompilationJob,
    *,
    token: str | None,
    attempt: int | None,
) -> bool:
    persisted_token = str(job.lease_token or "")
    supplied_token = str(token or "")
    if not persisted_token and not supplied_token:
        return False
    if (
        not persisted_token
        or not supplied_token
        or supplied_token != persisted_token
        or attempt is None
        or int(attempt) != int(job.lease_attempt or 0)
    ):
        raise CompilationLeaseLost("编译任务租约已失效，拒绝旧执行者写入")
    return True


def _persist_running_values(
    db: Session,
    job: AssistantCompilationJob,
    values: dict[str, Any],
    *,
    lease_token: str | None,
    lease_attempt: int | None,
    as_of: datetime,
    commit: bool,
) -> AssistantCompilationJob:
    if _uses_validated_lease(
        job,
        token=lease_token,
        attempt=lease_attempt,
    ):
        changed = db.execute(
            update(AssistantCompilationJob)
            .where(*_lease_mutation_filters(
                job.id,
                token=str(lease_token),
                attempt=int(lease_attempt),
                as_of=as_of,
            ))
            .values(**values, updated_at=as_of)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            if commit:
                db.rollback()
            raise CompilationLeaseLost("编译任务租约已过期或已被其他执行者接管")
    else:
        for field, value in values.items():
            setattr(job, field, value)
    if commit:
        db.commit()
    else:
        db.flush()
    refreshed = _fresh_job(db, job.id)
    if refreshed is None:
        raise RuntimeError("编译任务不存在")
    return refreshed


def record_progress(
    db: Session,
    job_id: str,
    *,
    step_id: str,
    title: str,
    detail: str,
    status: str = "running",
    calls_used: int | None = None,
    call_budget: int | None = None,
    result: str = "",
    lease_token: str | None = None,
    lease_attempt: int | None = None,
    as_of: datetime | None = None,
) -> None:
    """Persist one user-visible execution stage without storing model internals."""
    job = _fresh_job(db, job_id)
    if job is None or job.status != "running":
        raise RuntimeError("编译任务已不再运行，拒绝更新进度")
    raw = job.progress if isinstance(job.progress, dict) else {}
    steps = normalize_progress_steps(raw.get("steps"))
    if not steps:
        steps = [{
            "id": step_id,
            "title": title,
            "detail": "待开始",
            "status": "pending",
        }]
    existing = next((item for item in steps if item["id"] == step_id), None)
    if existing is None:
        existing = {"id": step_id, "title": title, "detail": "待开始", "status": "pending"}
        steps.append(existing)
    existing.update({
        "title": title[:160],
        "detail": detail[:500],
        "status": status if status in {"pending", "running", "done", "error"} else "running",
    })
    results = normalize_progress_results(raw.get("results"))
    if result:
        results = [item for item in results if item["step_id"] != step_id]
        results.append({"step_id": step_id[:80], "summary": result[:500]})
    progress = {
        **raw,
        "phase": step_id[:80],
        "detail": detail[:500],
        "steps": steps,
        "current_step": step_id[:80],
        "results": results,
        "calls_used": job.llm_calls_used if calls_used is None else int(calls_used),
        "call_budget": job.llm_call_budget if call_budget is None else int(call_budget),
    }
    _persist_running_values(
        db,
        job,
        {"progress": progress},
        lease_token=lease_token,
        lease_attempt=lease_attempt,
        as_of=as_of or _now(),
        commit=True,
    )


def record_provider_call(
    db: Session,
    job_id: str,
    *,
    used: int,
    budget: int,
    phase: str,
    lease_token: str | None = None,
    lease_attempt: int | None = None,
    as_of: datetime | None = None,
) -> None:
    job = _fresh_job(db, job_id)
    if job is None or job.status != "running":
        raise RuntimeError("编译任务已不再运行，拒绝继续调用模型")
    if used < job.llm_calls_used or used > budget or budget != job.llm_call_budget:
        raise RuntimeError("编译任务调用预算状态不一致")
    progress = {
        "phase": phase,
        "detail": f"正在执行模型调用 {used}/{budget}",
        "steps": normalize_progress_steps(
            (job.progress if isinstance(job.progress, dict) else {}).get("steps")
        ),
        "current_step": (
            (job.progress if isinstance(job.progress, dict) else {}).get("current_step")
            or "extract"
        ),
        "results": normalize_progress_results(
            (job.progress if isinstance(job.progress, dict) else {}).get("results")
        ),
        "calls_used": used,
        "call_budget": budget,
    }
    _persist_running_values(
        db,
        job,
        {"llm_calls_used": used, "progress": progress},
        lease_token=lease_token,
        lease_attempt=lease_attempt,
        as_of=as_of or _now(),
        commit=True,
    )


def mark_succeeded(
    db: Session,
    job_id: str,
    *,
    result: dict[str, Any],
    result_summary: str = "",
    commit: bool = True,
    lease_token: str | None = None,
    lease_attempt: int | None = None,
    as_of: datetime | None = None,
) -> AssistantCompilationJob:
    job = _fresh_job(db, job_id)
    if job is None:
        raise RuntimeError("编译任务不存在")
    if job.status == "succeeded":
        # The successful transition deliberately erases the lease capability.
        # A worker retrying the same terminal write can no longer prove that it
        # held that former token, so every token-bearing terminal retry is a
        # read-only replay.  This also prevents a stale token from changing the
        # outcome.  A trusted no-token maintenance call may still scrub legacy
        # rows created before terminal input minimization existed.
        if lease_token is not None or lease_attempt is not None:
            return job
        if (
            job.lease_token
            or job.lease_expires_at is not None
            or bool(job.execution_input)
        ):
            job.lease_token = ""
            job.lease_expires_at = None
            job.execution_input = {}
            if commit:
                db.commit()
                db.refresh(job)
            else:
                db.flush()
        return job
    if job.status != "running":
        raise RuntimeError("非运行中的编译任务不能标记成功")
    previous = job.progress if isinstance(job.progress, dict) else {}
    steps = normalize_progress_steps(previous.get("steps"))
    result_step = next((item for item in steps if item["id"] == "result"), None)
    if result_step is None:
        result_step = {
            "id": "result",
            "title": "汇总执行结果",
            "detail": "待开始",
            "status": "pending",
        }
        steps.append(result_step)
    result_step.update({
        "detail": "各阶段结果已汇总，已准备可审核的复合变更清单。",
        "status": "done",
    })
    results = normalize_progress_results(previous.get("results"))
    if result_summary:
        results = [item for item in results if item["step_id"] != "result"]
        results.append({"step_id": "result", "summary": result_summary[:500]})
    progress = {
        "phase": "completed",
        "detail": "复合变更清单已持久化，可安全重放",
        "steps": steps,
        "current_step": "result",
        "results": results,
        "calls_used": job.llm_calls_used,
        "call_budget": job.llm_call_budget,
    }
    now = as_of or _now()
    return _persist_running_values(
        db,
        job,
        {
            "status": "succeeded",
            "result": result,
            "error": "",
            "progress": progress,
            "completed_at": now,
            "lease_token": "",
            "lease_expires_at": None,
            # Exact message/attachment input is required only while a worker
            # can resume the job.  The result and content fingerprints are the
            # durable replay record; retaining the full input after success
            # unnecessarily extends the lifetime of private business data.
            "execution_input": {},
        },
        lease_token=lease_token,
        lease_attempt=lease_attempt,
        as_of=now,
        commit=commit,
    )


def mark_failed(
    db: Session,
    job_id: str,
    *,
    error: BaseException | str,
    commit: bool = True,
    lease_token: str | None = None,
    lease_attempt: int | None = None,
    as_of: datetime | None = None,
) -> AssistantCompilationJob:
    job = _fresh_job(db, job_id)
    if job is None:
        raise RuntimeError("编译任务不存在")
    if job.status in TERMINAL_JOB_STATUSES:
        if lease_token is not None or lease_attempt is not None:
            return job
        if (
            job.lease_token
            or job.lease_expires_at is not None
            or bool(job.execution_input)
        ):
            job.lease_token = ""
            job.lease_expires_at = None
            job.execution_input = {}
            if commit:
                db.commit()
                db.refresh(job)
            else:
                db.flush()
        return job
    public_error = public_compilation_error(error)
    previous = job.progress if isinstance(job.progress, dict) else {}
    steps = normalize_progress_steps(previous.get("steps"))
    current_step = str(previous.get("current_step") or "result")
    for step in steps:
        if step["id"] == current_step and step["status"] == "running":
            step["status"] = "error"
            step["detail"] = public_error.message[:500]
    progress = {
        "phase": "failed",
        "detail": public_error.message,
        "error_code": public_error.code,
        "steps": normalize_progress_steps(
            steps
        ),
        "current_step": (
            current_step or "result"
        ),
        "results": normalize_progress_results(
            previous.get("results")
        ),
        "calls_used": job.llm_calls_used,
        "call_budget": job.llm_call_budget,
    }
    now = as_of or _now()
    return _persist_running_values(
        db,
        job,
        {
            "status": "failed",
            "error": str(error or "编译失败")[:8_000],
            "result": {},
            "progress": progress,
            "completed_at": now,
            "lease_token": "",
            "lease_expires_at": None,
            "execution_input": {},
        },
        lease_token=lease_token,
        lease_attempt=lease_attempt,
        as_of=now,
        commit=commit,
    )
