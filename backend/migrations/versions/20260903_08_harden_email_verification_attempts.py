"""persist email verification guess limits

Revision ID: 20260903_08
Revises: 20260903_07
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260903_08"
down_revision: Union[str, None] = "20260903_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_verification_codes",
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "email_verification_codes",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("email_verification_codes", "failed_attempts", server_default=None)


def downgrade() -> None:
    op.drop_column("email_verification_codes", "locked_until")
    op.drop_column("email_verification_codes", "failed_attempts")
