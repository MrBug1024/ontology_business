"""Allow audited removal of historical keys without scenario bindings."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260928_51"
down_revision = "20260923_50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_api_keys", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError(
        "20260928_51 downgrade is refused: preserve the current Alembic head and "
        "API key removal audit state"
    )
