"""persist external MCP session to Agent conversation mappings

Revision ID: 20260903_09
Revises: 20260903_08
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_09"
down_revision: Union[str, None] = "20260903_08"
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
            {action} SELECT, INSERT, UPDATE, DELETE ON TABLE agent_mcp_conversations {grantee_clause} ontology_app;
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    op.create_table(
        "agent_mcp_conversations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("service_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("execution_user_id", sa.String(length=32), nullable=True),
        sa.Column("external_session_hash", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["service_id"], ["agent_mcp_services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_id",
            "external_session_hash",
            name="uq_agent_mcp_conversations_service_session",
        ),
    )
    op.create_index(
        "ix_agent_mcp_conversations_service_updated",
        "agent_mcp_conversations",
        ["service_id", "updated_at"],
    )
    op.create_index(
        "ix_agent_mcp_conversations_tenant_updated",
        "agent_mcp_conversations",
        ["tenant_id", "updated_at"],
    )
    for column in (
        "agent_id",
        "conversation_id",
        "execution_user_id",
        "service_id",
        "tenant_id",
    ):
        op.create_index(
            op.f(f"ix_agent_mcp_conversations_{column}"),
            "agent_mcp_conversations",
            [column],
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))
    for column in (
        "tenant_id",
        "service_id",
        "execution_user_id",
        "conversation_id",
        "agent_id",
    ):
        op.drop_index(
            op.f(f"ix_agent_mcp_conversations_{column}"),
            table_name="agent_mcp_conversations",
        )
    op.drop_index("ix_agent_mcp_conversations_tenant_updated", table_name="agent_mcp_conversations")
    op.drop_index("ix_agent_mcp_conversations_service_updated", table_name="agent_mcp_conversations")
    op.drop_table("agent_mcp_conversations")
