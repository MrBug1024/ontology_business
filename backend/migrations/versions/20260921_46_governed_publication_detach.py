"""Allow governed detachment of publication projections.

Revision ID: 20260921_46
Revises: 20260921_45
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260921_46"
down_revision = "20260921_45"
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
  IF OLD.project_id IS NOT NULL
     AND NEW.project_id IS NULL
     AND OLD.id = NEW.id
     AND OLD.tenant_id = NEW.tenant_id
     AND OLD.data_source_id IS NOT DISTINCT FROM NEW.data_source_id
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


DETACH_PUBLICATION_FUNCTION = """
CREATE OR REPLACE FUNCTION detach_distillation_publication(
    p_publication_id varchar,
    p_tenant_id varchar,
    p_detach_kind varchar
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    publication_source_id varchar;
    publication_project_id varchar;
BEGIN
    SELECT publication.data_source_id, publication.project_id
      INTO publication_source_id, publication_project_id
      FROM distillation_publications AS publication
     WHERE publication.id = p_publication_id
       AND publication.tenant_id = p_tenant_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF p_detach_kind = 'data_source' THEN
        IF publication_source_id IS NOT NULL THEN
            UPDATE distillation_publications
               SET data_source_id = NULL
             WHERE id = p_publication_id
               AND tenant_id = p_tenant_id;
        END IF;
    ELSIF p_detach_kind = 'project' THEN
        IF publication_project_id IS NOT NULL THEN
            UPDATE distillation_publications
               SET project_id = NULL
             WHERE id = p_publication_id
               AND tenant_id = p_tenant_id;
        END IF;
    ELSE
        RAISE EXCEPTION 'Unsupported business distillation detachment kind'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION detach_distillation_publication(varchar, varchar, varchar) FROM PUBLIC;
"""


DELETE_PUBLICATION_FUNCTION = """
CREATE OR REPLACE FUNCTION delete_distillation_publication(
    p_publication_id varchar,
    p_tenant_id varchar
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    publication_source_id varchar;
BEGIN
    SELECT publication.data_source_id
      INTO publication_source_id
      FROM distillation_publications AS publication
     WHERE publication.id = p_publication_id
       AND publication.tenant_id = p_tenant_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF publication_source_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1
              FROM data_sources AS source
             WHERE source.id = publication_source_id
               AND source.tenant_id = p_tenant_id
               AND source.type = 'distillation'
               AND source.config ->> 'publication_id' = p_publication_id
        ) THEN
            RAISE EXCEPTION 'Business distillation publication projection is invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM bucket_files
             WHERE data_source_id = publication_source_id
        ) THEN
            RAISE EXCEPTION 'Business distillation publication has file projections'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        UPDATE distillation_publications
           SET data_source_id = NULL
         WHERE id = p_publication_id
           AND tenant_id = p_tenant_id;
        DELETE FROM data_sources
         WHERE id = publication_source_id
           AND tenant_id = p_tenant_id;
    END IF;

    DELETE FROM distillation_publications
     WHERE id = p_publication_id
       AND tenant_id = p_tenant_id;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION delete_distillation_publication(varchar, varchar) FROM PUBLIC;
"""


LEGACY_DELETE_PUBLICATION_FUNCTION = """
CREATE OR REPLACE FUNCTION delete_distillation_publication(
    p_publication_id varchar,
    p_tenant_id varchar
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    publication_source_id varchar;
BEGIN
    SELECT publication.data_source_id
      INTO publication_source_id
      FROM distillation_publications AS publication
     WHERE publication.id = p_publication_id
       AND publication.tenant_id = p_tenant_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF publication_source_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1
              FROM data_sources AS source
             WHERE source.id = publication_source_id
               AND source.tenant_id = p_tenant_id
               AND source.type = 'distillation'
               AND source.config ->> 'publication_id' = p_publication_id
        ) THEN
            RAISE EXCEPTION 'Business distillation publication projection is invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM bucket_files
             WHERE data_source_id = publication_source_id
        ) THEN
            RAISE EXCEPTION 'Business distillation publication has file projections'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        DELETE FROM data_sources
         WHERE id = publication_source_id
           AND tenant_id = p_tenant_id;
    END IF;

    DELETE FROM distillation_publications
     WHERE id = p_publication_id
       AND tenant_id = p_tenant_id;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION delete_distillation_publication(varchar, varchar) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(sa.text(DELETE_PUBLICATION_FUNCTION))
    op.execute(sa.text(PUBLICATION_MUTATION_FUNCTION))
    op.execute(sa.text(DETACH_PUBLICATION_FUNCTION))
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            GRANT EXECUTE ON FUNCTION detach_distillation_publication(varchar, varchar, varchar)
              TO ontology_app;
          END IF;
        END $$;
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE EXECUTE ON FUNCTION detach_distillation_publication(varchar, varchar, varchar)
              FROM ontology_app;
          END IF;
        END $$;
    """))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS detach_distillation_publication(varchar, varchar, varchar)"
    ))
    op.execute(sa.text(LEGACY_DELETE_PUBLICATION_FUNCTION))
    op.execute(sa.text("""
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
    """))
