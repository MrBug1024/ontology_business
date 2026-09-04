"""Agent MCP publication lifecycle, opaque credentials and invocation audit."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic, perf_counter, sleep
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..external_api_models import (
    AgentMCPConversation,
    AgentMCPInvocation,
    AgentMCPService,
)
from ..models import Agent, Conversation
from . import permission_service, tenant_service


_TOKEN_HASH_DOMAIN = b"ontology-platform/agent-mcp-token/v1\0"
_CONVERSATION_BINDING_HASH_DOMAIN = b"ontology-platform/agent-mcp-conversation-binding/v2\0"
_REQUEST_HASH_DOMAIN = b"ontology-platform/agent-mcp-request/v1\0"


class AgentMCPError(ValueError):
    """A safe publication or invocation error."""


class AgentMCPTurnBusyError(AgentMCPError):
    """A prior turn for this durable business conversation is still running."""


class AgentMCPTurnLeaseLostError(AgentMCPError):
    """A worker lost its fenced right to persist or execute a conversation turn."""


@dataclass(frozen=True)
class AuthenticatedAgentMCP:
    service_id: str
    tenant_id: str
    execution_user_id: str


@dataclass(frozen=True)
class MCPConversationBinding:
    """The durable transcript and mapping used by one business conversation."""

    mapping_id: str | None
    conversation_id: str | None
    binding_key_hash: str | None


@dataclass(frozen=True)
class AgentMCPTurnClaim:
    """One database-fenced turn claim or a stored idempotent replay."""

    lease: "AgentMCPTurnLease | None" = None
    invocation_id: str | None = None
    replay_result: dict[str, Any] | None = None


class AgentMCPTurnLease:
    """Renewable, generation-fenced ownership of one MCP conversation turn."""

    def __init__(
        self,
        *,
        mapping_id: str,
        invocation_id: str,
        token: str,
        generation: int,
        deadline_at: datetime,
        session_factory: Callable[[], Session],
    ) -> None:
        self.mapping_id = mapping_id
        self.invocation_id = invocation_id
        self.token = token
        self.generation = generation
        self.deadline_at = deadline_at
        self.session_factory = session_factory
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        lease_seconds = int(get_settings().agent_mcp_turn_lease_seconds)
        interval = max(1.0, min(float(lease_seconds) / 3.0, 15.0))
        self._thread = threading.Thread(
            target=self._renew_loop,
            args=(interval,),
            name=f"agent-mcp-turn-{self.mapping_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _renew_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                _renew_mcp_turn_lease(self)
            except AgentMCPTurnLeaseLostError:
                self._lost.set()
                return
            except Exception:
                # A renewal failure is fenced at the next guarded operation.
                # Failing closed is safer than extending a lease blindly after
                # a database/network split.
                self._lost.set()
                return

    def assert_active(self) -> None:
        if self._lost.is_set() or utc_now() >= self.deadline_at:
            self._lost.set()
            raise AgentMCPTurnLeaseLostError("MCP 会话轮次租约已失效")
        db = self.session_factory()
        try:
            mapping = db.get(AgentMCPConversation, self.mapping_id)
            if not _lease_matches(mapping, self, now=utc_now()):
                self._lost.set()
                raise AgentMCPTurnLeaseLostError("MCP 会话轮次租约已被回收")
        finally:
            db.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def token_hash(token: str) -> str:
    return hashlib.sha256(_TOKEN_HASH_DOMAIN + token.encode("utf-8")).hexdigest()


def conversation_binding_hash(binding_key: str) -> str:
    """Hash an opaque business binding key before persisting it."""
    return hashlib.sha256(
        _CONVERSATION_BINDING_HASH_DOMAIN + binding_key.encode("utf-8")
    ).hexdigest()


def external_request_hash(request_id: str) -> str:
    """Return the durable replay identity for one external JSON-RPC request."""
    return hashlib.sha256(
        _REQUEST_HASH_DOMAIN + request_id.encode("utf-8")
    ).hexdigest()


def _external_identifier(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentMCPError(f"{label} 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise AgentMCPError(f"{label} 不能为空")
    if cleaned != value:
        raise AgentMCPError(f"{label} 不能包含首尾空白")
    if len(cleaned) > 256:
        raise AgentMCPError(f"{label} 超过长度限制")
    return cleaned


def _conversation_binding_key(
    *,
    message: str,
    conversation_id: str | None,
    external_conversation_id: str | None,
    external_session_id: str | None,
    external_request_id: str | None,
) -> tuple[str | None, str]:
    """Separate business conversation identity from MCP transport identity."""
    external_conversation_id = _external_identifier(
        external_conversation_id,
        label="external_conversation_id",
    )
    if external_conversation_id:
        return f"external-conversation\0{external_conversation_id}", "external_conversation_id"
    if conversation_id:
        return f"platform-conversation\0{conversation_id}", "conversation_id"
    if external_session_id:
        # Without an explicit business handle, one tool request is one isolated
        # transcript. Reusing a transport across several UI chats can no longer
        # merge their history. The request id keeps an HTTP retry idempotent.
        request_identity = str(external_request_id or "").strip() or uuid.uuid4().hex
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        return (
            f"isolated-turn\0{external_session_id}\0{request_identity}\0{message_hash}",
            "isolated_turn",
        )
    return None, "direct"


def _normalized_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _lease_matches(
    mapping: AgentMCPConversation | None,
    lease: AgentMCPTurnLease,
    *,
    now: datetime,
) -> bool:
    if mapping is None:
        return False
    expires_at = _normalized_datetime(mapping.turn_lease_expires_at)
    deadline_at = _normalized_datetime(mapping.turn_lease_deadline_at)
    return bool(
        mapping.turn_lease_token == lease.token
        and int(mapping.turn_lease_generation or 0) == lease.generation
        and expires_at is not None
        and deadline_at is not None
        and expires_at > now
        and deadline_at >= now
    )


def _active_mcp_turn_lease(
    mapping: AgentMCPConversation,
    *,
    now: datetime,
) -> bool:
    """Whether a mapping is still owned by a live, bounded turn lease."""
    expires_at = _normalized_datetime(mapping.turn_lease_expires_at)
    deadline_at = _normalized_datetime(mapping.turn_lease_deadline_at)
    return bool(
        mapping.turn_lease_token
        and expires_at is not None
        and deadline_at is not None
        and expires_at > now
        and deadline_at >= now
    )


def _begin_turn_transaction(db: Session) -> None:
    """Start the short transaction used to claim or release one turn.

    PostgreSQL receives a row lock below.  SQLite has no equivalent
    ``SELECT .. FOR UPDATE`` behavior, so its short claim/release sections use
    ``BEGIN IMMEDIATE``.  The lock is released before model execution; it is
    only used to atomically change the durable fencing fields.
    """
    if db.in_transaction():
        db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _copy_result(value: Any) -> dict[str, Any]:
    """Return a detached JSON-safe stored MCP result for replay."""
    if not isinstance(value, dict):
        return {}
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _renew_mcp_turn_lease(lease: AgentMCPTurnLease) -> None:
    """Extend a fenced lease without ever moving its absolute deadline."""
    db = lease.session_factory()
    try:
        _begin_turn_transaction(db)
        mapping = db.execute(
            select(AgentMCPConversation)
            .where(AgentMCPConversation.id == lease.mapping_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        now = utc_now()
        if not _lease_matches(mapping, lease, now=now):
            db.rollback()
            raise AgentMCPTurnLeaseLostError("MCP 会话轮次租约已被回收")
        deadline_at = _normalized_datetime(mapping.turn_lease_deadline_at)
        if deadline_at is None or deadline_at <= now:
            db.rollback()
            raise AgentMCPTurnLeaseLostError("MCP 会话轮次已达到最大执行时长")
        renewed_until = now + timedelta(
            seconds=int(get_settings().agent_mcp_turn_lease_seconds)
        )
        mapping.turn_lease_expires_at = min(renewed_until, deadline_at)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim_mcp_turn_once(
    db: Session,
    *,
    service: AgentMCPService,
    binding: MCPConversationBinding,
    request_hash: str,
    input_hash: str,
    request_id: str,
) -> AgentMCPTurnClaim | None:
    """Claim one unowned mapping turn, or return its completed replay.

    ``None`` means a different request currently owns a non-expired lease.
    The caller performs bounded polling outside this short transaction.
    """
    if binding.mapping_id is None or binding.conversation_id is None:
        raise AgentMCPError("MCP 会话缺少持久化对话映射")
    _begin_turn_transaction(db)
    try:
        mapping = db.execute(
            select(AgentMCPConversation)
            .where(
                AgentMCPConversation.id == binding.mapping_id,
                AgentMCPConversation.service_id == service.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        if mapping is None:
            raise AgentMCPError("MCP 会话映射不存在或已失效")
        if (
            mapping.tenant_id != service.tenant_id
            or mapping.agent_id != service.agent_id
            or mapping.execution_user_id != service.execution_user_id
            or mapping.conversation_id != binding.conversation_id
        ):
            raise AgentMCPError("MCP 会话作用域无效")

        invocation = db.execute(
            select(AgentMCPInvocation)
            .where(
                AgentMCPInvocation.mcp_conversation_id == mapping.id,
                AgentMCPInvocation.external_request_hash == request_hash,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        if invocation is not None:
            if invocation.input_hash != input_hash:
                raise AgentMCPError("MCP 请求标识不能复用不同的消息内容")
            if invocation.status == "succeeded":
                mapping.last_used_at = utc_now()
                replay = _copy_result(invocation.result)
                replay["mcp_replayed"] = True
                db.commit()
                return AgentMCPTurnClaim(replay_result=replay)

        now = utc_now()
        if _active_mcp_turn_lease(mapping, now=now):
            # Release the short row/write lock without expiring the resolved
            # frozen Agent context held by this request session.  There are no
            # mutations in this branch, so a commit is equivalent to a
            # rollback for durable state and preserves the in-memory context.
            db.commit()
            return None

        # Any older worker has exceeded its renewable deadline/lease.  Fence
        # its audit row before issuing a newer generation.  A late worker can
        # no longer turn that abandoned row into a successful answer.
        old_token = mapping.turn_lease_token
        old_generation = int(mapping.turn_lease_generation or 0)
        if old_token:
            stale_invocations = db.execute(
                select(AgentMCPInvocation)
                .where(
                    AgentMCPInvocation.mcp_conversation_id == mapping.id,
                    AgentMCPInvocation.status == "running",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            ).scalars().all()
            for stale in stale_invocations:
                if invocation is not None and stale.id == invocation.id:
                    continue
                stale.status = "failed"
                stale.error_code = "AgentMCPTurnLeaseExpired"
                stale.error_message = "MCP 会话轮次租约过期，已由新的 worker 接管"
                stale.completed_at = now

        token = secrets.token_hex(32)
        generation = old_generation + 1
        deadline_at = now + timedelta(
            seconds=int(get_settings().agent_mcp_turn_max_seconds)
        )
        expires_at = min(
            now + timedelta(seconds=int(get_settings().agent_mcp_turn_lease_seconds)),
            deadline_at,
        )
        mapping.turn_lease_token = token
        mapping.turn_lease_generation = generation
        mapping.turn_lease_expires_at = expires_at
        mapping.turn_lease_deadline_at = deadline_at
        mapping.last_used_at = now

        if invocation is None:
            invocation = AgentMCPInvocation(
                service_id=service.id,
                tenant_id=service.tenant_id,
                agent_id=service.agent_id,
                execution_user_id=service.execution_user_id,
                request_id=request_id,
                mcp_conversation_id=mapping.id,
                external_request_hash=request_hash,
                turn_lease_token=token,
                turn_lease_generation=generation,
                conversation_id=binding.conversation_id,
                input_hash=input_hash,
                status="running",
            )
            db.add(invocation)
        else:
            # Retrying a failed/expired request keeps its audit identity and
            # therefore retains a single durable replay key.
            invocation.turn_lease_token = token
            invocation.turn_lease_generation = generation
            invocation.conversation_id = binding.conversation_id
            invocation.status = "running"
            invocation.latency_ms = 0
            invocation.tool_call_count = 0
            invocation.result = {}
            invocation.error_code = ""
            invocation.error_message = ""
            invocation.completed_at = None
        db.flush()
        db.commit()
        return AgentMCPTurnClaim(
            lease=AgentMCPTurnLease(
                mapping_id=mapping.id,
                invocation_id=invocation.id,
                token=token,
                generation=generation,
                deadline_at=deadline_at,
                session_factory=SessionLocal,
            ),
            invocation_id=invocation.id,
        )
    except Exception:
        db.rollback()
        raise


def _claim_mcp_turn(
    db: Session,
    *,
    service: AgentMCPService,
    binding: MCPConversationBinding,
    request_hash: str,
    input_hash: str,
    request_id: str,
) -> AgentMCPTurnClaim:
    """Wait only a bounded interval for an earlier turn in this session."""
    wait_seconds = float(get_settings().agent_mcp_turn_wait_seconds)
    wait_deadline = monotonic() + wait_seconds
    while True:
        try:
            claim = _claim_mcp_turn_once(
                db,
                service=service,
                binding=binding,
                request_hash=request_hash,
                input_hash=input_hash,
                request_id=request_id,
            )
        except IntegrityError:
            # A database that cannot provide row locks (notably SQLite in
            # local tests) can still surface the unique replay key race.
            # Reloading on the next loop observes either the owner or result.
            db.rollback()
            claim = None
        if claim is not None:
            return claim
        remaining = wait_deadline - monotonic()
        if remaining <= 0:
            raise AgentMCPTurnBusyError(
                "同一外部业务会话的上一轮仍在执行，请在稍后重试"
            )
        sleep(min(0.1, remaining))


def _complete_mcp_turn(
    db: Session,
    *,
    lease: AgentMCPTurnLease,
    result: dict[str, Any],
    latency_ms: int,
) -> None:
    """Persist a complete result only while this worker still owns the lease."""
    _begin_turn_transaction(db)
    try:
        mapping = db.execute(
            select(AgentMCPConversation)
            .where(AgentMCPConversation.id == lease.mapping_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        now = utc_now()
        if not _lease_matches(mapping, lease, now=now):
            raise AgentMCPTurnLeaseLostError("MCP 会话轮次租约已被回收")
        invocation = db.execute(
            select(AgentMCPInvocation)
            .where(AgentMCPInvocation.id == lease.invocation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        if (
            invocation is None
            or invocation.turn_lease_token != lease.token
            or int(invocation.turn_lease_generation or 0) != lease.generation
            or invocation.status != "running"
        ):
            raise AgentMCPTurnLeaseLostError("MCP 调用已被新的轮次围栏")
        invocation.conversation_id = str(result.get("conversation_id") or "") or None
        invocation.trace_id = str(result.get("trace_id") or "")[:64]
        invocation.status = "succeeded"
        invocation.latency_ms = latency_ms
        invocation.tool_call_count = len(result.get("tool_calls") or [])
        invocation.result = _copy_result(result)
        invocation.completed_at = now
        mapping.turn_lease_token = ""
        mapping.turn_lease_expires_at = None
        mapping.turn_lease_deadline_at = None
        mapping.last_used_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise


def _fail_mcp_turn(
    db: Session,
    *,
    lease: AgentMCPTurnLease,
    error: Exception,
    latency_ms: int,
) -> None:
    """Release only the generation that failed; never clear a newer owner."""
    try:
        _begin_turn_transaction(db)
        mapping = db.execute(
            select(AgentMCPConversation)
            .where(AgentMCPConversation.id == lease.mapping_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        invocation = db.execute(
            select(AgentMCPInvocation)
            .where(AgentMCPInvocation.id == lease.invocation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().first()
        if (
            mapping is None
            or invocation is None
            or mapping.turn_lease_token != lease.token
            or int(mapping.turn_lease_generation or 0) != lease.generation
            or invocation.turn_lease_token != lease.token
            or int(invocation.turn_lease_generation or 0) != lease.generation
        ):
            db.rollback()
            return
        invocation.status = "failed"
        invocation.latency_ms = latency_ms
        invocation.error_code = type(error).__name__[:80]
        invocation.error_message = str(error)[:4000]
        invocation.completed_at = utc_now()
        mapping.turn_lease_token = ""
        mapping.turn_lease_expires_at = None
        mapping.turn_lease_deadline_at = None
        db.commit()
    except Exception:
        db.rollback()


def agent_config_hash(agent: Agent) -> str:
    payload = {
        "name": agent.name,
        "description": agent.description or "",
        "scenario_id": agent.scenario_id,
        "llm_config_id": agent.llm_config_id,
        "system_prompt": agent.system_prompt or "",
        "data_source_ids": sorted(str(item) for item in (agent.data_source_ids or [])),
        "capability_scope": agent.capability_scope,
        "temperature": agent.temperature,
        "max_tokens": agent.max_tokens,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str, str, str]:
    raw = f"agt_sk_{secrets.token_urlsafe(32)}"
    return raw, token_hash(raw), raw[:14], raw[-4:]


def client_config(name: str, endpoint_url: str, token: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            name: {
                "type": "http",
                "url": endpoint_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def validate_agent_runtime(
    db: Session,
    agent_id: str,
    *,
    writable: bool = False,
) -> tuple[Agent, Any, list[str]]:
    # Import lazily to keep the MCP service independent from router import order.
    from ..routers import agents

    agent = agents._agent(db, agent_id, writable=writable)
    # The current public MCP product intentionally shares the same live
    # authoring context as the browser Agent chat.  A deployment environment
    # selects infrastructure only; it must not require a release, snapshot, or
    # logical connector binding before a working Agent can be published.
    context = agents._authorization_context(db, agent, definition_mode="authoring")
    if context is None:
        return agent, None, ["当前 Agent 对话上下文或连接器"]
    missing = agents._agent_readiness_missing(db, agent, runtime_context=context)
    return agent, context, missing


def service_runtime_status(
    db: Session,
    service: AgentMCPService,
) -> tuple[Agent | None, Any | None, list[str], bool]:
    previous_tenant = db.info.get("tenant_id")
    previous_user = db.info.get("user_id")
    try:
        db.info["tenant_id"] = service.tenant_id
        db.info["user_id"] = service.execution_user_id or ""
        try:
            agent, context, missing = validate_agent_runtime(db, service.agent_id)
        except HTTPException as exc:
            return None, None, [str(exc.detail)], True
        if context is None:
            return agent, None, missing, True
        # A published service follows the current Agent configuration and live
        # definition.  Stored hashes remain useful issuance provenance, but
        # must not turn a successful browser Agent into a 409 for its MCP
        # callers after an ordinary edit.
        return agent, context, missing, False
    finally:
        if previous_tenant is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous_tenant
        if previous_user is None:
            db.info.pop("user_id", None)
        else:
            db.info["user_id"] = previous_user


def authenticate_token(raw_token: str) -> AuthenticatedAgentMCP | None:
    if not raw_token.startswith("agt_sk_") or len(raw_token) > 512:
        return None
    db = SessionLocal()
    try:
        now = utc_now()
        service = db.execute(
            select(AgentMCPService).where(
                AgentMCPService.token_hash == token_hash(raw_token),
                AgentMCPService.enabled.is_(True),
                AgentMCPService.deleted_at.is_(None),
            )
        ).scalars().first()
        if not service or not service.execution_user_id:
            return None
        expires_at = service.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return None
        service.last_used_at = now
        db.commit()
        return AuthenticatedAgentMCP(
            service_id=service.id,
            tenant_id=service.tenant_id,
            execution_user_id=service.execution_user_id,
        )
    finally:
        db.close()


def _owned_conversation(
    db: Session,
    service: AgentMCPService,
    conversation_id: str,
    *,
    agents: Any,
) -> Conversation:
    """Return an explicitly supplied transcript only after publication proof.

    The returned internal id is never a bearer capability: a client must have
    first received it from an invocation of this exact published service, and
    it must still be owned by the publication's execution principal.
    """
    owned = db.execute(
        select(AgentMCPInvocation.id).where(
            AgentMCPInvocation.service_id == service.id,
            AgentMCPInvocation.conversation_id == conversation_id,
        ).limit(1)
    ).scalar_one_or_none()
    if not owned:
        raise AgentMCPError("对话不属于当前 Agent MCP 服务")
    conversation = agents._conversation(db, conversation_id)
    if conversation.agent_id != service.agent_id:
        raise AgentMCPError("对话不属于当前 Agent MCP 服务")
    return conversation


def _new_external_conversation(service: AgentMCPService, message: str) -> Conversation:
    """Create an id up front so the mapping and transcript commit atomically."""
    return Conversation(
        id=uuid.uuid4().hex,
        agent_id=service.agent_id,
        created_by_user_id=service.execution_user_id,
        title=message[:50] or "新对话",
    )


def _conversation_for_external_binding(
    db: Session,
    service: AgentMCPService,
    *,
    message: str,
    conversation_id: str | None,
    binding_key: str | None,
    binding_mode: str,
    agents: Any,
    _retry_on_integrity: bool = True,
) -> MCPConversationBinding:
    """Resolve a durable transcript from an explicit business binding.

    A transport session is deliberately not a transcript key: MCP clients often
    reuse one connection across unrelated UI chats. A unique hashed binding is
    still the cross-worker single-flight boundary for a first call or retry.
    """
    if not binding_key:
        if conversation_id:
            return MCPConversationBinding(
                mapping_id=None,
                conversation_id=_owned_conversation(
                    db, service, conversation_id, agents=agents
                ).id,
                binding_key_hash=None,
            )
        # Backward-compatible direct service calls are still possible, but the
        # HTTP gateway always supplies a per-conversation binding key.
        return MCPConversationBinding(
            mapping_id=None,
            conversation_id=None,
            binding_key_hash=None,
        )

    requested = (
        _owned_conversation(db, service, conversation_id, agents=agents)
        if conversation_id
        else None
    )
    binding_hash = conversation_binding_hash(binding_key)
    identity_predicate = AgentMCPConversation.external_session_hash == binding_hash
    if requested is not None:
        identity_predicate = or_(
            identity_predicate,
            AgentMCPConversation.conversation_id == requested.id,
        )
    # Discover candidate ids without locks, then lock every involved row in one
    # primary-key order. Crossed bad requests therefore cannot create A->B / B->A
    # lock cycles between an external key and a platform conversation.
    candidate_ids = sorted(
        set(
            db.execute(
                select(AgentMCPConversation.id).where(
                    AgentMCPConversation.service_id == service.id,
                    identity_predicate,
                )
            )
            .scalars()
            .all()
        )
    )
    mappings = (
        db.execute(
            select(AgentMCPConversation)
            .where(AgentMCPConversation.id.in_(candidate_ids))
            .order_by(AgentMCPConversation.id.asc())
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalars().all()
        if candidate_ids
        else []
    )
    mapping_by_key = next(
        (item for item in mappings if item.external_session_hash == binding_hash),
        None,
    )
    mapping_by_conversation = next(
        (
            item
            for item in mappings
            if requested is not None and item.conversation_id == requested.id
        ),
        None,
    )
    if (
        mapping_by_key is not None
        and mapping_by_conversation is not None
        and mapping_by_key.id != mapping_by_conversation.id
    ):
        raise AgentMCPError(
            "CONVERSATION_BINDING_CONFLICT: external_conversation_id 与平台对话绑定不一致"
        )

    mapping = mapping_by_key or mapping_by_conversation
    if (
        binding_mode == "external_conversation_id"
        and requested is not None
        and mapping_by_key is None
        and mapping_by_conversation is not None
    ):
        # A bare platform id proves only that this publication used the
        # transcript; it cannot prove which host UI chat owns it. Automatically
        # re-keying legacy transport mappings would recreate the original
        # cross-chat bug when a host carries A's continuation into chat B.
        raise AgentMCPError(
            "CONVERSATION_BINDING_CONFLICT: 旧平台对话不能由新的外部会话标识自动认领"
        )

    if (
        mapping_by_key is not None
        and mapping_by_key.conversation_id is None
        and requested is not None
    ):
        raise AgentMCPError(
            "CONVERSATION_BINDING_CONFLICT: 已删除的外部会话不能绑定到其他平台对话"
        )

    if mapping is not None:
        if (
            mapping.tenant_id != service.tenant_id
            or mapping.agent_id != service.agent_id
            or mapping.execution_user_id != service.execution_user_id
        ):
            raise AgentMCPError("MCP 会话作用域无效")
        if (
            requested is not None
            and mapping.conversation_id
            and mapping.conversation_id != requested.id
        ):
            raise AgentMCPError(
                "CONVERSATION_BINDING_CONFLICT: MCP 会话已绑定到另一条对话"
            )
        bound_id = requested.id if requested is not None else mapping.conversation_id
        if bound_id:
            try:
                bound = agents._conversation(db, bound_id)
            except HTTPException as exc:
                # Conversation deletion intentionally does not delete the
                # binding record. A later call starts a new clean transcript
                # under the same explicit external conversation identity.
                if exc.status_code != 404:
                    raise
                bound = None
            if bound is not None and bound.agent_id != service.agent_id:
                raise AgentMCPError("MCP 会话绑定的对话无效")
        else:
            bound = None
        if bound is None:
            bound = _new_external_conversation(service, message)
            db.add(bound)
            # AgentMCPConversation does not own an ORM relationship to the
            # transcript, so PostgreSQL cannot infer the FK insertion order
            # from a raw ``conversation_id`` assignment below.
            db.flush()
            mapping.conversation_id = bound.id
        mapping.last_used_at = utc_now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            if _retry_on_integrity:
                return _conversation_for_external_binding(
                    db,
                    service,
                    message=message,
                    conversation_id=conversation_id,
                    binding_key=binding_key,
                    binding_mode=binding_mode,
                    agents=agents,
                    _retry_on_integrity=False,
                )
            raise AgentMCPError("MCP 会话绑定并发冲突，请重试") from exc
        return MCPConversationBinding(
            mapping_id=mapping.id,
            conversation_id=bound.id,
            binding_key_hash=mapping.external_session_hash,
        )

    bound = requested or _new_external_conversation(service, message)
    mapping = AgentMCPConversation(
        id=uuid.uuid4().hex,
        service_id=service.id,
        tenant_id=service.tenant_id,
        agent_id=service.agent_id,
        execution_user_id=service.execution_user_id,
        # Physical column name is retained for migration compatibility. Its
        # value is now a business binding hash, never a transport-session key.
        external_session_hash=binding_hash,
        binding_kind=binding_mode,
        conversation_id=bound.id,
        last_used_at=utc_now(),
    )
    if requested is None:
        db.add(bound)
        # Persist the transcript before adding its MCP mapping.  PostgreSQL
        # enforces the conversation FK immediately; SQLite's usual test
        # configuration does not, which previously hid this ordering bug.
        db.flush()
    db.add(mapping)
    try:
        db.commit()
        return MCPConversationBinding(
            mapping_id=mapping.id,
            conversation_id=bound.id,
            binding_key_hash=binding_hash,
        )
    except IntegrityError as exc:
        # A concurrent caller may have won either the business-key constraint
        # or the canonical service/conversation constraint. Rediscover and lock
        # both identities before deciding whether this is a replay or conflict.
        db.rollback()
        if _retry_on_integrity:
            return _conversation_for_external_binding(
                db,
                service,
                message=message,
                conversation_id=conversation_id,
                binding_key=binding_key,
                binding_mode=binding_mode,
                agents=agents,
                _retry_on_integrity=False,
            )
        raise AgentMCPError("MCP 会话初始化并发冲突，请重试") from exc


def invoke_published_agent(
    service_id: str,
    *,
    message: str,
    conversation_id: str | None,
    external_session_id: str | None = None,
    external_request_id: str | None = None,
    external_conversation_id: str | None = None,
    external_turn_id: str | None = None,
    input_source: str = "tool_argument",
    tool_argument_matched: bool = True,
) -> dict[str, Any]:
    from ..routers import agents

    db = SessionLocal()
    started = perf_counter()
    invocation: AgentMCPInvocation | None = None
    lease: AgentMCPTurnLease | None = None
    previous_guard: Any = None
    previous_request_hash: Any = None
    had_guard = False
    had_request_hash = False
    try:
        service = db.get(AgentMCPService, service_id)
        if not service or not service.enabled or not service.execution_user_id:
            raise AgentMCPError("Agent MCP 服务不存在或已停用")
        db.info["tenant_id"] = service.tenant_id
        db.info["user_id"] = service.execution_user_id
        permission_service.require_principal(db)

        agent, context, missing, stale = service_runtime_status(db, service)
        if not agent or context is None:
            raise AgentMCPError("Agent 当前不可用：" + "、".join(missing))
        if missing:
            raise AgentMCPError("Agent 尚未就绪：" + "、".join(missing))
        binding_key, binding_mode = _conversation_binding_key(
            message=message,
            conversation_id=conversation_id,
            external_conversation_id=external_conversation_id,
            external_session_id=external_session_id,
            external_request_id=external_request_id,
        )
        binding = _conversation_for_external_binding(
            db,
            service,
            message=message,
            conversation_id=conversation_id,
            binding_key=binding_key,
            binding_mode=binding_mode,
            agents=agents,
        )

        request_id = uuid.uuid4().hex
        input_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if binding.mapping_id is not None:
            # FastMCP exposes the JSON-RPC request id.  Direct callers that
            # predate the gateway still receive a deterministic payload key,
            # which makes a retry safe rather than silently duplicating an
            # automatic action.
            external_turn_id = _external_identifier(
                external_turn_id,
                label="external_turn_id",
            )
            if external_turn_id:
                # Host turn ids are commonly local counters such as "1". Scope
                # them to the publication and business conversation before the
                # hash also reaches automatic Action idempotency keys.
                request_identity = (
                    f"external-turn\0{service.id}\0"
                    f"{binding.binding_key_hash or binding.mapping_id}\0"
                    f"{external_turn_id}"
                )
            else:
                rpc_request_id = str(external_request_id or "").strip()
                request_identity = (
                    f"transport-request\0{external_session_id or ''}\0{rpc_request_id}\0{input_hash}"
                    if rpc_request_id
                    else ""
                )
            if not request_identity:
                request_identity = f"payload:{input_hash}"
            request_hash = external_request_hash(request_identity)
            claim = _claim_mcp_turn(
                db,
                service=service,
                binding=binding,
                request_hash=request_hash,
                input_hash=input_hash,
                request_id=request_id,
            )
            if claim.replay_result is not None:
                return claim.replay_result
            lease = claim.lease
            if lease is None or claim.invocation_id is None:
                raise AgentMCPError("MCP 会话轮次声明无效")
            invocation = db.get(AgentMCPInvocation, claim.invocation_id)
            if invocation is None:
                raise AgentMCPError("MCP 调用记录不存在")
            conversation_id = binding.conversation_id
            had_guard = "agent_mcp_turn_lease_guard" in db.info
            previous_guard = db.info.get("agent_mcp_turn_lease_guard")
            had_request_hash = "agent_mcp_turn_request_hash" in db.info
            previous_request_hash = db.info.get("agent_mcp_turn_request_hash")
            db.info["agent_mcp_turn_lease_guard"] = lease
            db.info["agent_mcp_turn_request_hash"] = request_hash
            lease.start()
            lease.assert_active()
        else:
            # The HTTP gateway always supplies a binding key. Keep this
            # narrow fallback for in-process callers built before published
            # MCP conversation mappings existed; it has no cross-worker ordering.
            conversation_id = binding.conversation_id
            invocation = AgentMCPInvocation(
                service_id=service.id,
                tenant_id=service.tenant_id,
                agent_id=service.agent_id,
                execution_user_id=service.execution_user_id,
                request_id=request_id,
                conversation_id=conversation_id,
                input_hash=input_hash,
                status="running",
            )
            db.add(invocation)
            db.commit()

        result = agents.invoke_agent_once(
            service.agent_id,
            message=message,
            conversation_id=conversation_id,
            db=db,
            runtime_context=context,
        )
        if not isinstance(result, dict):
            raise AgentMCPError("Agent MCP 调用未返回结构化结果")
        if lease is not None:
            lease.assert_active()
        result.update({
            "request_id": invocation.request_id if invocation is not None else request_id,
            "mcp_service_id": service.id,
            "mcp_service_name": service.name,
            "mcp_conversation_mode": binding_mode,
            "mcp_replayed": False,
            "mcp_input_receipt": {
                "source": str(input_source or "tool_argument")[:80],
                "message_sha256": input_hash,
                "message_length": len(message),
                "tool_argument_matched": bool(tool_argument_matched),
                "external_conversation_bound": bool(external_conversation_id),
                "external_turn_bound": bool(external_turn_id),
                "conversation_binding_hash": binding.binding_key_hash or "",
            },
        })
        # The audit result doubles as the exact retry response.  Normalize it
        # once here so a first response and a durable replay have identical
        # JSON shape even when a connector returned a datetime-like value.
        result = _copy_result(result)
        latency_ms = int((perf_counter() - started) * 1000)
        if lease is not None:
            lease.stop()
            _complete_mcp_turn(
                db,
                lease=lease,
                result=result,
                latency_ms=latency_ms,
            )
        elif invocation is not None:
            invocation.conversation_id = result.get("conversation_id") or None
            invocation.trace_id = str(result.get("trace_id") or "")[:64]
            invocation.status = "succeeded"
            invocation.latency_ms = latency_ms
            invocation.tool_call_count = len(result.get("tool_calls") or [])
            invocation.result = _copy_result(result)
            invocation.completed_at = utc_now()
            db.commit()
        return result
    except Exception as exc:
        if lease is not None:
            lease.stop()
            _fail_mcp_turn(
                db,
                lease=lease,
                error=exc,
                latency_ms=int((perf_counter() - started) * 1000),
            )
        elif invocation is not None:
            try:
                invocation.status = "failed"
                invocation.latency_ms = int((perf_counter() - started) * 1000)
                invocation.error_code = type(exc).__name__[:80]
                invocation.error_message = str(exc)[:4000]
                invocation.completed_at = utc_now()
                db.commit()
            except Exception:
                db.rollback()
        raise
    finally:
        if had_guard:
            db.info["agent_mcp_turn_lease_guard"] = previous_guard
        else:
            db.info.pop("agent_mcp_turn_lease_guard", None)
        if had_request_hash:
            db.info["agent_mcp_turn_request_hash"] = previous_request_hash
        else:
            db.info.pop("agent_mcp_turn_request_hash", None)
        db.close()
