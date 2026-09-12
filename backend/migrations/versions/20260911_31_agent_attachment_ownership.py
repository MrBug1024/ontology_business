"""Scope uploaded assets and upload runs to their owning validation Agent.

Revision ID: 20260911_31
Revises: 20260909_30
"""
from alembic import op
import sqlalchemy as sa


revision = "20260911_31"
down_revision = "20260909_30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy rows remain NULL and therefore retain their existing tenant-level
    # lifecycle. New Agent uploads are populated by the application after the
    # Agent and tenant have been authorized.
    op.add_column(
        "data_assets",
        sa.Column("owner_agent_id", sa.String(32), nullable=True),
    )
    op.create_foreign_key(
        "fk_data_assets_owner_agent_tenant",
        "data_assets",
        "agents",
        ["owner_agent_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_data_assets_owner_agent_id",
        "data_assets",
        ["owner_agent_id"],
    )

    op.add_column(
        "managed_upload_runs",
        sa.Column("owner_agent_id", sa.String(32), nullable=True),
    )
    op.create_foreign_key(
        "fk_managed_upload_runs_owner_agent_tenant",
        "managed_upload_runs",
        "agents",
        ["owner_agent_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_managed_upload_runs_owner_agent_id",
        "managed_upload_runs",
        ["owner_agent_id"],
    )


def downgrade() -> None:
    # The owner columns carry the only durable Agent boundary for uploaded
    # assets/runs.  Dropping them would silently merge private Agent data into
    # the tenant-wide namespace, so require an explicit operator cleanup first.
    bind = op.get_bind()
    for table_name in ("managed_upload_runs", "data_assets"):
        owned = bind.execute(
            sa.text(
                f"SELECT id FROM {table_name} "
                "WHERE owner_agent_id IS NOT NULL ORDER BY id LIMIT 1"
            )
        ).first()
        if owned is not None:
            raise RuntimeError(
                "20260911_31 downgrade requires every Agent-owned row to be "
                f"explicitly detached from {table_name}; refusing to discard "
                "ownership metadata"
            )

    op.drop_index("ix_managed_upload_runs_owner_agent_id", table_name="managed_upload_runs")
    op.drop_constraint(
        "fk_managed_upload_runs_owner_agent_tenant",
        "managed_upload_runs",
        type_="foreignkey",
    )
    op.drop_column("managed_upload_runs", "owner_agent_id")
    op.drop_index("ix_data_assets_owner_agent_id", table_name="data_assets")
    op.drop_constraint(
        "fk_data_assets_owner_agent_tenant",
        "data_assets",
        type_="foreignkey",
    )
    op.drop_column("data_assets", "owner_agent_id")
