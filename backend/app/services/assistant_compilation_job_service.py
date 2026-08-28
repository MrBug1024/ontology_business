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
# A staged modelling plan needs the original user-provided source for the next
# explicitly started task.  Keep that private restart input only briefly; it
# is never part of any public job/result projection and is erased as soon as
# the plan reaches a terminal user decision.
CONTINUATION_INPUT_RETENTION = timedelta(hours=24)
MAX_COMPILATION_GUIDANCE_ITEMS = 20
MAX_COMPILATION_GUIDANCE_CHARS = 200_000


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
            "results": [],
            "activities": [
                {
                    "id": "tool:source-context:1",
                    "kind": "tool",
                    "step_id": "analyze",
                    "title": "读取业务资料",
                    "detail": "已冻结本轮消息、附件和场景上下文，后续恢复不会重复上传或串用其他会话资料。",
                    "status": "done",
                    "created_at": _now().isoformat(),
                },
                {
                    "id": "tool:scenario-model:2",
                    "kind": "tool",
                    "step_id": "scenario-model",
                    "title": "启动场景建模能力",
                    "detail": "正在解析来源并生成可审核的场景模型草稿。",
                    "status": "running",
                    "created_at": _now().isoformat(),
                },
            ],
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


def load_owner_continuation_input(
    db: Session,
    job_id: str,
    *,
    tenant_id: str,
    created_by_user_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read a still-valid private source bundle for a staged continuation."""
    job = db.execute(
        select(AssistantCompilationJob).where(
            AssistantCompilationJob.id == job_id,
            *_owner_predicates(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
            ),
        )
    ).scalars().first()
    if job is None or not isinstance(job.execution_input, dict) or not job.execution_input:
        raise LookupError("建模资料已不可恢复")
    completed_at = job.completed_at
    if completed_at is not None:
        completed_at = (
            completed_at
            if completed_at.tzinfo is not None
            else completed_at.replace(tzinfo=timezone.utc)
        )
        if completed_at + CONTINUATION_INPUT_RETENTION <= (now or _now()):
            raise LookupError("建模资料续作窗口已过期")
    return copy.deepcopy(job.execution_input)


def discard_owner_execution_input(
    db: Session,
    job_id: str,
    *,
    tenant_id: str,
    created_by_user_id: str,
) -> bool:
    """Erase one owner-bound source bundle once its staged plan is complete."""
    job = db.execute(
        select(AssistantCompilationJob)
        .where(
            AssistantCompilationJob.id == job_id,
            *_owner_predicates(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
            ),
        )
        .with_for_update()
    ).scalars().first()
    if job is None or not job.execution_input:
        return False
    job.execution_input = {}
    db.flush()
    return True


def purge_expired_completed_execution_inputs(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Bound retention of private source bundles left by abandoned stage plans."""
    cutoff = (now or _now()) - CONTINUATION_INPUT_RETENTION
    bounded_limit = max(1, min(int(limit), 1_000))
    candidates = list(
        db.execute(
            select(AssistantCompilationJob)
            .where(
                AssistantCompilationJob.status == "succeeded",
                AssistantCompilationJob.completed_at.is_not(None),
                AssistantCompilationJob.completed_at <= cutoff,
            )
            .order_by(AssistantCompilationJob.completed_at)
            .limit(bounded_limit)
        ).scalars().all()
    )
    expired = [job for job in candidates if isinstance(job.execution_input, dict) and job.execution_input]
    for job in expired:
        job.execution_input = {}
    if expired:
        db.flush()
    return len(expired)


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
            "title": "建设本体模型",
            "detail": "识别对象、属性、关系和约束。",
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


def normalize_progress_activities(value: Any) -> list[dict[str, Any]]:
    """Normalize the compact, user-visible execution timeline.

    Activities are deliberately different from model reasoning: they describe
    an auditable stage or tool boundary and never contain provider output.
    Keeping only the latest bounded window makes the timeline safe to render
    and cheap to replay from assistant history.
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[-40:]:
        if not isinstance(raw, dict):
            continue
        activity_id = str(raw.get("id") or "").strip()[:100]
        kind = str(raw.get("kind") or "stage").strip().lower()
        step_id = str(raw.get("step_id") or "").strip()[:80]
        title = str(raw.get("title") or "").strip()[:160]
        detail = str(raw.get("detail") or "").strip()[:500]
        status = str(raw.get("status") or "running").strip().lower()
        if not activity_id or not title or not detail:
            continue
        if kind not in {"stage", "model", "tool"}:
            kind = "stage"
        if status not in {"pending", "running", "done", "error"}:
            status = "running"
        item: dict[str, Any] = {
            "id": activity_id,
            "kind": kind,
            "step_id": step_id,
            "title": title,
            "detail": detail,
            "status": status,
        }
        created_at = str(raw.get("created_at") or "").strip()
        if created_at:
            item["created_at"] = created_at[:40]
        result.append(item)
    return result


def normalize_progress_guidance(value: Any) -> list[dict[str, Any]]:
    """Return the bounded public summary of guidance queued during a run."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value[-MAX_COMPILATION_GUIDANCE_ITEMS:]:
        if not isinstance(raw, dict):
            continue
        guidance_id = str(raw.get("id") or "").strip()[:128]
        if not guidance_id or guidance_id in seen:
            continue
        seen.add(guidance_id)
        status = str(raw.get("status") or "queued").strip().lower()
        if status not in {"queued", "applied", "deferred"}:
            status = "queued"
        raw_attachment_names = raw.get("attachment_names")
        attachment_names = (
            raw_attachment_names if isinstance(raw_attachment_names, list) else []
        )
        result.append({
            "id": guidance_id,
            "summary": str(raw.get("summary") or "用户补充了新的建模要求").strip()[:240],
            "attachment_names": [
                str(value).strip()[:240]
                for value in attachment_names[:20]
                if str(value).strip()
            ],
            "status": status,
            "created_at": str(raw.get("created_at") or "").strip()[:40],
        })
    return result


def enqueue_guidance(
    db: Session,
    job_id: str,
    *,
    tenant_id: str,
    created_by_user_id: str,
    guidance_id: str,
    message: str,
    attachment_text: str = "",
    sources: list[dict[str, Any]] | None = None,
    as_of: datetime | None = None,
) -> tuple[AssistantCompilationJob, bool]:
    """Atomically queue owner guidance without creating a second compilation."""
    now = as_of or _now()
    normalized_id = str(guidance_id or "").strip()[:128]
    normalized_message = str(message or "").strip()
    if not normalized_id or not normalized_message:
        raise ValueError("运行中指导缺少请求标识或内容")
    job = db.execute(
        select(AssistantCompilationJob)
        .where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.status == "running",
            *_owner_predicates(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
            ),
        )
        .with_for_update()
    ).scalars().first()
    if job is None:
        raise LookupError("编译任务已结束或不属于当前用户")

    execution_input = copy.deepcopy(
        job.execution_input if isinstance(job.execution_input, dict) else {}
    )
    queue = [
        copy.deepcopy(item)
        for item in (execution_input.get("guidance_queue") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if any(str(item.get("id") or "") == normalized_id for item in queue):
        return job, False
    progress = copy.deepcopy(job.progress if isinstance(job.progress, dict) else {})
    if progress.get("accepting_guidance") is False:
        raise LookupError("当前编译检查点正在完成，请在本轮结果出现后继续发送")
    public_guidance = normalize_progress_guidance(progress.get("guidance"))
    if any(item["id"] == normalized_id for item in public_guidance):
        return job, False
    if len(queue) >= MAX_COMPILATION_GUIDANCE_ITEMS:
        raise ValueError("当前任务等待处理的补充指导过多，请等待现有指导被采纳")
    queued_chars = sum(
        len(str(item.get("message") or ""))
        + len(str(item.get("attachment_text") or ""))
        for item in queue
    )
    incoming_chars = len(normalized_message) + len(str(attachment_text or ""))
    if queued_chars + incoming_chars > MAX_COMPILATION_GUIDANCE_CHARS:
        raise ValueError(
            "当前任务等待处理的补充内容过多，请等待现有指导被采纳后再继续"
        )

    private_sources = [
        copy.deepcopy(item) for item in (sources or [])[:20] if isinstance(item, dict)
    ]
    queue.append({
        "id": normalized_id,
        "message": normalized_message,
        "attachment_text": str(attachment_text or ""),
        "sources": private_sources,
        "created_at": now.isoformat(),
    })
    public_guidance.append({
        "id": normalized_id,
        "summary": normalized_message[:240],
        "attachment_names": [
            str(item.get("filename") or "").strip()[:240]
            for item in private_sources
            if str(item.get("filename") or "").strip()
        ],
        "status": "queued",
        "created_at": now.isoformat(),
    })
    execution_input["guidance_queue"] = queue
    job.execution_input = execution_input
    job.progress = {
        **progress,
        "guidance": public_guidance,
        "guidance_pending_count": len(queue),
        "detail": "已收到新的补充指导，将在当前安全检查点后继续处理。",
    }
    job.updated_at = now
    db.flush()
    return job, True


def claim_pending_guidance(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Consume queued guidance only through the current fenced worker lease."""
    now = as_of or _now()
    job = db.execute(
        select(AssistantCompilationJob)
        .where(*_lease_mutation_filters(
            job_id,
            token=str(token),
            attempt=int(attempt),
            as_of=now,
        ))
        .with_for_update()
    ).scalars().first()
    if job is None:
        raise CompilationLeaseLost("编译任务租约已失效，拒绝读取运行中指导")
    execution_input = copy.deepcopy(
        job.execution_input if isinstance(job.execution_input, dict) else {}
    )
    queue = [
        copy.deepcopy(item)
        for item in (execution_input.get("guidance_queue") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if not queue:
        return []

    claimed_ids = {str(item.get("id") or "") for item in queue}
    progress = copy.deepcopy(job.progress if isinstance(job.progress, dict) else {})
    guidance = normalize_progress_guidance(progress.get("guidance"))
    for item in guidance:
        if item["id"] in claimed_ids:
            item["status"] = "applied"
    activities = normalize_progress_activities(progress.get("activities"))
    activities.append({
        "id": f"tool:guidance:{len(activities) + 1}",
        "kind": "tool",
        "step_id": "plan",
        "title": "采纳补充指导",
        "detail": f"已采纳 {len(queue)} 条运行中补充要求，正在基于当前草稿继续建模。",
        "status": "done",
        "created_at": now.isoformat(),
    })
    execution_input.pop("guidance_queue", None)
    job.execution_input = execution_input
    job.progress = {
        **progress,
        "guidance": guidance,
        "guidance_pending_count": 0,
        "activities": activities,
        "detail": f"已采纳 {len(queue)} 条补充指导，正在更新页面中的场景草稿。",
    }
    job.updated_at = now
    db.commit()
    return queue


def defer_pending_guidance(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Drain guidance honestly when the current run cannot compile it further."""
    now = as_of or _now()
    job = db.execute(
        select(AssistantCompilationJob)
        .where(*_lease_mutation_filters(
            job_id,
            token=str(token),
            attempt=int(attempt),
            as_of=now,
        ))
        .with_for_update()
    ).scalars().first()
    if job is None:
        raise CompilationLeaseLost("编译任务租约已失效，拒绝延后运行中指导")
    execution_input = copy.deepcopy(
        job.execution_input if isinstance(job.execution_input, dict) else {}
    )
    queue = [
        copy.deepcopy(item)
        for item in (execution_input.get("guidance_queue") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if not queue:
        return []

    deferred_ids = {str(item.get("id") or "") for item in queue}
    progress = copy.deepcopy(job.progress if isinstance(job.progress, dict) else {})
    guidance = normalize_progress_guidance(progress.get("guidance"))
    for item in guidance:
        if item["id"] in deferred_ids:
            item["status"] = "deferred"
    activities = normalize_progress_activities(progress.get("activities"))
    activities.append({
        "id": f"tool:guidance-deferred:{len(activities) + 1}",
        "kind": "tool",
        "step_id": "review",
        "title": "保留补充指导",
        "detail": (
            f"本轮已无法继续调用模型，{len(queue)} 条补充要求已保留在会话中，"
            "未声称已应用。"
        ),
        "status": "done",
        "created_at": now.isoformat(),
    })
    execution_input.pop("guidance_queue", None)
    job.execution_input = execution_input
    job.progress = {
        **progress,
        "guidance": guidance,
        "guidance_pending_count": 0,
        "activities": activities,
        "detail": "补充指导已保留，但本轮未应用；可在本轮结果后继续发起修正。",
    }
    job.updated_at = now
    db.commit()
    return queue


def close_guidance_window(
    db: Session,
    job_id: str,
    *,
    token: str,
    attempt: int,
    as_of: datetime | None = None,
) -> bool:
    """Atomically stop accepting guidance only when the private queue is empty."""
    now = as_of or _now()
    job = db.execute(
        select(AssistantCompilationJob)
        .where(*_lease_mutation_filters(
            job_id,
            token=str(token),
            attempt=int(attempt),
            as_of=now,
        ))
        .with_for_update()
    ).scalars().first()
    if job is None:
        raise CompilationLeaseLost("编译任务租约已失效，拒绝关闭指导窗口")
    execution_input = (
        job.execution_input if isinstance(job.execution_input, dict) else {}
    )
    if any(
        isinstance(item, dict) and str(item.get("id") or "").strip()
        for item in (execution_input.get("guidance_queue") or [])
    ):
        return False
    progress = copy.deepcopy(job.progress if isinstance(job.progress, dict) else {})
    progress["accepting_guidance"] = False
    job.progress = progress
    job.updated_at = now
    db.commit()
    return True


def record_draft_checkpoint(
    db: Session,
    job_id: str,
    *,
    resource_count: int,
    resource_kinds: Iterable[str],
    detail: str,
    lease_token: str,
    lease_attempt: int,
    as_of: datetime | None = None,
) -> AssistantCompilationJob:
    """Publish one durable, canvas-visible draft revision for a running job."""
    job = _fresh_job(db, job_id)
    if job is None or job.status != "running":
        raise RuntimeError("编译任务已不再运行，拒绝发布草稿检查点")
    now = as_of or _now()
    raw = copy.deepcopy(job.progress if isinstance(job.progress, dict) else {})
    revision = max(0, int(raw.get("draft_checkpoint_revision") or 0)) + 1
    kinds = sorted({str(value).strip()[:40] for value in resource_kinds if str(value).strip()})
    activities = normalize_progress_activities(raw.get("activities"))
    activities.append({
        "id": f"tool:draft-checkpoint:{revision}",
        "kind": "tool",
        "step_id": str(raw.get("current_step") or raw.get("phase") or "plan")[:80],
        "title": "更新场景草稿",
        "detail": str(detail or f"页面已同步 {resource_count} 项待审核草稿。")[:500],
        "status": "done",
        "created_at": now.isoformat(),
    })
    progress = {
        **raw,
        "detail": str(detail or raw.get("detail") or "场景草稿已更新")[:500],
        "activities": activities,
        "draft_checkpoint_revision": revision,
        "draft_resource_count": max(0, int(resource_count)),
        "draft_resource_kinds": kinds,
    }
    return _persist_running_values(
        db,
        job,
        {"progress": progress},
        lease_token=lease_token,
        lease_attempt=lease_attempt,
        as_of=now,
        commit=True,
    )


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
    activities = normalize_progress_activities(raw.get("activities"))
    if status in {"done", "error"}:
        for activity in activities:
            if (
                activity["kind"] == "model"
                and activity.get("step_id") == step_id
                and activity["status"] == "running"
            ):
                activity["status"] = "done" if status == "done" else "error"
    activity_index = len(activities) + 1
    activities.append({
        "id": f"stage:{step_id[:64]}:{activity_index}",
        "kind": "stage",
        "step_id": step_id[:80],
        "title": title[:160],
        "detail": detail[:500],
        "status": status if status in {"pending", "running", "done", "error"} else "running",
        "created_at": (as_of or _now()).isoformat(),
    })
    progress = {
        **raw,
        "phase": step_id[:80],
        "detail": detail[:500],
        "steps": steps,
        "current_step": step_id[:80],
        "results": results,
        "activities": activities,
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
    previous = job.progress if isinstance(job.progress, dict) else {}
    activities = normalize_progress_activities(previous.get("activities"))
    for activity in activities:
        if activity["kind"] == "model" and activity["status"] == "running":
            activity["status"] = "done"
    activities.append({
        "id": f"model:{used}:{len(activities) + 1}",
        "kind": "model",
        "step_id": str(phase or "model")[:80],
        "title": "调用模型",
        "detail": f"正在通过模型处理「{str(phase or '当前任务')[:120]}」。",
        "status": "running",
        "created_at": (as_of or _now()).isoformat(),
    })
    progress = {
        **previous,
        "phase": phase,
        "detail": f"正在执行模型调用 {used}/{budget}",
        "steps": normalize_progress_steps(
            previous.get("steps")
        ),
        "current_step": previous.get("current_step") or "extract",
        "results": normalize_progress_results(
            previous.get("results")
        ),
        "activities": activities,
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
    retain_execution_input: bool = False,
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
        if retain_execution_input:
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
    activities = normalize_progress_activities(previous.get("activities"))
    for activity in activities:
        if activity["status"] == "running":
            activity["status"] = "done"
    activities.append({
        "id": f"stage:result:{len(activities) + 1}",
        "kind": "stage",
        "step_id": "result",
        "title": "汇总执行结果",
        "detail": "各阶段结果已汇总，已准备可审核的复合变更清单。",
        "status": "done",
        "created_at": (as_of or _now()).isoformat(),
    })
    progress = {
        "phase": "completed",
        "detail": "复合变更清单已持久化，可安全重放",
        "steps": steps,
        "current_step": "result",
        "results": results,
        "activities": activities,
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
            # A pending staged plan is the single exception: the next explicit
            # task must see the exact same source bundle.  This remains private
            # to the owner-bound job, expires through the maintenance path, and
            # is cleared immediately when the user completes the plan.
            "execution_input": (
                normalize_execution_input(job.execution_input)
                if retain_execution_input
                else {}
            ),
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
    activities = normalize_progress_activities(previous.get("activities"))
    for activity in activities:
        if activity["status"] == "running":
            activity["status"] = "error"
    activities.append({
        "id": f"stage:{current_step[:64]}:error:{len(activities) + 1}",
        "kind": "stage",
        "step_id": current_step[:80],
        "title": next((step["title"] for step in steps if step["id"] == current_step), "处理阶段"),
        "detail": public_error.message[:500],
        "status": "error",
        "created_at": (as_of or _now()).isoformat(),
    })
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
        "activities": activities,
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
