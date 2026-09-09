"""Authenticated, server-only inputs for a later human capability confirmation."""
from __future__ import annotations

from dataclasses import asdict
import json

from sqlalchemy.orm import Session

from ..models import CapabilityInvocation
from . import agent_turn_payload_service
from .capability_contracts import Actor, DataBindingOverride, Request, CapabilityRef, canonical_json


STORAGE_KEY = "agent_confirmation_input_v1"


def _context(invocation: CapabilityInvocation, user_id: str, message_id: str) -> dict[str, str]:
    return {
        "contract": "agent-capability-confirmation-input/v1",
        "invocation_id": invocation.id,
        "tenant_id": invocation.tenant_id,
        "agent_id": invocation.principal_id,
        "user_id": user_id,
        "message_id": message_id,
        "definition_hash": invocation.definition_hash,
        "deployment_fingerprint": invocation.deployment_fingerprint,
    }


def remember_preview(db: Session, invocation_id: str, actor: Actor, request: Request) -> None:
    trace = db.info.get("llm_trace_context") or {}
    message_id = str(trace.get("assistant_message_id") or "")
    if not message_id:
        return
    invocation = db.get(CapabilityInvocation, invocation_id)
    if invocation is None or invocation.status != "awaiting_confirmation":
        return
    document = dict(invocation.request_document or {})
    if STORAGE_KEY in document:
        return
    sealed = agent_turn_payload_service.seal_payload(
        {
            "inputs": json.loads(canonical_json(request.inputs)),
            "binding_overrides": [asdict(item) for item in request.binding_overrides],
            "request_id": request.request_id,
            "confirmation": (invocation.result_document or {}).get("confirmation") or {},
        },
        context=_context(invocation, str(actor.user_id or ""), message_id),
    )
    document[STORAGE_KEY] = {
        "message_id": message_id,
        "user_id": actor.user_id,
        "envelope": sealed.envelope,
        "summary": sealed.summary,
        "digest": sealed.digest,
    }
    invocation.request_document = document
    db.flush()


def restore_request(invocation: CapabilityInvocation, user_id: str, message_id: str) -> Request:
    stored = (invocation.request_document or {}).get(STORAGE_KEY) or {}
    payload = agent_turn_payload_service.open_payload(
        stored.get("envelope") or {},
        context=_context(invocation, user_id, message_id),
        summary=stored.get("summary") or {},
        digest=str(stored.get("digest") or ""),
    )
    return Request(
        capability=CapabilityRef(kind=invocation.capability_kind, resource_id=invocation.capability_key),
        inputs=payload["inputs"],
        binding_overrides=tuple(DataBindingOverride(**item) for item in payload["binding_overrides"]),
        mode="confirm",
        idempotency_key=invocation.idempotency_key,
        correlation_id=invocation.correlation_id,
        expected_definition_hash=invocation.definition_hash,
        expected_deployment_fingerprint=invocation.deployment_fingerprint,
        confirmation=payload["confirmation"],
        request_id=payload["request_id"],
    )
