"""Allow temporary conversation attachments before a project exists.

Revision ID: 20260922_49
Revises: 20260921_48
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260922_49"
down_revision = "20260921_48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("distillation_attachments", "project_id", nullable=True)
    op.drop_constraint("uq_distillation_attachment_request", "distillation_attachments", type_="unique")
    op.create_unique_constraint(
        "uq_distillation_attachment_request",
        "distillation_attachments",
        ["tenant_id", "created_by", "request_id"],
    )
    op.create_index(
        "ix_distillation_attachments_owner_status",
        "distillation_attachments",
        ["tenant_id", "created_by", "status", "expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text(
        "SELECT 1 FROM distillation_attachments WHERE project_id IS NULL LIMIT 1"
    )):
        raise RuntimeError("Unbound conversation attachments exist; bind or remove them before downgrading")
    op.drop_index("ix_distillation_attachments_owner_status", table_name="distillation_attachments")
    op.drop_constraint("uq_distillation_attachment_request", "distillation_attachments", type_="unique")
    op.create_unique_constraint(
        "uq_distillation_attachment_request",
        "distillation_attachments",
        ["project_id", "created_by", "request_id"],
    )
    op.alter_column("distillation_attachments", "project_id", nullable=False)
