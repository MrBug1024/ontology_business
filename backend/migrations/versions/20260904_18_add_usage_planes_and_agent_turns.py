"""add authoritative data usage planes and durable Agent turns

Revision ID: 20260904_18
Revises: 20260831_17
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_18"
down_revision: Union[str, None] = "20260831_17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


AGENT_TURN_TABLES = ("agent_turn_runs", "agent_turn_events")
USAGE_PLANES = ("modeling_material", "invocation_input", "generated_output")


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
    tables = ", ".join(AGENT_TURN_TABLES)
    grantee_clause = "TO" if action == "GRANT" else "FROM"
    return sa.text(
        f"""DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            {action} SELECT, INSERT, UPDATE, DELETE ON TABLE {tables}
              {grantee_clause} ontology_app;
          END IF;
        END
        $$"""
    )


def _add_usage_planes() -> None:
    for table_name in ("data_assets", "logical_datasets"):
        op.add_column(
            table_name,
            sa.Column(
                "usage_plane",
                sa.String(length=30),
                server_default="generated_output",
                nullable=False,
            ),
        )
        op.create_check_constraint(
            f"ck_{table_name}_usage_plane",
            table_name,
            "usage_plane IN ('modeling_material', 'invocation_input', "
            "'generated_output')",
        )


def _backfill_usage_planes() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Only provenance already enforced by a persisted relationship may promote
    # legacy rows into the modeling plane. Ambiguous rows remain generated
    # output and are consequently invisible to modeling APIs until governed.
    op.execute(
        sa.text(
            """
            UPDATE data_assets AS asset
               SET usage_plane = 'invocation_input'
             WHERE asset.labels->>'catalog_purpose' IN (
                       'validation_asset', 'invocation_attachment'
                   );

            UPDATE data_assets AS asset
               SET usage_plane = 'modeling_material'
             WHERE asset.usage_plane = 'generated_output'
               AND EXISTS (
                   SELECT 1
                     FROM data_asset_versions AS version
                     JOIN data_sources AS source
                       ON source.id = version.bucket_data_source_id
                      AND source.tenant_id = version.tenant_id
                    WHERE version.asset_id = asset.id
                      AND version.tenant_id = asset.tenant_id
                      AND source.resource_scope = 'modeling'
               );

            UPDATE logical_datasets AS dataset
               SET usage_plane = 'invocation_input'
             WHERE dataset.labels->>'catalog_purpose' = 'validation_dataset';

            UPDATE logical_datasets AS dataset
               SET usage_plane = 'modeling_material'
             WHERE dataset.usage_plane = 'generated_output'
               AND (
                   EXISTS (
                       SELECT 1
                         FROM scenario_dataset_bindings AS binding
                        WHERE binding.dataset_id = dataset.id
                          AND binding.tenant_id = dataset.tenant_id
                          AND binding.role = 'modeling_evidence'
                   )
                   OR EXISTS (
                       SELECT 1
                         FROM scenario_capability_ports AS port
                        WHERE port.dataset_id = dataset.id
                          AND port.tenant_id = dataset.tenant_id
                          AND port.role = 'modeling_evidence'
                   )
               );
            """
        )
    )


def _create_agent_turn_tables() -> None:
    op.create_unique_constraint("uq_users_id_tenant", "users", ["id", "tenant_id"])
    op.create_unique_constraint("uq_agents_id_tenant", "agents", ["id", "tenant_id"])
    op.create_table(
        "agent_turn_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("conversation_id", sa.String(length=32), nullable=True),
        sa.Column("user_message_id", sa.String(length=32), nullable=True),
        sa.Column("assistant_message_id", sa.String(length=32), nullable=True),
        sa.Column("preparation_run_id", sa.String(length=32), nullable=True),
        sa.Column("parent_run_id", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("request_summary", _json_document_type(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("deployment_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("data_context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result_document", _json_document_type(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('accepted', 'preparing_inputs', 'validating_contracts', "
            "'planning', 'invoking_tools', 'responding', 'cancel_requested', "
            "'succeeded', 'failed', 'cancelled', 'indeterminate')",
            name="ck_agent_turn_runs_status",
        ),
        sa.CheckConstraint(
            "environment IN ('dev', 'staging', 'prod')",
            name="ck_agent_turn_runs_environment",
        ),
        sa.CheckConstraint("revision > 0", name="ck_agent_turn_runs_revision"),
        sa.CheckConstraint(
            "lease_generation >= 0", name="ck_agent_turn_runs_lease_generation"
        ),
        sa.CheckConstraint(
            _sha256_check("request_fingerprint"),
            name="ck_agent_turn_runs_request_fingerprint",
        ),
        sa.CheckConstraint(
            _sha256_check("request_digest"),
            name="ck_agent_turn_runs_request_digest",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["preparation_run_id"], ["ingestion_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["agent_turn_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_agent_turn_runs_user_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["agents.id", "agents.tenant_id"],
            name="fk_agent_turn_runs_agent_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["preparation_run_id", "tenant_id"],
            ["ingestion_runs.id", "ingestion_runs.tenant_id"],
            name="fk_agent_turn_runs_preparation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_turn_runs.id", "agent_turn_runs.tenant_id"],
            name="fk_agent_turn_runs_parent_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "requested_by_user_id",
            "idempotency_key",
            name="uq_agent_turn_runs_principal_request",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_agent_turn_runs_id_tenant"),
    )
    op.create_index(
        "ix_agent_turn_runs_dispatch",
        "agent_turn_runs",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_agent_turn_runs_owner_created",
        "agent_turn_runs",
        ["tenant_id", "requested_by_user_id", "created_at"],
    )
    for column in (
        "tenant_id",
        "requested_by_user_id",
        "agent_id",
        "conversation_id",
        "user_message_id",
        "assistant_message_id",
        "preparation_run_id",
        "parent_run_id",
        "status",
    ):
        op.create_index(f"ix_agent_turn_runs_{column}", "agent_turn_runs", [column])

    op.create_table(
        "agent_turn_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("data", _json_document_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_agent_turn_events_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_turn_runs.id", "agent_turn_runs.tenant_id"],
            name="fk_agent_turn_events_run_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "revision", name="uq_agent_turn_events_revision"
        ),
    )
    op.create_index(
        "ix_agent_turn_events_run_revision",
        "agent_turn_events",
        ["run_id", "revision"],
    )
    op.create_index(
        "ix_agent_turn_events_tenant_id",
        "agent_turn_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_agent_turn_events_run_id", "agent_turn_events", ["run_id"]
    )


def upgrade() -> None:
    _add_usage_planes()
    _backfill_usage_planes()
    _create_agent_turn_tables()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))
    op.drop_table("agent_turn_events")
    op.drop_table("agent_turn_runs")
    op.drop_constraint("uq_agents_id_tenant", "agents", type_="unique")
    op.drop_constraint("uq_users_id_tenant", "users", type_="unique")
    for table_name in ("logical_datasets", "data_assets"):
        op.drop_constraint(
            f"ck_{table_name}_usage_plane", table_name, type_="check"
        )
        op.drop_column(table_name, "usage_plane")
