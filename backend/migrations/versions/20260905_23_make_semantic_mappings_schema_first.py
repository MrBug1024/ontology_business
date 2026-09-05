"""make semantic mappings schema-first while retaining legacy binding references

Revision ID: 20260905_23
Revises: 20260904_22
Create Date: 2026-09-05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_23"
down_revision: Union[str, None] = "20260904_22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_unambiguous_schema_ownership() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                  SELECT 1
                    FROM semantic_mappings
                   GROUP BY scenario_id, entity_id, dataset_schema_id
                  HAVING COUNT(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'semantic mappings contain duplicate scenario/entity/schema ownership; govern duplicates before upgrading';
              END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    _require_unambiguous_schema_ownership()
    op.drop_constraint(
        "fk_semantic_relations_source_mapping",
        "semantic_relation_mappings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_semantic_relations_target_mapping",
        "semantic_relation_mappings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_semantic_mappings_entity_binding",
        "semantic_mappings",
        type_="unique",
    )
    op.alter_column(
        "semantic_mappings",
        "scenario_dataset_binding_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_semantic_mappings_entity_schema",
        "semantic_mappings",
        ["scenario_id", "entity_id", "dataset_schema_id"],
    )
    op.alter_column(
        "semantic_relation_mappings",
        "scenario_dataset_binding_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_semantic_relations_source_mapping",
        "semantic_relation_mappings",
        "semantic_mappings",
        [
            "source_semantic_mapping_id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "source_dataset_relation_id",
            "source_entity_id",
        ],
        [
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
        ],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_semantic_relations_target_mapping",
        "semantic_relation_mappings",
        "semantic_mappings",
        [
            "target_semantic_mapping_id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "target_dataset_relation_id",
            "target_entity_id",
        ],
        [
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
        ],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM semantic_mappings "
            "WHERE scenario_dataset_binding_id IS NULL LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "20260905_23 downgrade requires every schema-first semantic mapping "
            "to be removed or assigned an explicit legacy binding"
        )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM semantic_relation_mappings "
            "WHERE scenario_dataset_binding_id IS NULL LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "20260905_23 downgrade requires every schema-first semantic relation "
            "mapping to be removed or assigned an explicit legacy binding"
        )
    op.drop_constraint(
        "fk_semantic_relations_source_mapping",
        "semantic_relation_mappings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_semantic_relations_target_mapping",
        "semantic_relation_mappings",
        type_="foreignkey",
    )
    op.alter_column(
        "semantic_relation_mappings",
        "scenario_dataset_binding_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_semantic_relations_source_mapping",
        "semantic_relation_mappings",
        "semantic_mappings",
        [
            "source_semantic_mapping_id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "source_dataset_relation_id",
            "source_entity_id",
            "scenario_dataset_binding_id",
        ],
        [
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
            "scenario_dataset_binding_id",
        ],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_semantic_relations_target_mapping",
        "semantic_relation_mappings",
        "semantic_mappings",
        [
            "target_semantic_mapping_id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "target_dataset_relation_id",
            "target_entity_id",
            "scenario_dataset_binding_id",
        ],
        [
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
            "scenario_dataset_binding_id",
        ],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_semantic_mappings_entity_schema",
        "semantic_mappings",
        type_="unique",
    )
    op.alter_column(
        "semantic_mappings",
        "scenario_dataset_binding_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_semantic_mappings_entity_binding",
        "semantic_mappings",
        ["scenario_id", "entity_id", "scenario_dataset_binding_id"],
    )
