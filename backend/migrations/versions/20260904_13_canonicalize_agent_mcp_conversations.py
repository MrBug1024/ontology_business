"""canonicalize Agent MCP business-conversation mappings

Revision ID: 20260904_13
Revises: 20260904_12
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_13"
down_revision: Union[str, None] = "20260904_12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _canonicalize_statement() -> sa.TextClause:
    return sa.text(
        """WITH ranked AS (
          SELECT id,
                 ROW_NUMBER() OVER (
                   PARTITION BY service_id, conversation_id
                   ORDER BY created_at ASC, id ASC
                 ) AS mapping_rank
          FROM agent_mcp_conversations
          WHERE conversation_id IS NOT NULL
        )
        UPDATE agent_mcp_conversations AS mapping
        SET legacy_conversation_id = mapping.conversation_id,
            conversation_id = NULL,
            binding_kind = 'legacy_duplicate',
            turn_lease_token = '',
            turn_lease_expires_at = NULL,
            turn_lease_deadline_at = NULL
        FROM ranked
        WHERE mapping.id = ranked.id
          AND ranked.mapping_rank > 1"""
    )


def _restore_legacy_conversations_statement() -> sa.TextClause:
    return sa.text(
        """UPDATE agent_mcp_conversations
        SET conversation_id = legacy_conversation_id
        WHERE binding_kind = 'legacy_duplicate'
          AND conversation_id IS NULL
          AND legacy_conversation_id IS NOT NULL"""
    )


def _fail_interrupted_duplicate_invocations_statement() -> sa.TextClause:
    return sa.text(
        """WITH ranked AS (
          SELECT id,
                 ROW_NUMBER() OVER (
                   PARTITION BY service_id, conversation_id
                   ORDER BY created_at ASC, id ASC
                 ) AS mapping_rank
          FROM agent_mcp_conversations
          WHERE conversation_id IS NOT NULL
        )
        UPDATE agent_mcp_invocations AS invocation
        SET status = 'failed',
            error_code = 'AgentMCPMigrationInterrupted',
            error_message = '历史重复 MCP 会话映射已迁移，请重试本轮请求',
            completed_at = CURRENT_TIMESTAMP
        FROM ranked
        WHERE invocation.mcp_conversation_id = ranked.id
          AND ranked.mapping_rank > 1
          AND invocation.status = 'running'"""
    )


def upgrade() -> None:
    # This migration changes the identity rules used by running workers. Hold
    # the table for the full transactional migration, and deploy only after all
    # workers running the previous release have been drained.
    op.execute(
        sa.text(
            "LOCK TABLE agent_mcp_conversations IN ACCESS EXCLUSIVE MODE"
        )
    )
    op.add_column(
        "agent_mcp_conversations",
        sa.Column(
            "binding_kind",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_transport",
        ),
    )
    op.add_column(
        "agent_mcp_conversations",
        sa.Column("legacy_conversation_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_mcp_conversations_legacy_conversation",
        "agent_mcp_conversations",
        "conversations",
        ["legacy_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Older releases keyed mappings by MCP transport session and could leave
    # several lease rows pointing at one transcript. Keep those rows for their
    # invocation audit FKs, but retain only one canonical conversation owner.
    op.execute(_fail_interrupted_duplicate_invocations_statement())
    op.execute(_canonicalize_statement())
    # Retain an explicit marker for any out-of-band legacy insert. This default
    # is audit provenance, not compatibility with previous-release workers.
    op.create_unique_constraint(
        "uq_agent_mcp_conversations_service_conversation",
        "agent_mcp_conversations",
        ["service_id", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_agent_mcp_conversations_service_conversation",
        "agent_mcp_conversations",
        type_="unique",
    )
    op.execute(_restore_legacy_conversations_statement())
    op.drop_constraint(
        "fk_agent_mcp_conversations_legacy_conversation",
        "agent_mcp_conversations",
        type_="foreignkey",
    )
    op.drop_column("agent_mcp_conversations", "legacy_conversation_id")
    op.drop_column("agent_mcp_conversations", "binding_kind")
