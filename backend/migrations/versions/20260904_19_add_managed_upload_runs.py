"""add durable managed upload intake and profiling runs

Revision ID: 20260904_19
Revises: 20260904_18
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_19"
down_revision: Union[str, None] = "20260904_18"
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
    grantee_clause = "TO" if action == "GRANT" else "FROM"
    return sa.text(
        f"""DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            {action} SELECT, INSERT, UPDATE, DELETE ON TABLE managed_upload_runs
              {grantee_clause} ontology_app;
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_agent_turn_runs_parent_retry",
        "agent_turn_runs",
        ["tenant_id", "parent_run_id"],
    )
    op.create_table(
        "managed_upload_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("data_source_id", sa.String(length=32), nullable=False),
        sa.Column("bucket_file_id", sa.String(length=32), nullable=True),
        sa.Column("asset_id", sa.String(length=32), nullable=True),
        sa.Column("asset_version_id", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("client_media_type", sa.String(length=200), nullable=False),
        sa.Column("declared_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_document", _json_document_type(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('validation_asset', 'invocation_attachment')",
            name="ck_managed_upload_runs_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_upload', 'uploading', 'stored', 'processing', "
            "'ready', 'failed', 'cancelled')",
            name="ck_managed_upload_runs_status",
        ),
        sa.CheckConstraint(
            "declared_byte_size > 0", name="ck_managed_upload_runs_declared_size"
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_managed_upload_runs_size"),
        sa.CheckConstraint("revision > 0", name="ck_managed_upload_runs_revision"),
        sa.CheckConstraint(
            "lease_generation >= 0", name="ck_managed_upload_runs_lease_generation"
        ),
        sa.CheckConstraint(
            _sha256_check("request_fingerprint"),
            name="ck_managed_upload_runs_request_fingerprint",
        ),
        sa.CheckConstraint(
            "content_sha256 = '' OR (" + _sha256_check("content_sha256") + ")",
            name="ck_managed_upload_runs_content_sha256",
        ),
        sa.CheckConstraint(
            "(asset_id IS NULL) = (asset_version_id IS NULL)",
            name="ck_managed_upload_runs_asset_pair",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"], ["data_sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["bucket_file_id"], ["bucket_files.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["data_assets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"], ["data_asset_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_managed_upload_runs_user_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_managed_upload_runs_source_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bucket_file_id", "data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_managed_upload_runs_file_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["data_assets.id", "data_assets.tenant_id"],
            name="fk_managed_upload_runs_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id", "tenant_id"],
            ["data_asset_versions.id", "data_asset_versions.tenant_id"],
            name="fk_managed_upload_runs_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "requested_by_user_id",
            "idempotency_key",
            name="uq_managed_upload_runs_principal_request",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_managed_upload_runs_id_tenant"),
    )
    for column in (
        "tenant_id",
        "requested_by_user_id",
        "data_source_id",
        "bucket_file_id",
        "asset_id",
        "asset_version_id",
    ):
        op.create_index(f"ix_managed_upload_runs_{column}", "managed_upload_runs", [column])
    op.create_index(
        "ix_managed_upload_runs_dispatch",
        "managed_upload_runs",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_managed_upload_runs_owner_created",
        "managed_upload_runs",
        ["tenant_id", "requested_by_user_id", "created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))
    op.drop_table("managed_upload_runs")
    op.drop_constraint(
        "uq_agent_turn_runs_parent_retry",
        "agent_turn_runs",
        type_="unique",
    )
