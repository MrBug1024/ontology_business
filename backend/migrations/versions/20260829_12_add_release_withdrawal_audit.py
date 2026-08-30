"""add explicit release withdrawal audit facts

Revision ID: 20260829_12
Revises: 20260829_11
Create Date: 2026-08-29
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_12"
down_revision: Union[str, None] = "20260829_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ontology_releases",
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ontology_releases",
        sa.Column("withdrawn_by_user_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ontology_releases",
        sa.Column(
            "withdraw_reason",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.create_foreign_key(
        "fk_ontology_releases_withdrawn_by_user",
        "ontology_releases",
        "users",
        ["withdrawn_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_ontology_releases_withdrawn_at"),
        "ontology_releases",
        ["withdrawn_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ontology_releases_withdrawn_by_user_id"),
        "ontology_releases",
        ["withdrawn_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ontology_releases_withdrawn_by_user_id"),
        table_name="ontology_releases",
    )
    op.drop_index(
        op.f("ix_ontology_releases_withdrawn_at"),
        table_name="ontology_releases",
    )
    op.drop_constraint(
        "fk_ontology_releases_withdrawn_by_user",
        "ontology_releases",
        type_="foreignkey",
    )
    op.drop_column("ontology_releases", "withdraw_reason")
    op.drop_column("ontology_releases", "withdrawn_by_user_id")
    op.drop_column("ontology_releases", "withdrawn_at")
