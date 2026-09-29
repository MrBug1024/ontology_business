"""Persist the actor that requested document parsing and indexing."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260928_52"
down_revision = "20260928_51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_index_jobs",
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_index_jobs_requested_by_user",
        "document_index_jobs",
        "users",
        ["requested_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_document_index_jobs_requested_by_user_id",
        "document_index_jobs",
        ["requested_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260928_52 downgrade is refused: preserve durable document task actor audit"
    )
