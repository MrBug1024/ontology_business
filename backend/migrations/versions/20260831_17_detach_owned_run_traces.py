"""detach platform run traces before deleting owned file-bucket payloads

Revision ID: 20260831_17
Revises: 20260831_16
Create Date: 2026-08-31

Ingestion and derivation runs may keep a trace pointer to a modeling file.
Those pointers are platform audit metadata, not remote database data.  The
same controlled deletion function also removes local evidence that would
otherwise protect a document chunk or dataset fragment from deletion.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_17"
down_revision: Union[str, None] = "20260831_16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FUNCTION_SIGNATURE = (
    "public.detach_data_source_file_references(varchar, varchar, varchar[])"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION public.detach_data_source_file_references(
                p_source_id varchar(32),
                p_tenant_id varchar(32),
                p_file_ids varchar[]
            )
            RETURNS TABLE (
                asset_versions_detached bigint,
                dataset_fragments_deleted bigint,
                manifest_versions_detached bigint
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $function$
            DECLARE
                selected_source_id varchar(32);
                detached_assets bigint := 0;
                deleted_fragments bigint := 0;
                detached_manifests bigint := 0;
            BEGIN
                SELECT source.id
                  INTO selected_source_id
                  FROM public.data_sources AS source
                 WHERE source.id = p_source_id
                   AND source.tenant_id = p_tenant_id
                   AND source.type = 'file_bucket'
                 FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        MESSAGE = 'file bucket does not belong to the requested tenant';
                END IF;

                -- Trace pointers are platform-owned audit metadata.  Clear
                -- them before their protected BucketFile rows are removed.
                UPDATE public.ingestion_runs AS run
                   SET trace_bucket_file_id = NULL,
                       trace_data_source_id = NULL
                 WHERE run.trace_data_source_id = selected_source_id
                   AND run.trace_bucket_file_id IS NOT NULL
                   AND (p_file_ids IS NULL OR run.trace_bucket_file_id = ANY(p_file_ids));

                UPDATE public.derivation_runs AS run
                   SET trace_bucket_file_id = NULL,
                       trace_data_source_id = NULL
                 WHERE run.trace_data_source_id = selected_source_id
                   AND run.trace_bucket_file_id IS NOT NULL
                   AND (p_file_ids IS NULL OR run.trace_bucket_file_id = ANY(p_file_ids));

                UPDATE public.data_asset_versions AS version
                   SET status = 'retired',
                       bucket_file_id = NULL,
                       bucket_data_source_id = NULL,
                       source_locator = '{}'::jsonb
                 WHERE version.bucket_data_source_id = selected_source_id
                   AND version.bucket_file_id IN (
                       SELECT file.id
                         FROM public.bucket_files AS file
                        WHERE file.data_source_id = selected_source_id
                          AND (p_file_ids IS NULL OR file.id = ANY(p_file_ids))
                   );
                GET DIAGNOSTICS detached_assets = ROW_COUNT;

                UPDATE public.dataset_versions AS version
                   SET status = 'retired',
                       manifest_bucket_file_id = NULL,
                       manifest_data_source_id = NULL
                 WHERE version.manifest_data_source_id = selected_source_id
                   AND version.manifest_bucket_file_id IS NOT NULL
                   AND (p_file_ids IS NULL OR version.manifest_bucket_file_id = ANY(p_file_ids));
                GET DIAGNOSTICS detached_manifests = ROW_COUNT;

                UPDATE public.dataset_versions AS version
                   SET status = 'retired'
                 WHERE version.id IN (
                       SELECT fragment.dataset_version_id
                         FROM public.dataset_fragments AS fragment
                        WHERE fragment.bucket_data_source_id = selected_source_id
                          AND (p_file_ids IS NULL OR fragment.bucket_file_id = ANY(p_file_ids))
                    );

                -- Evidence is derived from these local fragments/chunks.  It
                -- cannot remain pointing at a row that this deletion removes.
                DELETE FROM public.derivation_evidence AS evidence
                 WHERE evidence.dataset_fragment_id IN (
                           SELECT fragment.id
                             FROM public.dataset_fragments AS fragment
                            WHERE fragment.bucket_data_source_id = selected_source_id
                              AND (p_file_ids IS NULL OR fragment.bucket_file_id = ANY(p_file_ids))
                       )
                    OR evidence.document_chunk_id IN (
                           SELECT chunk.id
                             FROM public.document_chunks AS chunk
                            WHERE chunk.data_source_id = selected_source_id
                              AND (p_file_ids IS NULL OR chunk.bucket_file_id = ANY(p_file_ids))
                       );

                DELETE FROM public.dataset_fragments AS fragment
                 WHERE fragment.bucket_data_source_id = selected_source_id
                   AND (p_file_ids IS NULL OR fragment.bucket_file_id = ANY(p_file_ids));
                GET DIAGNOSTICS deleted_fragments = ROW_COUNT;

                RETURN QUERY SELECT
                    detached_assets,
                    deleted_fragments,
                    detached_manifests;
            END
            $function$;

            REVOKE ALL ON FUNCTION
                public.detach_data_source_file_references(varchar, varchar, varchar[])
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
                  public.detach_data_source_file_references(varchar, varchar, varchar[])
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
    # Revision 16 introduced this function. Reversing revision 17 must restore
    # the v16 body; revision 16 remains responsible for dropping it.
    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION public.detach_data_source_file_references(
                p_source_id varchar(32),
                p_tenant_id varchar(32),
                p_file_ids varchar[]
            )
            RETURNS TABLE (
                asset_versions_detached bigint,
                dataset_fragments_deleted bigint,
                manifest_versions_detached bigint
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $function$
            DECLARE
                selected_source_id varchar(32);
                detached_assets bigint := 0;
                deleted_fragments bigint := 0;
                detached_manifests bigint := 0;
            BEGIN
                SELECT source.id
                  INTO selected_source_id
                  FROM public.data_sources AS source
                 WHERE source.id = p_source_id
                   AND source.tenant_id = p_tenant_id
                   AND source.type = 'file_bucket'
                 FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        MESSAGE = 'file bucket does not belong to the requested tenant';
                END IF;

                UPDATE public.data_asset_versions AS version
                   SET status = 'retired',
                       bucket_file_id = NULL,
                       bucket_data_source_id = NULL,
                       source_locator = '{}'::jsonb
                 WHERE version.bucket_data_source_id = selected_source_id
                   AND version.bucket_file_id IN (
                       SELECT file.id
                         FROM public.bucket_files AS file
                        WHERE file.data_source_id = selected_source_id
                          AND (p_file_ids IS NULL OR file.id = ANY(p_file_ids))
                   );
                GET DIAGNOSTICS detached_assets = ROW_COUNT;

                UPDATE public.dataset_versions AS version
                   SET status = 'retired',
                       manifest_bucket_file_id = NULL,
                       manifest_data_source_id = NULL
                 WHERE version.manifest_data_source_id = selected_source_id
                   AND version.manifest_bucket_file_id IS NOT NULL
                   AND (p_file_ids IS NULL OR version.manifest_bucket_file_id = ANY(p_file_ids));
                GET DIAGNOSTICS detached_manifests = ROW_COUNT;

                UPDATE public.dataset_versions AS version
                   SET status = 'retired'
                 WHERE version.id IN (
                       SELECT fragment.dataset_version_id
                         FROM public.dataset_fragments AS fragment
                        WHERE fragment.bucket_data_source_id = selected_source_id
                          AND (p_file_ids IS NULL OR fragment.bucket_file_id = ANY(p_file_ids))
                    );

                DELETE FROM public.dataset_fragments AS fragment
                 WHERE fragment.bucket_data_source_id = selected_source_id
                   AND (p_file_ids IS NULL OR fragment.bucket_file_id = ANY(p_file_ids));
                GET DIAGNOSTICS deleted_fragments = ROW_COUNT;

                RETURN QUERY SELECT
                    detached_assets,
                    deleted_fragments,
                    detached_manifests;
            END
            $function$;
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    )
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE}
                  TO ontology_app;
              END IF;
            END
            $$
            """
        )
    )
