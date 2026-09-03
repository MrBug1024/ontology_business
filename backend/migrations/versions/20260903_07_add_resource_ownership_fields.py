"""add tenant resource ownership audit fields

Revision ID: 20260903_07
Revises: 20260828_06
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_07"
down_revision: Union[str, None] = "20260828_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = ("business_scenarios", "data_sources", "agents")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("created_by_user_id", sa.String(length=32), nullable=True))
        op.add_column(table, sa.Column("owner_user_id", sa.String(length=32), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_created_by_user",
            table,
            "users",
            ["created_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            f"fk_{table}_owner_user",
            table,
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table}_created_by_user_id", table, ["created_by_user_id"])
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.drop_index(f"ix_{table}_created_by_user_id", table_name=table)
        op.drop_constraint(f"fk_{table}_owner_user", table, type_="foreignkey")
        op.drop_constraint(f"fk_{table}_created_by_user", table, type_="foreignkey")
        op.drop_column(table, "owner_user_id")
        op.drop_column(table, "created_by_user_id")
