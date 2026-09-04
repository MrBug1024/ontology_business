"""add cross-workspace invitations and active session workspace

Revision ID: 20260904_12
Revises: 20260903_11
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_12"
down_revision: Union[str, None] = "20260903_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions",
        sa.Column("active_tenant_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_auth_sessions_active_tenant",
        "auth_sessions",
        "tenants",
        ["active_tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_auth_sessions_active_tenant_id",
        "auth_sessions",
        ["active_tenant_id"],
    )

    op.add_column(
        "organization_members",
        sa.Column("invited_by_user_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_organization_members_invited_by_user",
        "organization_members",
        "users",
        ["invited_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_organization_members_invited_by_user_id",
        "organization_members",
        ["invited_by_user_id"],
    )

    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("invited_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked', 'expired')",
            name="ck_organization_invitations_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["organization_members.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organization_invitations_organization_id",
        "organization_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_invitations_member_id",
        "organization_invitations",
        ["member_id"],
    )
    op.create_index(
        "ix_organization_invitations_user_id",
        "organization_invitations",
        ["user_id"],
    )
    op.create_index(
        "ix_organization_invitations_invited_by_user_id",
        "organization_invitations",
        ["invited_by_user_id"],
    )
    op.create_index(
        "ix_organization_invitations_expires_at",
        "organization_invitations",
        ["expires_at"],
    )
    op.create_index(
        "ix_organization_invitations_user_status",
        "organization_invitations",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_organization_invitations_member_status",
        "organization_invitations",
        ["member_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_invitations_member_status",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_user_status",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_expires_at",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_invited_by_user_id",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_user_id",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_member_id",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_organization_id",
        table_name="organization_invitations",
    )
    op.drop_table("organization_invitations")

    op.drop_index(
        "ix_organization_members_invited_by_user_id",
        table_name="organization_members",
    )
    op.drop_constraint(
        "fk_organization_members_invited_by_user",
        "organization_members",
        type_="foreignkey",
    )
    op.drop_column("organization_members", "invited_by_user_id")

    op.drop_index("ix_auth_sessions_active_tenant_id", table_name="auth_sessions")
    op.drop_constraint(
        "fk_auth_sessions_active_tenant",
        "auth_sessions",
        type_="foreignkey",
    )
    op.drop_column("auth_sessions", "active_tenant_id")
