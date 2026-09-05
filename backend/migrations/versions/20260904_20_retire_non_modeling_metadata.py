"""retire metadata derived from non-modeling catalog planes

Revision ID: 20260904_20
Revises: 20260904_19
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_20"
down_revision: Union[str, None] = "20260904_19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WITHDRAW_REASON = (
    "system_source_isolation_v1: release contains catalog-derived metadata "
    "without immutable modeling provenance"
)


def _withdraw_unproven_catalog_releases() -> None:
    op.execute(
        sa.text(
            f"""
            WITH unproven_catalog_releases AS (
                SELECT rel.id
                  FROM ontology_releases AS rel
                  JOIN ontology_snapshots AS snapshot
                    ON snapshot.id = rel.snapshot_id
                   AND snapshot.tenant_id = rel.tenant_id
                   AND snapshot.scenario_id = rel.scenario_id
                 WHERE rel.status = 'released'
                   AND (
                       -- A non-empty hash proves that this immutable port
                       -- contract was derived from a DatasetSchema. Historical
                       -- snapshots did not persist that dataset's usage plane,
                       -- so no mutable authoring row can prove its provenance.
                       EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements(
                                 CASE
                                   WHEN jsonb_typeof(snapshot.content::jsonb -> 'capability_ports') = 'array'
                                   THEN snapshot.content::jsonb -> 'capability_ports'
                                   ELSE '[]'::jsonb
                                 END
                             ) AS port_document
                            WHERE COALESCE(
                                      BTRIM(port_document ->> 'dataset_schema_hash'),
                                      ''
                                  ) <> ''
                       )
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements(
                                 CASE
                                   WHEN jsonb_typeof(snapshot.content::jsonb -> 'functions') = 'array'
                                   THEN snapshot.content::jsonb -> 'functions'
                                   ELSE '[]'::jsonb
                                 END
                             ) AS function_document
                            WHERE CASE
                                    WHEN function_document #> '{{runtime_config,provider_config,semantic_mapping_ids}}'
                                         IS NULL
                                    THEN FALSE
                                    WHEN jsonb_typeof(
                                             function_document #> '{{runtime_config,provider_config,semantic_mapping_ids}}'
                                         ) = 'array'
                                    THEN jsonb_array_length(
                                             function_document #> '{{runtime_config,provider_config,semantic_mapping_ids}}'
                                         ) > 0
                                    -- A malformed historical dependency is
                                    -- unprovable and therefore also fails closed.
                                    ELSE TRUE
                                  END
                       )
                   )
            )
            UPDATE ontology_releases AS rel
               SET status = 'rolled_back',
                   withdrawn_at = CURRENT_TIMESTAMP,
                   withdrawn_by_user_id = NULL,
                   withdraw_reason = '{WITHDRAW_REASON}'
              FROM unproven_catalog_releases AS unproven
             WHERE rel.id = unproven.id
            """
        )
    )


def _retire_non_modeling_references() -> None:
    op.execute(
        sa.text(
            """
            UPDATE semantic_relation_mappings AS relation_mapping
               SET status = 'retired', updated_at = CURRENT_TIMESTAMP
              FROM logical_datasets AS dataset
             WHERE dataset.id = relation_mapping.dataset_id
               AND dataset.tenant_id = relation_mapping.tenant_id
               AND dataset.usage_plane <> 'modeling_material'
               AND relation_mapping.status <> 'retired'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE semantic_mappings AS mapping
               SET status = 'retired', updated_at = CURRENT_TIMESTAMP
              FROM logical_datasets AS dataset
             WHERE dataset.id = mapping.dataset_id
               AND dataset.tenant_id = mapping.tenant_id
               AND dataset.usage_plane <> 'modeling_material'
               AND mapping.status <> 'retired'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE scenario_capability_ports AS port
               SET status = 'retired', updated_at = CURRENT_TIMESTAMP
              FROM logical_datasets AS dataset
             WHERE dataset.id = port.dataset_id
               AND dataset.tenant_id = port.tenant_id
               AND dataset.usage_plane <> 'modeling_material'
               AND port.status <> 'retired'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE scenario_dataset_bindings AS binding
               SET status = 'disabled', updated_at = CURRENT_TIMESTAMP
              FROM logical_datasets AS dataset
             WHERE dataset.id = binding.dataset_id
               AND dataset.tenant_id = binding.tenant_id
               AND dataset.usage_plane <> 'modeling_material'
               AND binding.role = 'modeling_evidence'
               AND binding.status <> 'disabled'
            """
        )
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Read immutable snapshots before retiring their mutable authoring anchors.
    _withdraw_unproven_catalog_releases()
    _retire_non_modeling_references()


def downgrade() -> None:
    raise RuntimeError(
        "20260904_20 is irreversible: prior metadata eligibility cannot be "
        "reconstructed without violating source isolation"
    )
