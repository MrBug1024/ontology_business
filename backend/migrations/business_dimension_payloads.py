"""Authenticated, bounded payload conversion for migration 20260908_27."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import sqlalchemy as sa

from app.schemas import ChatRequest
from app.services import agent_turn_payload_service, workflow_payload_service


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def migrate_payloads(connection: sa.Connection) -> None:
    metadata = sa.MetaData()
    workflows = sa.Table("workflow_runs", metadata, autoload_with=connection)
    turns = sa.Table("agent_turn_runs", metadata, autoload_with=connection)
    messages = sa.Table("messages", metadata, autoload_with=connection)
    try:
        _workflows(connection, workflows)
        _turns(connection, turns, messages)
    except Exception:
        # Validation/decryption exceptions may include payload values. The transaction rolls back.
        raise RuntimeError("Encrypted runtime payload migration failed; verify the configured key ring and source integrity") from None


def _workflows(connection: sa.Connection, table: sa.Table) -> None:
    cursor = ""
    while True:
        rows = connection.execute(sa.select(table).where(table.c.id > cursor).order_by(table.c.id).limit(100)).mappings().all()
        if not rows:
            return
        for row in rows:
            common = {"run_id": row["id"], "scenario_id": row["scenario_id"], "workflow_id": row["workflow_id"], "definition_hash": row["definition_hash"]}
            old_context = workflow_payload_service.payload_context(**common, environment=row["environment"])
            plain = workflow_payload_service.open_payload(row["input_payload"], context=old_context, summary=row["input_summary"], digest=row["input_digest"])
            sealed = workflow_payload_service.seal_payload(plain, context=workflow_payload_service.runtime_payload_context(**common))
            connection.execute(table.update().where(table.c.id == row["id"]).values(input_payload=sealed.envelope, input_summary=sealed.summary, input_digest=sealed.digest))
        cursor = rows[-1]["id"]


def _context(row: SimpleNamespace, fingerprint: str) -> dict[str, str]:
    return {
        "contract": "agent-turn-payload-context/v1", "run_id": row.id,
        "tenant_id": row.tenant_id, "requested_by_user_id": row.requested_by_user_id or "",
        "agent_id": row.agent_id or "", "conversation_id": row.conversation_id or "",
        "request_fingerprint": fingerprint,
    }


def _turns(connection: sa.Connection, table: sa.Table, messages: sa.Table) -> None:
    cursor = ""
    while True:
        rows = connection.execute(sa.select(table, messages.c.content.label("message_content")).outerjoin(
            messages, messages.c.id == table.c.user_message_id,
        ).where(table.c.id > cursor).order_by(table.c.id).limit(100)).mappings().all()
        if not rows:
            return
        for mapping in rows:
            row = SimpleNamespace(**mapping)
            if (
                row.status in {"succeeded", "failed", "cancelled", "indeterminate"}
                and row.conversation_id is None
                and row.user_message_id is None
                and row.assistant_message_id is None
            ):
                # Conversation deletion intentionally retains terminal audit rows,
                # but removes their authenticated context. Keep the original sealed
                # evidence; these rows cannot be resumed or retried by the runtime.
                continue
            document = agent_turn_payload_service.open_payload(
                row.request_payload, context=_context(row, row.request_fingerprint),
                summary=row.request_summary, digest=row.request_digest,
            )
            document.pop("environment", None)
            document["message"] = row.message_content or ""
            payload = ChatRequest.model_validate(document)
            fingerprint = hashlib.sha256(_json({
                "contract": "agent-turn-request/v1", "tenant_id": row.tenant_id,
                "user_id": row.requested_by_user_id or "", "agent_id": row.agent_id or "",
                "parent_run_id": row.parent_run_id or "",
                "request": payload.model_dump(mode="json", exclude_none=True),
            })).hexdigest()
            sealed = agent_turn_payload_service.seal_payload(
                payload.model_dump(mode="json", exclude={"message"}, exclude_none=True),
                context=_context(row, fingerprint),
            )
            connection.execute(table.update().where(table.c.id == row.id).values(
                request_payload=sealed.envelope, request_summary=sealed.summary,
                request_digest=sealed.digest, request_fingerprint=fingerprint,
            ))
        cursor = rows[-1]["id"]
