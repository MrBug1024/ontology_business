"""Keep business distillation products attached to scenarios, not chats.

Revision ID: 20260921_42
Revises: 20260920_41
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260921_42"
down_revision = "20260920_41"
branch_labels = None
depends_on = None


IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_distillation_publication_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Business distillation publications are immutable';
  END IF;
  IF OLD.project_id IS NOT NULL AND NEW.project_id IS NULL
     AND OLD.tenant_id = NEW.tenant_id
     AND OLD.scenario_id IS NOT DISTINCT FROM NEW.scenario_id
     AND OLD.project_revision = NEW.project_revision
     AND OLD.data_source_id = NEW.data_source_id
     AND OLD.document = NEW.document
     AND OLD.artifacts = NEW.artifacts
     AND OLD.created_by = NEW.created_by
     AND OLD.created_at = NEW.created_at THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'Business distillation publications are immutable';
END;
$$;
REVOKE ALL ON FUNCTION reject_distillation_publication_mutation() FROM PUBLIC;
"""

LEGACY_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_distillation_publication_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN RAISE EXCEPTION 'Business distillation publications are immutable'; END;
$$;
REVOKE ALL ON FUNCTION reject_distillation_publication_mutation() FROM PUBLIC;
"""


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "distillation_scenario_states",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("updated_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scenario_id", "tenant_id", name="uq_distillation_scenario_state"),
        sa.CheckConstraint("revision >= 1", name="ck_distillation_scenario_state_revision"),
        sa.ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_scenario_state_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_distillation_scenario_states_tenant_id",
        "distillation_scenario_states",
        ["tenant_id"],
    )
    bind.execute(sa.text("""
        INSERT INTO distillation_scenario_states
            (id, tenant_id, scenario_id, revision, document, created_by, updated_by, created_at, updated_at)
        SELECT md5('distillation-state:' || project.tenant_id || ':' || project.scenario_id),
               project.tenant_id, project.scenario_id, 1, project.document,
               project.updated_by, project.updated_by, project.updated_at, project.updated_at
          FROM (
              SELECT DISTINCT ON (scenario_id, tenant_id) *
                FROM distillation_projects
               WHERE scenario_id IS NOT NULL
               ORDER BY scenario_id, tenant_id, updated_at DESC, id DESC
          ) AS project
        ON CONFLICT (scenario_id, tenant_id) DO NOTHING
    """))
    op.execute(sa.text("DROP TRIGGER distillation_publication_immutable ON distillation_publications"))
    op.add_column("distillation_publications", sa.Column("scenario_id", sa.String(32), nullable=True))
    op.alter_column("distillation_publications", "project_id", existing_type=sa.String(32), nullable=True)
    bind.execute(sa.text("""
        UPDATE distillation_publications AS publication
           SET scenario_id = project.scenario_id
          FROM distillation_projects AS project
         WHERE publication.project_id = project.id
           AND publication.tenant_id = project.tenant_id
    """))
    op.drop_constraint(
        "fk_distillation_publication_project_tenant",
        "distillation_publications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_distillation_publication_project_tenant",
        "distillation_publications",
        "distillation_projects",
        ["project_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_distillation_publication_scenario_tenant",
        "distillation_publications",
        "business_scenarios",
        ["scenario_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_distillation_publications_scenario_id",
        "distillation_publications",
        ["scenario_id"],
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(sa.text("""
        CREATE TRIGGER distillation_publication_immutable BEFORE UPDATE OR DELETE
        ON distillation_publications FOR EACH ROW EXECUTE FUNCTION reject_distillation_publication_mutation()
    """))
    op.execute(sa.text("""
        REVOKE ALL ON TABLE distillation_scenario_states FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE ALL ON TABLE distillation_scenario_states, distillation_projects FROM ontology_app;
            GRANT SELECT, INSERT, UPDATE ON TABLE distillation_scenario_states TO ontology_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE distillation_projects TO ontology_app;
            GRANT DELETE ON TABLE distillation_turn_attachments, distillation_conversation_turns,
                distillation_attachments, distillation_system_access TO ontology_app;
            GRANT UPDATE (project_id) ON TABLE distillation_publications TO ontology_app;
          END IF;
        END $$
    """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT 1 FROM distillation_scenario_states WHERE revision > 1 LIMIT 1")):
        raise RuntimeError("Scenario distillation state has changed; archive it before downgrading")
    if bind.scalar(sa.text("SELECT 1 FROM distillation_publications WHERE project_id IS NULL LIMIT 1")):
        raise RuntimeError("Publications have outlived their source chats; archive them before downgrading")
    op.alter_column("distillation_publications", "project_id", existing_type=sa.String(32), nullable=False)
    op.execute(sa.text("DROP TRIGGER distillation_publication_immutable ON distillation_publications"))
    op.execute(LEGACY_IMMUTABLE_FUNCTION)
    op.execute(sa.text("""
        CREATE TRIGGER distillation_publication_immutable BEFORE UPDATE OR DELETE
        ON distillation_publications FOR EACH ROW EXECUTE FUNCTION reject_distillation_publication_mutation()
    """))
    op.drop_constraint("fk_distillation_publication_scenario_tenant", "distillation_publications", type_="foreignkey")
    op.drop_constraint("fk_distillation_publication_project_tenant", "distillation_publications", type_="foreignkey")
    op.create_foreign_key(
        "fk_distillation_publication_project_tenant",
        "distillation_publications",
        "distillation_projects",
        ["project_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.drop_index("ix_distillation_publications_scenario_id", table_name="distillation_publications")
    op.drop_column("distillation_publications", "scenario_id")
    op.drop_index("ix_distillation_scenario_states_tenant_id", table_name="distillation_scenario_states")
    op.drop_table("distillation_scenario_states")
