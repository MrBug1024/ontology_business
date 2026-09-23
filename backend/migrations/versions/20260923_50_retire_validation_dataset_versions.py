"""Retire Agent-owned validation dataset versions through a narrow DB function.

Revision ID: 20260923_50
Revises: 20260922_49
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260923_50"
down_revision = "20260922_49"
branch_labels = None
depends_on = None


_FUNCTION_SIGNATURE = (
    "public.retire_validation_dataset_versions(varchar, varchar, varchar[])"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION public.retire_validation_dataset_versions(
                p_tenant_id varchar(32),
                p_agent_id varchar(32),
                p_dataset_ids varchar[]
            )
            RETURNS bigint
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $function$
            DECLARE
                retired_count bigint;
            BEGIN
                IF p_tenant_id IS NULL OR p_agent_id IS NULL OR p_dataset_ids IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'invalid_parameter_value',
                        MESSAGE = 'tenant, Agent and dataset scope are required';
                END IF;

                IF EXISTS (
                    SELECT 1
                      FROM unnest(p_dataset_ids) AS requested(id)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM public.logical_datasets AS dataset
                          WHERE dataset.id = requested.id
                            AND dataset.tenant_id = p_tenant_id
                            AND dataset.usage_plane = 'invocation_input'
                            AND dataset.labels ->> 'catalog_purpose' = 'validation_dataset'
                            AND dataset.labels ->> 'owner_agent_id' = p_agent_id
                     )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'foreign_key_violation',
                        MESSAGE = 'validation dataset ownership scope is invalid';
                END IF;

                UPDATE public.dataset_versions AS version
                   SET status = 'retired'
                 WHERE version.tenant_id = p_tenant_id
                   AND version.dataset_id = ANY(p_dataset_ids)
                   AND version.status <> 'retired';
                GET DIAGNOSTICS retired_count = ROW_COUNT;
                RETURN retired_count;
            END
            $function$;

            REVOKE ALL ON FUNCTION
                public.retire_validation_dataset_versions(varchar, varchar, varchar[])
                FROM PUBLIC;
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                GRANT EXECUTE ON FUNCTION
                  public.retire_validation_dataset_versions(varchar, varchar, varchar[])
                  TO ontology_app;
              END IF;
            END
            $$
            """
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC;"
        )
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION_SIGNATURE}"))
