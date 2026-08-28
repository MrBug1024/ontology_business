"""publish configured Agents over authenticated MCP

Revision ID: 20260828_05
Revises: 20260827_04
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_05"
down_revision: Union[str, None] = "20260827_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_mcp_services",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("execution_user_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("name_key", sa.String(length=240), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=20), nullable=False),
        sa.Column("token_hint", sa.String(length=8), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_config_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_snapshot_id", sa.String(length=32), nullable=True),
        sa.Column("release_id", sa.String(length=32), nullable=True),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_environment", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name_key", name="uq_agent_mcp_services_tenant_name"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_agent_mcp_services_agent_enabled", "agent_mcp_services", ["agent_id", "enabled"])
    op.create_index("ix_agent_mcp_services_tenant_enabled", "agent_mcp_services", ["tenant_id", "enabled"])
    op.create_index(op.f("ix_agent_mcp_services_agent_id"), "agent_mcp_services", ["agent_id"])
    op.create_index(op.f("ix_agent_mcp_services_created_by_user_id"), "agent_mcp_services", ["created_by_user_id"])
    op.create_index(op.f("ix_agent_mcp_services_execution_user_id"), "agent_mcp_services", ["execution_user_id"])
    op.create_index(op.f("ix_agent_mcp_services_expires_at"), "agent_mcp_services", ["expires_at"])
    op.create_index(op.f("ix_agent_mcp_services_tenant_id"), "agent_mcp_services", ["tenant_id"])
    op.create_index(op.f("ix_agent_mcp_services_token_hash"), "agent_mcp_services", ["token_hash"], unique=True)

    op.create_table(
        "agent_mcp_invocations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("service_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("execution_user_id", sa.String(length=32), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["service_id"], ["agent_mcp_services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_mcp_invocations_service_created", "agent_mcp_invocations", ["service_id", "created_at"])
    op.create_index("ix_agent_mcp_invocations_tenant_created", "agent_mcp_invocations", ["tenant_id", "created_at"])
    for column in (
        "agent_id", "conversation_id", "execution_user_id", "request_id", "service_id", "tenant_id"
    ):
        op.create_index(op.f(f"ix_agent_mcp_invocations_{column}"), "agent_mcp_invocations", [column])


def downgrade() -> None:
    for column in (
        "tenant_id", "service_id", "request_id", "execution_user_id", "conversation_id", "agent_id"
    ):
        op.drop_index(op.f(f"ix_agent_mcp_invocations_{column}"), table_name="agent_mcp_invocations")
    op.drop_index("ix_agent_mcp_invocations_tenant_created", table_name="agent_mcp_invocations")
    op.drop_index("ix_agent_mcp_invocations_service_created", table_name="agent_mcp_invocations")
    op.drop_table("agent_mcp_invocations")

    op.drop_index(op.f("ix_agent_mcp_services_token_hash"), table_name="agent_mcp_services")
    op.drop_index(op.f("ix_agent_mcp_services_tenant_id"), table_name="agent_mcp_services")
    op.drop_index(op.f("ix_agent_mcp_services_expires_at"), table_name="agent_mcp_services")
    op.drop_index(op.f("ix_agent_mcp_services_execution_user_id"), table_name="agent_mcp_services")
    op.drop_index(op.f("ix_agent_mcp_services_created_by_user_id"), table_name="agent_mcp_services")
    op.drop_index(op.f("ix_agent_mcp_services_agent_id"), table_name="agent_mcp_services")
    op.drop_index("ix_agent_mcp_services_tenant_enabled", table_name="agent_mcp_services")
    op.drop_index("ix_agent_mcp_services_agent_enabled", table_name="agent_mcp_services")
    op.drop_table("agent_mcp_services")
