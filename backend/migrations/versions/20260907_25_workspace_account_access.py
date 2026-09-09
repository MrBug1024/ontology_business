"""Separate account governance from workspace membership.

Revision ID: 20260907_25
Revises: 20260905_24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260907_25"
down_revision = "20260905_24"
branch_labels = None
depends_on = None

ACCOUNT_ACTOR_TABLES = ("agent_turn_runs", "assistant_request_runs", "managed_upload_runs")


def upgrade() -> None:
    op.add_column("users", sa.Column("system_role", sa.String(20), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_users_system_role", "users", "system_role IN ('user', 'superadmin')")
    op.create_check_constraint("ck_users_status", "users", "status IN ('pending', 'active', 'disabled')")
    op.add_column("organization_members", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_unique_constraint("uq_org_roles_id_org", "organization_roles", ["id", "organization_id"])
    op.create_foreign_key("fk_org_member_role_org", "organization_members", "organization_roles",
                          ["role_id", "organization_id"], ["id", "organization_id"], ondelete="RESTRICT")
    op.add_column("auth_sessions", sa.Column("active_tenant_id", sa.String(32), nullable=True))
    op.create_foreign_key("fk_auth_sessions_active_tenant", "auth_sessions", "tenants", ["active_tenant_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_auth_sessions_active_tenant_id", "auth_sessions", ["active_tenant_id"])
    # Explicit v1 -> v2 session cutover: no undomained token fallback.
    op.execute("DELETE FROM auth_sessions")
    op.execute("DELETE FROM email_verification_codes")
    # Accounts are global identities. Resource/parent composite tenant FKs stay
    # unchanged; request and worker authorization require a live membership.
    for table in ACCOUNT_ACTOR_TABLES:
        op.drop_constraint(f"fk_{table}_user_tenant", table, type_="foreignkey")
        op.create_foreign_key(f"fk_{table}_user", table, "users", ["requested_by_user_id"], ["id"])
    op.create_table("access_governance_guard",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("bootstrap_completed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("INSERT INTO access_governance_guard (id, bootstrap_completed) VALUES (1, false)")
    op.create_table("workspace_invitations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.String(32), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("invited_by_user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_status", sa.String(20), nullable=False),
        sa.Column("delivery_generation", sa.Integer(), nullable=False),
        sa.Column("delivery_lease", sa.String(32)),
        sa.Column("delivery_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "email", name="uq_workspace_invitation_email"),
        sa.ForeignKeyConstraint(["role_id", "organization_id"], ["organization_roles.id", "organization_roles.organization_id"], name="fk_workspace_invitation_role_org", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'declined', 'revoked')", name="ck_workspace_invitation_status"),
        sa.CheckConstraint("delivery_status IN ('queued', 'sending', 'sent', 'failed', 'indeterminate', 'cancelled')", name="ck_workspace_invitation_delivery"))
    op.create_index("ix_workspace_invitation_recipient", "workspace_invitations", ["email", "status", "expires_at"])
    op.create_index("ix_workspace_invitation_delivery", "workspace_invitations", ["delivery_status", "created_at"])
    op.create_table("access_audit_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id", ondelete="RESTRICT")),
        sa.Column("actor_user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_id", sa.String(32), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("before_value", sa.String(100), nullable=False),
        sa.Column("after_value", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_access_audit_scope_time", "access_audit_events", ["tenant_id", "created_at"])
    op.create_table("auth_rate_limits",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_auth_rate_limits_expires_at", "auth_rate_limits", ["expires_at"])
    from app.config import get_settings
    role = get_settings().postgresql_user.strip()
    if not role:
        raise RuntimeError("POSTGRESQL_USER must identify the access runtime role")
    quoted = op.get_bind().dialect.identifier_preparer.quote(role)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.workspace_invitations, public.auth_rate_limits TO {quoted}")
    op.execute(f"REVOKE ALL ON public.access_governance_guard, public.access_audit_events FROM {quoted}")
    op.execute(f"GRANT SELECT, UPDATE ON public.access_governance_guard TO {quoted}")
    op.execute(f"GRANT SELECT, INSERT ON public.access_audit_events TO {quoted}")


def downgrade() -> None:
    bind = op.get_bind()
    for table in ACCOUNT_ACTOR_TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} r JOIN users u ON u.id=r.requested_by_user_id WHERE r.tenant_id <> u.tenant_id LIMIT 1")).first():
            raise RuntimeError("Access downgrade cannot discard cross-workspace execution attribution")
    for table in ("workspace_invitations", "access_audit_events"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Access governance downgrade requires an audited export and removal of invitations/audit history")
    if bind.execute(sa.text("SELECT 1 FROM users WHERE system_role <> 'user' LIMIT 1")).first():
        raise RuntimeError("Access governance downgrade requires removal of system role assignments")
    if bind.execute(sa.text("SELECT 1 FROM organization_members m JOIN organizations o ON o.id=m.organization_id JOIN users u ON u.id=m.user_id WHERE o.tenant_id <> u.tenant_id LIMIT 1")).first():
        raise RuntimeError("Access governance downgrade cannot discard cross-workspace memberships")
    for table in ("auth_rate_limits", "access_audit_events", "workspace_invitations", "access_governance_guard"):
        op.drop_table(table)
    for table in ACCOUNT_ACTOR_TABLES:
        op.drop_constraint(f"fk_{table}_user", table, type_="foreignkey")
        op.create_foreign_key(f"fk_{table}_user_tenant", table, "users", ["requested_by_user_id", "tenant_id"], ["id", "tenant_id"])
    op.drop_index("ix_auth_sessions_active_tenant_id", table_name="auth_sessions")
    op.drop_constraint("fk_auth_sessions_active_tenant", "auth_sessions", type_="foreignkey")
    op.drop_column("auth_sessions", "active_tenant_id")
    op.drop_constraint("fk_org_member_role_org", "organization_members", type_="foreignkey")
    op.drop_constraint("uq_org_roles_id_org", "organization_roles", type_="unique")
    op.drop_column("organization_members", "revision")
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    op.drop_constraint("ck_users_status", "users", type_="check")
    for name in ("last_login_at", "revision", "system_role"):
        op.drop_column("users", name)
