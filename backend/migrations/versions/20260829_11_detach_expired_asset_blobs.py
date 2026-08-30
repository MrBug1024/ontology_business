"""detach expired temporary asset blobs from retained audit versions

Revision ID: 20260829_11
Revises: 20260829_10
Create Date: 2026-08-29

The logical DataAssetVersion remains an immutable audit identity after its
temporary payload expires.  Only its physical BucketFile pair becomes
nullable, allowing the expiry worker to enqueue exact MinIO deletion without
breaking historical RunInputBinding references.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_11"
down_revision: Union[str, None] = "20260829_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "data_asset_versions",
        "bucket_file_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.alter_column(
        "data_asset_versions",
        "bucket_data_source_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_asset_versions_blob_pair",
        "data_asset_versions",
        "(bucket_file_id IS NULL) = (bucket_data_source_id IS NULL)",
    )
    # ``data_asset_versions`` remains immutable to the application role.  The
    # only permitted mutation is this narrowly checked lifecycle transition;
    # callers cannot select a cutoff or detach a non-temporary payload early.
    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION public.detach_expired_catalog_asset_blob(
                p_version_id varchar(32)
            )
            RETURNS TABLE (
                detached_bucket_file_id varchar(32),
                detached_source_id varchar(32)
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $function$
            DECLARE
                selected_bucket_file_id varchar(32);
                selected_source_id varchar(32);
                selected_expiry_text text;
                selected_expiry timestamptz;
                updated_rows integer;
            BEGIN
                SELECT
                    version.bucket_file_id,
                    version.bucket_data_source_id,
                    version.version_document #>> '{lifecycle,expires_at}'
                INTO
                    selected_bucket_file_id,
                    selected_source_id,
                    selected_expiry_text
                FROM public.data_asset_versions AS version
                WHERE version.id = p_version_id
                  AND version.status = 'ready'
                  AND version.bucket_file_id IS NOT NULL
                  AND version.bucket_data_source_id IS NOT NULL
                  AND version.version_document #>> '{lifecycle,purpose}' =
                      'invocation_attachment'
                  AND version.version_document #>> '{lifecycle,temporary}' = 'true'
                FOR UPDATE;

                IF NOT FOUND OR selected_expiry_text IS NULL THEN
                    RETURN;
                END IF;
                BEGIN
                    selected_expiry := selected_expiry_text::timestamptz;
                EXCEPTION
                    WHEN invalid_datetime_format OR datetime_field_overflow THEN
                        RETURN;
                END;
                IF selected_expiry IS NULL OR selected_expiry > clock_timestamp() THEN
                    RETURN;
                END IF;

                UPDATE public.data_asset_versions AS version
                   SET status = 'retired',
                       bucket_file_id = NULL,
                       bucket_data_source_id = NULL,
                       source_locator = '{}'::jsonb
                 WHERE version.id = p_version_id
                   AND version.status = 'ready'
                   AND version.bucket_file_id = selected_bucket_file_id
                   AND version.bucket_data_source_id = selected_source_id
                   AND version.version_document #>> '{lifecycle,purpose}' =
                       'invocation_attachment'
                   AND version.version_document #>> '{lifecycle,temporary}' = 'true';
                GET DIAGNOSTICS updated_rows = ROW_COUNT;

                IF updated_rows = 1 THEN
                    RETURN QUERY
                    SELECT selected_bucket_file_id, selected_source_id;
                END IF;
            END
            $function$;

            REVOKE ALL ON FUNCTION
                public.detach_expired_catalog_asset_blob(varchar)
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
                  public.detach_expired_catalog_asset_blob(varchar)
                  TO ontology_app;
              END IF;
            END
            $$
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    detached = int(
        bind.scalar(
            sa.text(
                "SELECT COUNT(*) FROM data_asset_versions "
                "WHERE bucket_file_id IS NULL OR bucket_data_source_id IS NULL"
            )
        )
        or 0
    )
    if detached:
        raise RuntimeError(
            "cannot downgrade detached temporary asset blobs: "
            f"{detached} retained logical versions no longer have physical payloads"
        )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                REVOKE EXECUTE ON FUNCTION
                  public.detach_expired_catalog_asset_blob(varchar)
                  FROM ontology_app;
              END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION public.detach_expired_catalog_asset_blob(varchar)"
        )
    )
    op.drop_constraint(
        "ck_asset_versions_blob_pair",
        "data_asset_versions",
        type_="check",
    )
    op.alter_column(
        "data_asset_versions",
        "bucket_data_source_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.alter_column(
        "data_asset_versions",
        "bucket_file_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
