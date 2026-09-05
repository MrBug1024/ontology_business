"""add durable attachment-backed assistant request runs

Revision ID: 20260904_22
Revises: 20260904_21
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_22"
down_revision: Union[str, None] = "20260904_21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_document_type():
    return sa.JSON().with_variant(
        postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
        "postgresql",
    )


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _runtime_role_statement(action: str) -> sa.TextClause:
    if action not in {"GRANT", "REVOKE"}:
        raise ValueError("unsupported privilege action")
    role_statement = (
        "REVOKE ALL ON TABLE assistant_request_runs FROM ontology_app; "
        "GRANT SELECT, INSERT, UPDATE ON TABLE assistant_request_runs TO ontology_app;"
        if action == "GRANT"
        else "REVOKE ALL ON TABLE assistant_request_runs FROM ontology_app;"
    )
    return sa.text(
        f"""DO $$
        BEGIN
          REVOKE ALL ON TABLE assistant_request_runs FROM PUBLIC;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            {role_statement}
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_assistant_threads_id_tenant",
        "assistant_threads",
        ["id", "tenant_id"],
    )
    op.create_unique_constraint(
        "uq_assistant_threads_id_tenant_user",
        "assistant_threads",
        ["id", "tenant_id", "created_by_user_id"],
    )
    op.create_unique_constraint(
        "uq_assistant_messages_id_thread",
        "assistant_messages",
        ["id", "thread_id"],
    )
    op.create_table(
        "assistant_request_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=32), nullable=False),
        sa.Column("user_message_id", sa.String(length=32), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=32), nullable=False),
        sa.Column("payload_document", _json_document_type(), nullable=False),
        sa.Column("upload_run_ids", _json_document_type(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('waiting_upload', 'queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_assistant_request_runs_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_assistant_request_runs_revision"),
        sa.CheckConstraint(
            "lease_generation >= 0",
            name="ck_assistant_request_runs_lease_generation",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token <> '' AND lease_generation > 0 "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_token = '' AND lease_expires_at IS NULL)",
            name="ck_assistant_request_runs_lease_state",
        ),
        sa.CheckConstraint(
            _sha256_check("request_fingerprint"),
            name="ck_assistant_request_runs_fingerprint",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_assistant_request_runs_user_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "tenant_id", "requested_by_user_id"],
            [
                "assistant_threads.id",
                "assistant_threads.tenant_id",
                "assistant_threads.created_by_user_id",
            ],
            name="fk_assistant_request_runs_thread_principal",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id", "thread_id"],
            ["assistant_messages.id", "assistant_messages.thread_id"],
            name="fk_assistant_request_runs_user_message_thread",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id", "thread_id"],
            ["assistant_messages.id", "assistant_messages.thread_id"],
            name="fk_assistant_request_runs_assistant_message_thread",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "requested_by_user_id",
            "request_id",
            name="uq_assistant_request_runs_principal_request",
        ),
    )
    for column in (
        "tenant_id",
        "requested_by_user_id",
        "thread_id",
        "status",
    ):
        op.create_index(
            f"ix_assistant_request_runs_{column}",
            "assistant_request_runs",
            [column],
        )
    op.create_index(
        "ix_assistant_request_runs_dispatch",
        "assistant_request_runs",
        ["status", "available_at", "lease_expires_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))
    op.drop_table("assistant_request_runs")
    op.drop_constraint(
        "uq_assistant_messages_id_thread",
        "assistant_messages",
        type_="unique",
    )
    op.drop_constraint(
        "uq_assistant_threads_id_tenant_user",
        "assistant_threads",
        type_="unique",
    )
    op.drop_constraint(
        "uq_assistant_threads_id_tenant",
        "assistant_threads",
        type_="unique",
    )
