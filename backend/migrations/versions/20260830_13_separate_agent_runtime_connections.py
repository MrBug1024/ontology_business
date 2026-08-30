"""separate Agent runtime connections from modeling materials

Revision ID: 20260830_13
Revises: 20260829_12
Create Date: 2026-08-30
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260830_13"
down_revision: Union[str, None] = "20260829_12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "data_sources",
        sa.Column("resource_scope", sa.String(length=20), nullable=False, server_default="modeling"),
    )
    op.add_column(
        "data_sources",
        sa.Column("owner_agent_id", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_data_sources_resource_scope",
        "data_sources",
        "resource_scope IN ('modeling', 'agent_runtime')",
    )
    op.create_foreign_key(
        "fk_data_sources_owner_agent",
        "data_sources",
        "agents",
        ["owner_agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_data_sources_owner_agent_id"),
        "data_sources",
        ["owner_agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_data_sources_resource_scope",
        "data_sources",
        ["tenant_id", "resource_scope"],
        unique=False,
    )
    op.add_column(
        "agents",
        sa.Column(
            "runtime_data_source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "runtime_data_source_ids")
    op.drop_index("ix_data_sources_resource_scope", table_name="data_sources")
    op.drop_index(op.f("ix_data_sources_owner_agent_id"), table_name="data_sources")
    op.drop_constraint("fk_data_sources_owner_agent", "data_sources", type_="foreignkey")
    op.drop_constraint("ck_data_sources_resource_scope", "data_sources", type_="check")
    op.drop_column("data_sources", "owner_agent_id")
    op.drop_column("data_sources", "resource_scope")
