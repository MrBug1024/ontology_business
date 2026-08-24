"""Persistent single-flight ledger for compound assistant compilations.

The ledger intentionally stores hashes and version identifiers instead of raw
business documents.  A unique request fingerprint gives one worker ownership;
duplicates observe the existing terminal/running state and never launch a
second provider call chain.  P0 recovery does not add a separate subscriber
table: every duplicate request stores the shared job id in its own durable
assistant placeholder.  Thread status APIs can therefore rediscover the job,
while the result endpoint reads the server-owned proposal and confirmation
continues to resolve the exact proposal from a persisted assistant message.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
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


class CompilationBaselineChanged(RuntimeError):
    """The scenario changed while a proposal-only compilation was running."""


@dataclass(frozen=True)
class PublicCompilationError:
    code: str
    message: str


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
            # Kept only in memory to build the exact compiler input; it is
            # excluded from every persisted job record below.
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
    mapping_context_fingerprint: str = "",
    execution_policy: dict[str, Any] | None = None,
) -> CompilationIdentity:
    normalized_message = normalize_message(message)
    message_hash = _sha256(normalized_message)
    attachments_hash = attachment_content_fingerprint(attachments)
    llm_hash = llm_config_fingerprint(llm)
    policy_hash = _canonical_hash(execution_policy or {})
    fingerprint = _canonical_hash({
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "scenario_id": str(scenario_id),
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
) -> tuple[AssistantCompilationJob, bool]:
    """Atomically create the owner row or return the existing exact request."""
    budget = int(llm_call_budget)
    if budget < 1:
        raise ValueError("完整业务模型的 LLM 总调用预算必须至少为 1")
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
        status="running",
        progress={"phase": "claimed", "detail": "已取得唯一编译执行权"},
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


def record_provider_call(
    db: Session,
    job_id: str,
    *,
    used: int,
    budget: int,
    phase: str,
) -> None:
    job = db.get(AssistantCompilationJob, job_id)
    if job is None or job.status != "running":
        raise RuntimeError("编译任务已不再运行，拒绝继续调用模型")
    if used < job.llm_calls_used or used > budget or budget != job.llm_call_budget:
        raise RuntimeError("编译任务调用预算状态不一致")
    job.llm_calls_used = used
    job.progress = {
        "phase": phase,
        "detail": f"正在执行模型调用 {used}/{budget}",
        "calls_used": used,
        "call_budget": budget,
    }
    db.commit()


def mark_succeeded(
    db: Session,
    job_id: str,
    *,
    result: dict[str, Any],
    commit: bool = True,
) -> AssistantCompilationJob:
    job = db.get(AssistantCompilationJob, job_id)
    if job is None:
        raise RuntimeError("编译任务不存在")
    if job.status == "succeeded":
        return job
    if job.status != "running":
        raise RuntimeError("非运行中的编译任务不能标记成功")
    job.status = "succeeded"
    job.result = result
    job.error = ""
    job.progress = {
        "phase": "completed",
        "detail": "复合变更清单已持久化，可安全重放",
        "calls_used": job.llm_calls_used,
        "call_budget": job.llm_call_budget,
    }
    job.completed_at = _now()
    if commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job


def mark_failed(
    db: Session,
    job_id: str,
    *,
    error: BaseException | str,
    commit: bool = True,
) -> AssistantCompilationJob:
    job = db.get(AssistantCompilationJob, job_id)
    if job is None:
        raise RuntimeError("编译任务不存在")
    if job.status in {"succeeded", "failed"}:
        return job
    public_error = public_compilation_error(error)
    job.status = "failed"
    job.error = str(error or "编译失败")[:8_000]
    job.result = {}
    job.progress = {
        "phase": "failed",
        "detail": public_error.message,
        "error_code": public_error.code,
        "calls_used": job.llm_calls_used,
        "call_budget": job.llm_call_budget,
    }
    job.completed_at = _now()
    if commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job
