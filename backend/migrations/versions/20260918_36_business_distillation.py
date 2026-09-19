"""Business distillation drafts and immutable PostgreSQL modeling documents.

Revision ID: 20260918_36
Revises: 20260918_35
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260918_36"
down_revision = "20260918_35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("distillation_projects",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("updated_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "tenant_id", name="uq_distillation_projects_tenant"),
        sa.CheckConstraint("revision >= 1", name="ck_distillation_project_revision"),
        sa.ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_project_scenario_tenant", ondelete="RESTRICT"),
    )
    op.create_index("ix_distillation_projects_tenant_id", "distillation_projects", ["tenant_id"])
    op.create_index("ix_distillation_projects_scenario_id", "distillation_projects", ["scenario_id"])
    op.create_table("distillation_publications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("data_source_id", sa.String(32), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "project_revision", name="uq_distillation_publication_revision"),
        sa.UniqueConstraint("data_source_id", name="uq_distillation_publication_source"),
        sa.ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_publication_project_tenant", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["data_source_id", "tenant_id"], ["data_sources.id", "data_sources.tenant_id"],
            name="fk_distillation_publication_source_tenant", ondelete="RESTRICT"),
    )
    op.create_index("ix_distillation_publications_tenant_id", "distillation_publications", ["tenant_id"])
    op.execute("""
        CREATE FUNCTION reject_distillation_publication_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN RAISE EXCEPTION 'Business distillation publications are immutable'; END;
        $$;
        REVOKE ALL ON FUNCTION reject_distillation_publication_mutation() FROM PUBLIC;
        CREATE TRIGGER distillation_publication_immutable BEFORE UPDATE OR DELETE
        ON distillation_publications FOR EACH ROW EXECUTE FUNCTION reject_distillation_publication_mutation();
        CREATE FUNCTION protect_distillation_source() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF OLD.type = 'distillation' THEN
                RAISE EXCEPTION 'Published business distillation sources are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$;
        REVOKE ALL ON FUNCTION protect_distillation_source() FROM PUBLIC;
        CREATE TRIGGER distillation_source_immutable BEFORE UPDATE OR DELETE
        ON data_sources FOR EACH ROW EXECUTE FUNCTION protect_distillation_source();
        REVOKE ALL ON TABLE distillation_projects, distillation_publications FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE ALL ON TABLE distillation_projects, distillation_publications FROM ontology_app;
            GRANT SELECT, INSERT, UPDATE ON TABLE distillation_projects TO ontology_app;
            GRANT SELECT, INSERT ON TABLE distillation_publications TO ontology_app;
          END IF;
        END $$;
    """)


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM distillation_projects"))
    if count:
        raise RuntimeError("Business distillation data exists; export and archive it before downgrading")
    op.execute("DROP TRIGGER distillation_source_immutable ON data_sources")
    op.execute("DROP FUNCTION protect_distillation_source()")
    op.drop_table("distillation_publications")
    op.execute("DROP FUNCTION reject_distillation_publication_mutation()")
    op.drop_table("distillation_projects")
