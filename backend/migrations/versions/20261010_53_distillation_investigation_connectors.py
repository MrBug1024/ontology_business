"""Investigation connector sessions for member-machine browser execution.

Revision ID: 20261010_53
Revises: 20260928_52
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20261010_53"
down_revision = "20260928_52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "distillation_investigation_connector_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_key", sa.String(64), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("connector_info", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(200), nullable=True),
        sa.CheckConstraint("status IN ('pending','connected','revoked','expired')", name="ck_distillation_connector_status"),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_connector_project_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_distillation_connector_active",
        "distillation_investigation_connector_sessions",
        ["project_id", "target_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','connected')"),
    )


def downgrade() -> None:
    op.drop_index("uq_distillation_connector_active", table_name="distillation_investigation_connector_sessions")
    op.drop_table("distillation_investigation_connector_sessions")
