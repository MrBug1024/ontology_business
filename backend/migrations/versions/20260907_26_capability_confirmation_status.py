"""Allow the full capability confirmation status to be persisted.

Revision ID: 20260907_26
Revises: 20260907_25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260907_26"
down_revision = "20260907_25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "capability_invocations",
        "status",
        existing_type=sa.String(20),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Keep the length check and narrowing atomic with concurrent confirmations.
    op.execute("LOCK TABLE capability_invocations IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM capability_invocations WHERE length(status) > 20)"
    )):
        raise RuntimeError(
            "Cannot narrow capability status while long statuses remain; "
            "reconcile pending confirmations before downgrade."
        )
    op.alter_column(
        "capability_invocations",
        "status",
        existing_type=sa.String(32),
        type_=sa.String(20),
        existing_nullable=False,
    )
