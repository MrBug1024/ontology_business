"""allow explicitly confirmed retired-scenario audit purges

Revision ID: 20260830_15
Revises: 20260830_14
Create Date: 2026-08-30

The application runtime role cannot DELETE immutable reasoning and evidence
rows.  This migration installs one narrowly scoped SECURITY DEFINER function
owned by the migration role so an administrator-confirmed retired-scenario
purge can remove those rows without broadening table privileges.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_15"
down_revision: Union[str, None] = "20260830_14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FUNCTION_SIGNATURE = "public.purge_retired_scenario_audit(varchar, varchar)"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION public.purge_retired_scenario_audit(
                p_scenario_id varchar(32),
                p_tenant_id varchar(32)
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $function$
            DECLARE
                scenario_run_ids varchar(32)[];
                scenario_assertion_ids varchar(32)[];
            BEGIN
                IF p_scenario_id IS NULL OR p_tenant_id IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'invalid_parameter_value',
                        MESSAGE = 'scenario and tenant are required';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                      FROM public.business_scenarios AS scenario
                     WHERE scenario.id = p_scenario_id
                       AND scenario.tenant_id = p_tenant_id
                       AND scenario.status = 'retired'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'foreign_key_violation',
                        MESSAGE = 'only a retired scenario in the requested tenant can be purged';
                END IF;

                SELECT COALESCE(array_agg(run.id), ARRAY[]::varchar(32)[])
                  INTO scenario_run_ids
                  FROM public.derivation_runs AS run
                 WHERE run.scenario_id = p_scenario_id
                   AND run.tenant_id = p_tenant_id;

                -- A run-scoped assertion may have a NULL scenario_id because
                -- it is a global predicate.  It is still part of this run's
                -- audit chain and must be included in the bounded purge.
                SELECT COALESCE(array_agg(assertion.id), ARRAY[]::varchar(32)[])
                  INTO scenario_assertion_ids
                  FROM public.assertions AS assertion
                 WHERE assertion.tenant_id = p_tenant_id
                   AND (
                       assertion.scenario_id = p_scenario_id
                       OR assertion.derivation_run_id = ANY(scenario_run_ids)
                   );

                -- Do not rewrite another scenario's immutable history merely
                -- to make this purge possible.  Such a supersedes edge is a
                -- protected cross-scenario reference and is reported as a
                -- normal foreign-key failure to the API.
                IF EXISTS (
                    SELECT 1
                      FROM public.assertions AS external_assertion
                     WHERE external_assertion.tenant_id = p_tenant_id
                       AND external_assertion.supersedes_assertion_id =
                           ANY(scenario_assertion_ids)
                       AND NOT (external_assertion.id = ANY(scenario_assertion_ids))
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'foreign_key_violation',
                        MESSAGE = 'scenario assertions are referenced by another audit chain';
                END IF;

                DELETE FROM public.derivation_evidence AS evidence
                 WHERE evidence.derivation_run_id = ANY(scenario_run_ids)
                    OR evidence.assertion_id = ANY(scenario_assertion_ids)
                    OR evidence.evidence_assertion_id = ANY(scenario_assertion_ids)
                    OR evidence.action_scenario_id = p_scenario_id;

                DELETE FROM public.derivation_run_inputs AS run_input
                 WHERE run_input.derivation_run_id = ANY(scenario_run_ids)
                   AND run_input.tenant_id = p_tenant_id;

                -- RESTRICT self-links are immediate in PostgreSQL.  Clear
                -- only links owned by the rows being purged; cross-scenario
                -- links were rejected above.
                UPDATE public.assertions AS assertion
                   SET supersedes_assertion_id = NULL
                 WHERE assertion.id = ANY(scenario_assertion_ids)
                   AND assertion.tenant_id = p_tenant_id
                   AND assertion.supersedes_assertion_id IS NOT NULL;

                DELETE FROM public.assertions AS assertion
                 WHERE assertion.id = ANY(scenario_assertion_ids)
                   AND assertion.tenant_id = p_tenant_id;

                DELETE FROM public.reasoning_terms AS term
                 WHERE term.scenario_id = p_scenario_id
                   AND term.tenant_id = p_tenant_id;
            END
            $function$;

            REVOKE ALL ON FUNCTION public.purge_retired_scenario_audit(varchar, varchar)
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
                  public.purge_retired_scenario_audit(varchar, varchar)
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
            """
            DO $$
            BEGIN
              REVOKE EXECUTE ON FUNCTION
                public.purge_retired_scenario_audit(varchar, varchar)
                FROM PUBLIC;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                REVOKE EXECUTE ON FUNCTION
                  public.purge_retired_scenario_audit(varchar, varchar)
                  FROM ontology_app;
              END IF;
            END
            $$
            """
        )
    )
    op.execute(sa.text(f"DROP FUNCTION {_FUNCTION_SIGNATURE}"))
