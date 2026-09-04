"""preserve Agent MCP invocation audit when publications are removed

Revision ID: 20260904_14
Revises: 20260904_13
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_14"
down_revision: Union[str, None] = "20260904_13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_mcp_services",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_mcp_services_deleted_at",
        "agent_mcp_services",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_mcp_services_deleted_at",
        table_name="agent_mcp_services",
    )
    op.drop_column("agent_mcp_services", "deleted_at")
