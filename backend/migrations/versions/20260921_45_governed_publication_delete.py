"""Keep publication deletion behind a tenant-bound security-definer function.

Revision ID: 20260921_45
Revises: 20260921_44
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260921_45"
down_revision = "20260921_44"
branch_labels = None
depends_on = None


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
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE DELETE ON TABLE distillation_publications FROM ontology_app;
            REVOKE UPDATE (data_source_id) ON TABLE distillation_publications FROM ontology_app;
          END IF;
        END $$;
    """))
    op.execute(sa.text(DELETE_PUBLICATION_FUNCTION))
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            GRANT EXECUTE ON FUNCTION delete_distillation_publication(varchar, varchar) TO ontology_app;
          END IF;
        END $$;
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE EXECUTE ON FUNCTION delete_distillation_publication(varchar, varchar) FROM ontology_app;
            GRANT DELETE ON TABLE distillation_publications TO ontology_app;
            GRANT UPDATE (data_source_id) ON TABLE distillation_publications TO ontology_app;
          END IF;
        END $$;
    """))
    op.execute(sa.text("DROP FUNCTION IF EXISTS delete_distillation_publication(varchar, varchar)"))
