"""encrypt durable workflow run inputs

Revision ID: 20260829_09
Revises: 20260829_08
Create Date: 2026-08-29

Existing plaintext rows are encrypted in the migration transaction.  If any
row exists, the deployment must provide the same external workflow payload key
ring used by the application; otherwise upgrade/downgrade aborts without
silently discarding an input or pretending an irreversible hash is recoverable.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.services import workflow_payload_service


revision: str = "20260829_09"
down_revision: Union[str, None] = "20260829_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_runs = sa.table(
    "workflow_runs",
    sa.column("id", sa.String()),
    sa.column("scenario_id", sa.String()),
    sa.column("workflow_id", sa.String()),
    sa.column("environment", sa.String()),
    sa.column("definition_hash", sa.String()),
    sa.column("input_payload", sa.JSON()),
    sa.column("input_summary", sa.JSON()),
    sa.column("input_digest", sa.String()),
)
_BATCH_SIZE = 200


def _plain_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "workflow_runs contains a non-JSON legacy input; migration aborted"
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(
        "workflow_runs contains a non-object legacy input; migration aborted"
    )


def _context(row: Mapping[str, Any]) -> dict[str, str]:
    return workflow_payload_service.payload_context(
        run_id=str(row["id"] or ""),
        scenario_id=str(row["scenario_id"] or ""),
        workflow_id=str(row["workflow_id"] or ""),
        environment=str(row["environment"] or ""),
        definition_hash=str(row["definition_hash"] or ""),
    )


def _encrypt_existing_rows(connection: Any) -> int:
    count = 0
    last_id = ""
    keyring = None
    while True:
        rows = list(
            connection.execute(
                sa.select(
                    _runs.c.id,
                    _runs.c.scenario_id,
                    _runs.c.workflow_id,
                    _runs.c.environment,
                    _runs.c.definition_hash,
                    _runs.c.input_payload,
                )
                .where(_runs.c.id > last_id)
                .order_by(_runs.c.id)
                .limit(_BATCH_SIZE)
            ).mappings()
        )
        if not rows:
            break
        if keyring is None:
            # Validate before the first rewrite. The Alembic transaction keeps
            # every batch atomic if a later malformed row aborts the migration.
            keyring = workflow_payload_service.load_keyring()
        for row in rows:
            sealed = workflow_payload_service.seal_payload(
                _plain_object(row["input_payload"]),
                context=_context(row),
                keyring=keyring,
            )
            connection.execute(
                _runs.update()
                .where(_runs.c.id == row["id"])
                .values(
                    input_payload=sealed.envelope,
                    input_summary=sealed.summary,
                    input_digest=sealed.digest,
                )
            )
        count += len(rows)
        last_id = str(rows[-1]["id"])
    return count


def _decrypt_existing_rows(connection: Any) -> int:
    count = 0
    last_id = ""
    keyring = None
    while True:
        rows = list(
            connection.execute(
                sa.select(
                    _runs.c.id,
                    _runs.c.scenario_id,
                    _runs.c.workflow_id,
                    _runs.c.environment,
                    _runs.c.definition_hash,
                    _runs.c.input_payload,
                    _runs.c.input_summary,
                    _runs.c.input_digest,
                )
                .where(_runs.c.id > last_id)
                .order_by(_runs.c.id)
                .limit(_BATCH_SIZE)
            ).mappings()
        )
        if not rows:
            break
        if keyring is None:
            keyring = workflow_payload_service.load_keyring()
        for row in rows:
            plaintext = workflow_payload_service.open_payload(
                _plain_object(row["input_payload"]),
                context=_context(row),
                summary=_plain_object(row["input_summary"]),
                digest=str(row["input_digest"] or ""),
                keyring=keyring,
            )
            connection.execute(
                _runs.update()
                .where(_runs.c.id == row["id"])
                .values(input_payload=plaintext)
            )
        count += len(rows)
        last_id = str(rows[-1]["id"])
    return count


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "input_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("input_digest", sa.String(length=64), nullable=True),
    )
    op.alter_column(
        "workflow_runs",
        "input_params",
        new_column_name="input_payload",
        existing_type=sa.JSON(),
        existing_nullable=False,
    )
    _encrypt_existing_rows(op.get_bind())
    op.alter_column(
        "workflow_runs",
        "input_summary",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    )
    op.alter_column(
        "workflow_runs",
        "input_digest",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_workflow_runs_input_digest",
        "workflow_runs",
        "input_digest = '' OR (length(input_digest) = 64 "
        "AND input_digest = lower(input_digest))",
    )


def downgrade() -> None:
    # Downgrade intentionally restores the legacy plaintext column and therefore
    # also requires every historical key referenced by an existing envelope.
    # Missing/tampered payloads abort the transaction rather than losing data.
    op.drop_constraint(
        "ck_workflow_runs_input_digest",
        "workflow_runs",
        type_="check",
    )
    _decrypt_existing_rows(op.get_bind())
    op.alter_column(
        "workflow_runs",
        "input_payload",
        new_column_name="input_params",
        existing_type=sa.JSON(),
        existing_nullable=False,
    )
    op.drop_column("workflow_runs", "input_digest")
    op.drop_column("workflow_runs", "input_summary")


__all__ = [
    "downgrade",
    "revision",
    "down_revision",
    "upgrade",
]
