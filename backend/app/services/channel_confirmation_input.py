"""Encrypted continuation inputs for authenticated external message replies."""
from __future__ import annotations

from dataclasses import asdict
import json

from sqlalchemy.orm import Session

from ..models import CapabilityInvocation
from . import agent_turn_payload_service
from .capability_contracts import Actor, CapabilityRef, DataBindingOverride, Request, canonical_json


STORAGE_KEY = "channel_confirmation_input_v1"


def _context(invocation: CapabilityInvocation, actor: Actor) -> dict[str, str]:
    return {
        "contract": "channel-confirmation-input/v1", "tenant_id": actor.tenant_id,
        "principal_type": actor.actor_type, "principal_id": actor.principal_id,
        "user_id": str(actor.user_id or ""), "invocation_id": invocation.id,
        "definition_hash": invocation.definition_hash, "deployment_fingerprint": invocation.deployment_fingerprint,
        "input_hash": invocation.input_hash,
    }


def remember(db: Session, invocation_id: str, actor: Actor, request: Request) -> None:
    if actor.actor_type == "agent":
        return
    invocation = db.get(CapabilityInvocation, invocation_id)
    if invocation is None or invocation.status != "awaiting_confirmation":
        return
    document = dict(invocation.request_document or {})
    if STORAGE_KEY in document:
        return
    sealed = agent_turn_payload_service.seal_payload({
        "inputs": json.loads(canonical_json(request.inputs)),
        "binding_overrides": [asdict(item) for item in request.binding_overrides],
        "request_id": request.request_id,
        "confirmation": (invocation.result_document or {}).get("confirmation") or {},
    }, context=_context(invocation, actor))
    document[STORAGE_KEY] = {"envelope": sealed.envelope, "summary": sealed.summary, "digest": sealed.digest}
    invocation.request_document = document
    db.flush()


def restore(invocation: CapabilityInvocation, actor: Actor) -> Request:
    stored = (invocation.request_document or {}).get(STORAGE_KEY) or {}
    payload = agent_turn_payload_service.open_payload(
        stored.get("envelope") or {}, context=_context(invocation, actor),
        summary=stored.get("summary") or {}, digest=str(stored.get("digest") or ""),
    )
    return Request(
        capability=CapabilityRef(kind=invocation.capability_kind, resource_id=invocation.capability_key),
        inputs=payload["inputs"], binding_overrides=tuple(DataBindingOverride(**item) for item in payload["binding_overrides"]),
        mode="confirm", idempotency_key=invocation.idempotency_key, correlation_id=invocation.correlation_id,
        expected_definition_hash=invocation.definition_hash,
        expected_deployment_fingerprint=invocation.deployment_fingerprint,
        confirmation=payload["confirmation"], request_id=payload["request_id"],
    )
