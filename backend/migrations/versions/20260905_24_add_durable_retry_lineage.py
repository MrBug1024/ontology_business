"""add durable retry lineage

Revision ID: 20260905_24
Revises: 20260905_23
Create Date: 2026-09-05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_24"
down_revision: Union[str, None] = "20260905_23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "managed_upload_runs",
        sa.Column("parent_run_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_managed_upload_runs_parent_tenant",
        "managed_upload_runs",
        "managed_upload_runs",
        ["parent_run_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_managed_upload_runs_parent_retry",
        "managed_upload_runs",
        ["tenant_id", "parent_run_id"],
    )
    op.create_index(
        "ix_managed_upload_runs_parent_run_id",
        "managed_upload_runs",
        ["parent_run_id"],
    )

    op.create_unique_constraint(
        "uq_assistant_request_runs_id_tenant",
        "assistant_request_runs",
        ["id", "tenant_id"],
    )
    op.add_column(
        "assistant_request_runs",
        sa.Column("parent_run_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_assistant_request_runs_parent_tenant",
        "assistant_request_runs",
        "assistant_request_runs",
        ["parent_run_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_assistant_request_runs_parent_retry",
        "assistant_request_runs",
        ["tenant_id", "parent_run_id"],
    )
    op.create_index(
        "ix_assistant_request_runs_parent_run_id",
        "assistant_request_runs",
        ["parent_run_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    lineage_tables = (
        "managed_upload_runs",
        "assistant_request_runs",
    )
    for table_name in lineage_tables:
        if bind.execute(
            sa.text(
                f"SELECT 1 FROM {table_name} "
                "WHERE parent_run_id IS NOT NULL LIMIT 1"
            )
        ).first() is not None:
            raise RuntimeError(
                "20260905_24 downgrade requires every durable retry child "
                f"to be removed from {table_name}; retry lineage cannot be discarded"
            )

    op.drop_index(
        "ix_assistant_request_runs_parent_run_id",
        table_name="assistant_request_runs",
    )
    op.drop_constraint(
        "uq_assistant_request_runs_parent_retry",
        "assistant_request_runs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_assistant_request_runs_parent_tenant",
        "assistant_request_runs",
        type_="foreignkey",
    )
    op.drop_column("assistant_request_runs", "parent_run_id")
    op.drop_constraint(
        "uq_assistant_request_runs_id_tenant",
        "assistant_request_runs",
        type_="unique",
    )

    op.drop_index(
        "ix_managed_upload_runs_parent_run_id",
        table_name="managed_upload_runs",
    )
    op.drop_constraint(
        "uq_managed_upload_runs_parent_retry",
        "managed_upload_runs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_managed_upload_runs_parent_tenant",
        "managed_upload_runs",
        type_="foreignkey",
    )
    op.drop_column("managed_upload_runs", "parent_run_id")
