"""Separate encrypted, scoped research access from model-visible documents."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260919_40"
down_revision = "20260918_38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("distillation_system_access",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("target_key", sa.String(64), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("credential_envelope", postgresql.JSONB(), nullable=False),
        sa.Column("authorization_basis", sa.String(4000), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_system_access_project", ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "project_revision", name="uq_distillation_system_access_revision"),
        sa.CheckConstraint("auth_type IN ('basic','bearer','browser')", name="ck_distillation_system_access_auth"),
    )
    op.create_index("ix_distillation_system_access_target", "distillation_system_access", ["project_id", "target_key", "project_revision"])
    op.execute("""REVOKE ALL ON TABLE distillation_system_access FROM PUBLIC;
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
          GRANT SELECT, INSERT ON TABLE distillation_system_access TO ontology_app;
          GRANT UPDATE (revoked_at) ON TABLE distillation_system_access TO ontology_app;
        END IF; END $$;""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM distillation_system_access")):
        raise RuntimeError("System authorization audit exists; archive it before downgrading")
    op.drop_table("distillation_system_access")
