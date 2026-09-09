"""Remove deployment labels from business state and add manual release lifecycle.

Revision ID: 20260908_27
Revises: 20260907_26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260908_27"
down_revision = "20260907_26"
branch_labels = None
depends_on = None

ENVIRONMENT_TABLES = (
    "data_mapping_refresh_jobs", "agent_turn_runs", "ontology_releases",
    "ontology_rollbacks", "connector_bindings", "workflow_runs", "event_envelopes",
    "action_execution_logs", "dataset_heads", "scenario_dataset_bindings",
    "capability_invocations",
)


def _reject_duplicates(table: str, keys: str, *, where: str = "TRUE") -> None:
    # Identifiers and predicates are migration-owned constants, never request values.
    if op.get_bind().scalar(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {where} "
        f"GROUP BY {keys} HAVING count(*) > 1)"
    )):
        raise RuntimeError(
            f"Business identity conflict in {table}; explicitly reconcile duplicate "
            "logical records before migration. No deployment label is selected automatically."
        )


def _drop_dimension(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints(table):
        if column in constraint["column_names"]:
            op.drop_constraint(constraint["name"], table, type_="unique")
    for constraint in inspector.get_check_constraints(table):
        if column in constraint["sqltext"]:
            op.drop_constraint(constraint["name"], table, type_="check")
    for index in inspector.get_indexes(table):
        if column in index["column_names"] and not index.get("duplicates_constraint"):
            op.drop_index(index["name"], table_name=table)
    op.drop_column(table, column)


def upgrade() -> None:
    op.execute("LOCK TABLE " + ", ".join((*ENVIRONMENT_TABLES, "ontology_instances"))
               + " IN ACCESS EXCLUSIVE MODE")
    _reject_duplicates("dataset_heads", "dataset_id")
    _reject_duplicates("connector_bindings", "scenario_id, binding_key")
    _reject_duplicates("scenario_dataset_bindings", "scenario_id, binding_key")
    _reject_duplicates("ontology_releases", "scenario_id", where="status = 'released'")
    _reject_duplicates("capability_invocations", "tenant_id, scenario_id, capability_kind, capability_key, idempotency_key",
                       where="idempotency_key IS NOT NULL")
    _reject_duplicates("data_mapping_refresh_jobs", "mapping_id", where="active_key IS NOT NULL")
    _reject_duplicates(
        "ontology_instances", "entity_id, source_metadata->>'mapping_id', source_metadata->>'record_key'",
        where="source_metadata->>'mapping_id' IS NOT NULL AND source_metadata->>'record_key' IS NOT NULL",
    )
    from migrations.business_dimension_payloads import migrate_payloads
    migrate_payloads(op.get_bind())
    # Existing caller keys must retain replay identity after removal of their prefix.
    for table, scope, column in (
        ("workflow_runs", "workflow_id", "dedupe_key"),
        ("event_envelopes", "event_id", "dedupe_key"),
        ("action_execution_logs", "scenario_id, target_type, target_id", "idempotency_key"),
    ):
        if op.get_bind().scalar(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {column} ~ '^(dev|staging|prod):sha256:')"
        )):
            raise RuntimeError(f"Legacy hashed caller keys in {table} require explicit replay reconciliation before migration")
        normalized = f"regexp_replace({column}, '^(dev|staging|prod):', '')"
        _reject_duplicates(table, f"{scope}, {normalized}", where=f"{column} IS NOT NULL")
        op.execute(sa.text(f"UPDATE {table} SET {column} = {normalized} WHERE {column} IS NOT NULL"))
    op.execute("UPDATE data_mapping_refresh_jobs SET active_key = mapping_id WHERE active_key IS NOT NULL")
    for table in ENVIRONMENT_TABLES:
        _drop_dimension(table, "environment")
    _drop_dimension("agent_mcp_services", "runtime_environment")
    op.drop_column("data_mappings", "environment_status")
    op.add_column("event_envelopes", sa.Column("created_by_user_id", sa.String(32), nullable=True))
    op.create_foreign_key("fk_event_envelopes_creator", "event_envelopes", "users",
                          ["created_by_user_id"], ["id"], ondelete="RESTRICT")
    op.drop_constraint("uq_capability_invocations_idempotency", "capability_invocations", type_="unique")
    op.create_unique_constraint("uq_capability_invocations_idempotency", "capability_invocations",
                                ["tenant_id", "scenario_id", "capability_kind", "capability_key", "idempotency_key"])
    op.create_unique_constraint("uq_dataset_heads_dataset", "dataset_heads", ["dataset_id"])
    op.create_unique_constraint("uq_connector_bindings_scenario_key", "connector_bindings", ["scenario_id", "binding_key"])
    op.create_unique_constraint("uq_scenario_dataset_binding_key", "scenario_dataset_bindings", ["scenario_id", "binding_key"])
    op.create_index("ix_mapping_refresh_jobs_dispatch", "data_mapping_refresh_jobs", ["status", "available_at"])
    op.create_index("ix_ontology_releases_scenario", "ontology_releases", ["scenario_id", "created_at"])
    op.create_index("ix_connector_bindings_scenario", "connector_bindings", ["scenario_id"])
    op.create_index("ix_scenario_dataset_bindings_scenario", "scenario_dataset_bindings", ["scenario_id", "role", "status"])
    op.create_index("ix_capability_invocations_dispatch", "capability_invocations", ["status", "created_at"])
    op.alter_column("ontology_releases", "branch_id", existing_type=sa.String(32), nullable=True)
    op.add_column("ontology_releases", sa.Column("name", sa.String(160), server_default="", nullable=False))
    op.add_column("ontology_releases", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column("ontology_releases", sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("ontology_releases", sa.Column("retired_at", sa.DateTime(timezone=True)))
    op.add_column("ontology_releases", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    # Preserve existing explicitly published active records; never publish live definitions.
    op.execute("UPDATE ontology_releases SET name = '历史发布', enabled = (status = 'released')")
    op.create_index("uq_ontology_releases_enabled", "ontology_releases", ["scenario_id"], unique=True,
                    postgresql_where=sa.text("enabled"))
    op.create_check_constraint("ck_ontology_releases_revision", "ontology_releases", "revision > 0")
    op.create_check_constraint("ck_ontology_releases_enabled", "ontology_releases",
                               "NOT enabled OR (status = 'released' AND deleted_at IS NULL AND retired_at IS NULL)")
    op.create_check_constraint("ck_ontology_releases_deleted", "ontology_releases", "deleted_at IS NULL OR status = 'retired'")
    op.create_table(
        "release_lifecycle_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("release_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=False),
        sa.Column("snapshot_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("release_id", "revision", name="uq_release_events_revision"),
        sa.ForeignKeyConstraint(
            ["release_id", "tenant_id", "scenario_id", "snapshot_id"],
            ["ontology_releases.id", "ontology_releases.tenant_id", "ontology_releases.scenario_id", "ontology_releases.snapshot_id"],
            name="fk_release_events_release_scope", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_release_lifecycle_events_release_id", "release_lifecycle_events", ["release_id"])
    from app.config import get_settings
    role = get_settings().postgresql_user.strip()
    if not role:
        raise RuntimeError("POSTGRESQL_USER must identify the release runtime role")
    quoted = op.get_bind().dialect.identifier_preparer.quote(role)
    op.execute(f"REVOKE ALL ON public.release_lifecycle_events FROM {quoted}")
    op.execute(f"GRANT SELECT, INSERT ON public.release_lifecycle_events TO {quoted}")


def downgrade() -> None:
    raise RuntimeError(
        "Deployment labels cannot be reconstructed without guessing business ownership. "
        "Restore a pre-migration backup to return to the previous schema."
    )
