"""bind Agent-originated workflow runs to their private conversations

Revision ID: 20260903_11
Revises: 20260903_10
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_11"
down_revision: Union[str, None] = "20260903_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("agent_conversation_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflow_runs_agent_conversation",
        "workflow_runs",
        "conversations",
        ["agent_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workflow_runs_agent_conversation_id",
        "workflow_runs",
        ["agent_conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_runs_agent_conversation_id",
        table_name="workflow_runs",
    )
    op.drop_constraint(
        "fk_workflow_runs_agent_conversation",
        "workflow_runs",
        type_="foreignkey",
    )
    op.drop_column("workflow_runs", "agent_conversation_id")
