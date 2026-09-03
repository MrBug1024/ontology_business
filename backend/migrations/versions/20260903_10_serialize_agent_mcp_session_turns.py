"""serialize published Agent MCP turns with fenced database leases

Revision ID: 20260903_10
Revises: 20260903_09
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_10"
down_revision: Union[str, None] = "20260903_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _runtime_role_statement(action: str) -> sa.TextClause:
    if action not in {"GRANT", "REVOKE"}:
        raise ValueError("unsupported privilege action")
    grantee_clause = "TO" if action == "GRANT" else "FROM"
    return sa.text(
        f"""DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            {action} SELECT, INSERT, UPDATE, DELETE ON TABLE agent_mcp_conversations, agent_mcp_invocations {grantee_clause} ontology_app;
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    op.add_column(
        "agent_mcp_conversations",
        sa.Column("turn_lease_token", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_mcp_conversations",
        sa.Column("turn_lease_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_mcp_conversations",
        sa.Column("turn_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_mcp_conversations",
        sa.Column("turn_lease_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("agent_mcp_conversations", "turn_lease_token", server_default=None)
    op.alter_column("agent_mcp_conversations", "turn_lease_generation", server_default=None)

    op.add_column(
        "agent_mcp_invocations",
        sa.Column("mcp_conversation_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "agent_mcp_invocations",
        sa.Column("external_request_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_mcp_invocations",
        sa.Column("turn_lease_token", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_mcp_invocations",
        sa.Column("turn_lease_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("agent_mcp_invocations", "turn_lease_token", server_default=None)
    op.alter_column("agent_mcp_invocations", "turn_lease_generation", server_default=None)
    op.create_foreign_key(
        "fk_agent_mcp_invocations_mcp_conversation",
        "agent_mcp_invocations",
        "agent_mcp_conversations",
        ["mcp_conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_mcp_invocations_mcp_conversation_request",
        "agent_mcp_invocations",
        ["mcp_conversation_id", "external_request_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_agent_mcp_invocations_mcp_conversation_id"),
        "agent_mcp_invocations",
        ["mcp_conversation_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))
    op.drop_index(
        op.f("ix_agent_mcp_invocations_mcp_conversation_id"),
        table_name="agent_mcp_invocations",
    )
    op.drop_index(
        "ix_agent_mcp_invocations_mcp_conversation_request",
        table_name="agent_mcp_invocations",
    )
    op.drop_constraint(
        "fk_agent_mcp_invocations_mcp_conversation",
        "agent_mcp_invocations",
        type_="foreignkey",
    )
    op.drop_column("agent_mcp_invocations", "turn_lease_generation")
    op.drop_column("agent_mcp_invocations", "turn_lease_token")
    op.drop_column("agent_mcp_invocations", "external_request_hash")
    op.drop_column("agent_mcp_invocations", "mcp_conversation_id")
    op.drop_column("agent_mcp_conversations", "turn_lease_deadline_at")
    op.drop_column("agent_mcp_conversations", "turn_lease_expires_at")
    op.drop_column("agent_mcp_conversations", "turn_lease_generation")
    op.drop_column("agent_mcp_conversations", "turn_lease_token")
