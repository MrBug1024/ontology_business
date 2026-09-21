"""Allow governed deletion of distillation material projections and products.

Revision ID: 20260921_44
Revises: 20260921_43
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260921_44"
down_revision = "20260921_43"
branch_labels = None
depends_on = None


PUBLICATION_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_distillation_publication_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF OLD.data_source_id IS NOT NULL
     AND NEW.data_source_id IS NULL
     AND OLD.id = NEW.id
     AND OLD.tenant_id = NEW.tenant_id
     AND OLD.project_id IS NOT DISTINCT FROM NEW.project_id
     AND OLD.scenario_id IS NOT DISTINCT FROM NEW.scenario_id
     AND OLD.project_revision = NEW.project_revision
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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "distillation_publications",
        "data_source_id",
        existing_type=sa.String(32),
        nullable=True,
    )
    op.execute(sa.text(PUBLICATION_MUTATION_FUNCTION))
    op.execute(sa.text("DROP TRIGGER IF EXISTS distillation_source_immutable ON data_sources"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS protect_distillation_source()"))
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            GRANT DELETE ON TABLE distillation_publications TO ontology_app;
            GRANT DELETE ON TABLE distillation_scenario_states TO ontology_app;
            GRANT UPDATE (data_source_id) ON TABLE distillation_publications TO ontology_app;
          END IF;
        END $$;
    """))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    bind = op.get_bind()
    if bind.scalar(sa.text("""
        SELECT 1 FROM distillation_publications
        WHERE data_source_id IS NULL OR created_at > '2026-09-21T00:00:00Z'
        LIMIT 1
    """)):
        raise RuntimeError(
            "Distillation publication history must be archived before restoring immutable projections"
        )
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_distillation_publication_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
          RAISE EXCEPTION 'Business distillation publications are immutable';
        END;
        $$;
        REVOKE ALL ON FUNCTION reject_distillation_publication_mutation() FROM PUBLIC;
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION protect_distillation_source() RETURNS trigger
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
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE DELETE ON TABLE distillation_publications FROM ontology_app;
            REVOKE DELETE ON TABLE distillation_scenario_states FROM ontology_app;
            REVOKE UPDATE (data_source_id) ON TABLE distillation_publications FROM ontology_app;
          END IF;
        END $$;
    """))
    op.alter_column(
        "distillation_publications",
        "data_source_id",
        existing_type=sa.String(32),
        nullable=False,
    )
