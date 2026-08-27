"""enforce dataset catalog integrity

Revision ID: 20260827_03
Revises: 20260827_02
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_03"
down_revision: Union[str, None] = "20260827_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


HASH_CONSTRAINTS = (
    ("platform_migration_runs", "ck_platform_runs_plan_sha", "plan_digest"),
    ("platform_migration_runs", "ck_platform_runs_source_sha", "source_fingerprint"),
    ("platform_migration_checkpoints", "ck_platform_checkpoints_payload_sha", "payload_sha256"),
    ("data_asset_versions", "ck_asset_versions_content_sha", "content_sha256"),
    ("dataset_schemas", "ck_dataset_schemas_hash_sha", "schema_hash"),
    ("dataset_versions", "ck_dataset_versions_content_sha", "content_hash"),
    ("dataset_fragments", "ck_dataset_fragments_content_sha", "content_sha256"),
    ("dataset_lineage_edges", "ck_dataset_lineage_transform_sha", "transformation_hash"),
    ("serving_projections", "ck_serving_projections_locator_sha", "locator_hash"),
    ("serving_projections", "ck_serving_projections_schema_sha", "schema_hash"),
    ("reasoning_terms", "ck_reasoning_terms_canonical_sha", "canonical_hash"),
    ("derivation_runs", "ck_derivation_runs_ontology_sha", "ontology_content_hash"),
    ("derivation_runs", "ck_derivation_runs_rules_sha", "rule_set_hash"),
    ("derivation_runs", "ck_derivation_runs_input_sha", "input_fingerprint"),
    ("derivation_run_inputs", "ck_derivation_inputs_content_sha", "content_hash"),
    ("assertions", "ck_assertions_canonical_sha", "canonical_hash"),
    ("derivation_evidence", "ck_derivation_evidence_content_sha", "content_hash"),
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_dataset_schemas_id_dataset", "dataset_schemas", ["id", "dataset_id"]
    )
    op.create_unique_constraint(
        "uq_dataset_relations_id_schema", "dataset_relations", ["id", "schema_id"]
    )
    op.create_unique_constraint(
        "uq_dataset_versions_id_dataset", "dataset_versions", ["id", "dataset_id"]
    )
    op.create_unique_constraint(
        "uq_dataset_versions_id_schema", "dataset_versions", ["id", "schema_id"]
    )
    op.create_unique_constraint(
        "uq_dataset_heads_id_dataset", "dataset_heads", ["id", "dataset_id"]
    )

    op.drop_constraint(
        "dataset_versions_schema_id_fkey", "dataset_versions", type_="foreignkey"
    )
    op.drop_constraint(
        "dataset_versions_parent_version_id_fkey",
        "dataset_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_dataset_versions_schema_dataset",
        "dataset_versions",
        "dataset_schemas",
        ["schema_id", "dataset_id"],
        ["id", "dataset_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_dataset_versions_parent_dataset",
        "dataset_versions",
        "dataset_versions",
        ["parent_version_id", "dataset_id"],
        ["id", "dataset_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "dataset_heads_dataset_version_id_fkey", "dataset_heads", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_dataset_heads_version_dataset",
        "dataset_heads",
        "dataset_versions",
        ["dataset_version_id", "dataset_id"],
        ["id", "dataset_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "scenario_dataset_bindings_dataset_head_id_fkey",
        "scenario_dataset_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "scenario_dataset_bindings_dataset_version_id_fkey",
        "scenario_dataset_bindings",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_scenario_bindings_head_dataset",
        "scenario_dataset_bindings",
        "dataset_heads",
        ["dataset_head_id", "dataset_id"],
        ["id", "dataset_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scenario_bindings_version_dataset",
        "scenario_dataset_bindings",
        "dataset_versions",
        ["dataset_version_id", "dataset_id"],
        ["id", "dataset_id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "dataset_fragments", sa.Column("schema_id", sa.String(length=32), nullable=True)
    )
    op.execute(
        "UPDATE dataset_fragments AS fragment "
        "SET schema_id = relation.schema_id "
        "FROM dataset_relations AS relation "
        "WHERE relation.id = fragment.dataset_relation_id"
    )
    op.alter_column(
        "dataset_fragments", "schema_id", existing_type=sa.String(length=32), nullable=False
    )
    op.create_index(
        "ix_dataset_fragments_schema_id", "dataset_fragments", ["schema_id"], unique=False
    )
    op.drop_constraint(
        "dataset_fragments_dataset_version_id_fkey",
        "dataset_fragments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "dataset_fragments_dataset_relation_id_fkey",
        "dataset_fragments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_dataset_fragments_version_schema",
        "dataset_fragments",
        "dataset_versions",
        ["dataset_version_id", "schema_id"],
        ["id", "schema_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_dataset_fragments_relation_schema",
        "dataset_fragments",
        "dataset_relations",
        ["dataset_relation_id", "schema_id"],
        ["id", "schema_id"],
        ondelete="RESTRICT",
    )

    for table_name, constraint_name, column_name in HASH_CONSTRAINTS:
        op.create_check_constraint(
            constraint_name, table_name, _sha256_check(column_name)
        )


def downgrade() -> None:
    for table_name, constraint_name, _column_name in reversed(HASH_CONSTRAINTS):
        op.drop_constraint(constraint_name, table_name, type_="check")

    op.drop_constraint(
        "fk_dataset_fragments_relation_schema", "dataset_fragments", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_dataset_fragments_version_schema", "dataset_fragments", type_="foreignkey"
    )
    op.create_foreign_key(
        "dataset_fragments_dataset_relation_id_fkey",
        "dataset_fragments",
        "dataset_relations",
        ["dataset_relation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "dataset_fragments_dataset_version_id_fkey",
        "dataset_fragments",
        "dataset_versions",
        ["dataset_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("ix_dataset_fragments_schema_id", table_name="dataset_fragments")
    op.drop_column("dataset_fragments", "schema_id")

    op.drop_constraint(
        "fk_scenario_bindings_version_dataset",
        "scenario_dataset_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scenario_bindings_head_dataset",
        "scenario_dataset_bindings",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "scenario_dataset_bindings_dataset_version_id_fkey",
        "scenario_dataset_bindings",
        "dataset_versions",
        ["dataset_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "scenario_dataset_bindings_dataset_head_id_fkey",
        "scenario_dataset_bindings",
        "dataset_heads",
        ["dataset_head_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "fk_dataset_heads_version_dataset", "dataset_heads", type_="foreignkey"
    )
    op.create_foreign_key(
        "dataset_heads_dataset_version_id_fkey",
        "dataset_heads",
        "dataset_versions",
        ["dataset_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "fk_dataset_versions_parent_dataset", "dataset_versions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_dataset_versions_schema_dataset", "dataset_versions", type_="foreignkey"
    )
    op.create_foreign_key(
        "dataset_versions_parent_version_id_fkey",
        "dataset_versions",
        "dataset_versions",
        ["parent_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "dataset_versions_schema_id_fkey",
        "dataset_versions",
        "dataset_schemas",
        ["schema_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("uq_dataset_heads_id_dataset", "dataset_heads", type_="unique")
    op.drop_constraint("uq_dataset_versions_id_schema", "dataset_versions", type_="unique")
    op.drop_constraint("uq_dataset_versions_id_dataset", "dataset_versions", type_="unique")
    op.drop_constraint("uq_dataset_relations_id_schema", "dataset_relations", type_="unique")
    op.drop_constraint("uq_dataset_schemas_id_dataset", "dataset_schemas", type_="unique")
