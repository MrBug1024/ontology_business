"""Purge retired-scenario append-only history through the governed function.

Revision ID: 20260921_48
Revises: 20260921_47
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260921_48"
down_revision = "20260921_47"
branch_labels = None
depends_on = None


PURGE_FUNCTION = """
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

    SELECT COALESCE(array_agg(assertion.id), ARRAY[]::varchar(32)[])
      INTO scenario_assertion_ids
      FROM public.assertions AS assertion
     WHERE assertion.tenant_id = p_tenant_id
       AND (
           assertion.scenario_id = p_scenario_id
           OR assertion.derivation_run_id = ANY(scenario_run_ids)
       );

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

    -- These append-only records are protected from the runtime role.  They
    -- are still owned by the retired scenario and must be removed before the
    -- release or scenario rows can be purged after explicit audit confirmation.
    DELETE FROM public.release_lifecycle_events AS event
     WHERE event.scenario_id = p_scenario_id
       AND event.tenant_id = p_tenant_id;

    DELETE FROM public.workflow_approval_evidence AS evidence
     WHERE evidence.scenario_id = p_scenario_id
       AND evidence.tenant_id = p_tenant_id;

    DELETE FROM public.derivation_evidence AS evidence
     WHERE evidence.tenant_id = p_tenant_id
       AND (
           evidence.derivation_run_id = ANY(scenario_run_ids)
        OR evidence.assertion_id = ANY(scenario_assertion_ids)
        OR evidence.evidence_assertion_id = ANY(scenario_assertion_ids)
        OR evidence.action_scenario_id = p_scenario_id
       );

    DELETE FROM public.derivation_run_inputs AS run_input
     WHERE run_input.derivation_run_id = ANY(scenario_run_ids)
       AND run_input.tenant_id = p_tenant_id;

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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(sa.text(PURGE_FUNCTION))
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
    raise RuntimeError(
        "20260921_48 downgrade is refused: restoring the previous purge "
        "function would reintroduce protected-history deletion failures"
    )
