"""add capability runtime contracts and invocation input bindings

Revision ID: 20260829_07
Revises: 20260828_06
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260829_07"
down_revision: Union[str, None] = "20260828_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CAPABILITY_RUNTIME_TABLES = (
    "scenario_capability_ports",
    "capability_invocations",
    "run_input_bindings",
)

SCENARIO_DATASET_BINDING_ROLES = (
    "input",
    "modeling_evidence",
    "test_fixture",
    "invocation_input",
    "reference",
    "rules",
    "output",
)


def _json_document_type(*, nullable_value: bool = False):
    return sa.JSON(none_as_null=nullable_value).with_variant(
        postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
        "postgresql",
    )


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _runtime_role_statement(action: str) -> sa.TextClause:
    if action not in {"GRANT", "REVOKE"}:
        raise ValueError("unsupported privilege action")
    tables = ", ".join(CAPABILITY_RUNTIME_TABLES)
    grantee_clause = "TO" if action == "GRANT" else "FROM"
    return sa.text(
        f"""DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            {action} SELECT, INSERT, UPDATE, DELETE ON TABLE {tables} {grantee_clause} ontology_app;
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "runtime_binding_mode",
            sa.String(length=20),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_agents_runtime_binding_mode",
        "agents",
        "runtime_binding_mode IN ('legacy', 'shadow', 'prefer_capability', "
        "'capability_only')",
    )

    op.add_column(
        "scenario_dataset_bindings",
        sa.Column(
            "environment",
            sa.String(length=20),
            server_default="dev",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "uq_scenario_dataset_binding_key",
        "scenario_dataset_bindings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_dataset_binding_key",
        "scenario_dataset_bindings",
        ["scenario_id", "environment", "binding_key"],
    )
    op.drop_constraint(
        "ck_scenario_dataset_bindings_role",
        "scenario_dataset_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scenario_dataset_bindings_role",
        "scenario_dataset_bindings",
        "role IN ('input', 'modeling_evidence', 'test_fixture', "
        "'invocation_input', 'reference', 'rules', 'output')",
    )
    op.create_check_constraint(
        "ck_scenario_dataset_bindings_environment",
        "scenario_dataset_bindings",
        "environment IN ('dev', 'staging', 'prod')",
    )
    op.create_index(
        "ix_scenario_dataset_bindings_scenario_environment",
        "scenario_dataset_bindings",
        ["scenario_id", "environment", "role", "status"],
    )

    op.create_unique_constraint(
        "uq_connector_bindings_id_scope",
        "connector_bindings",
        ["id", "tenant_id", "scenario_id"],
    )

    op.create_table(
        "scenario_capability_ports",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("port_key", sa.String(length=180), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("media_kind", sa.String(length=20), nullable=False),
        sa.Column("dataset_id", sa.String(length=32), nullable=True),
        sa.Column("dataset_schema_id", sa.String(length=32), nullable=True),
        sa.Column("schema_document", _json_document_type(), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("cardinality", sa.String(length=20), nullable=False),
        sa.Column("binding_policy", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config", _json_document_type(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('input', 'output')",
            name="ck_scenario_capability_ports_direction",
        ),
        sa.CheckConstraint(
            "role IN ('modeling_evidence', 'test_fixture', 'invocation_input', "
            "'reference', 'rules', 'output')",
            name="ck_scenario_capability_ports_role",
        ),
        sa.CheckConstraint(
            "media_kind IN ('message', 'structured', 'document', 'dataset', "
            "'connector', 'artifact')",
            name="ck_scenario_capability_ports_media_kind",
        ),
        sa.CheckConstraint(
            "cardinality IN ('one', 'many')",
            name="ck_scenario_capability_ports_cardinality",
        ),
        sa.CheckConstraint(
            "binding_policy IN ('per_invocation', 'scenario_default', "
            "'release_pinned', 'none')",
            name="ck_scenario_capability_ports_binding_policy",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_scenario_capability_ports_status",
        ),
        sa.CheckConstraint(
            "(dataset_id IS NULL AND dataset_schema_id IS NULL) OR "
            "(dataset_id IS NOT NULL AND dataset_schema_id IS NOT NULL)",
            name="ck_scenario_capability_ports_dataset_contract",
        ),
        sa.CheckConstraint(
            "(direction = 'output' AND role = 'output') OR "
            "(direction = 'input' AND role <> 'output')",
            name="ck_scenario_capability_ports_direction_role",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_schemas.id",
                "dataset_schemas.dataset_id",
                "dataset_schemas.tenant_id",
            ],
            name="fk_scenario_capability_ports_schema_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["business_scenarios.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_scenario_capability_ports_scenario_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scenario_id", "port_key", name="uq_scenario_capability_ports_key"
        ),
        sa.UniqueConstraint(
            "id", "tenant_id", "scenario_id",
            name="uq_scenario_capability_ports_id_scope",
        ),
    )
    for column in (
        "created_by_user_id",
        "dataset_id",
        "dataset_schema_id",
        "scenario_id",
        "tenant_id",
    ):
        op.create_index(
            op.f(f"ix_scenario_capability_ports_{column}"),
            "scenario_capability_ports",
            [column],
        )
    op.create_index(
        "ix_scenario_capability_ports_scenario_role",
        "scenario_capability_ports",
        ["scenario_id", "direction", "role", "status"],
    )

    op.create_table(
        "capability_invocations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("release_id", sa.String(length=32), nullable=True),
        sa.Column("definition_snapshot_id", sa.String(length=32), nullable=True),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("capability_kind", sa.String(length=40), nullable=False),
        sa.Column("capability_key", sa.String(length=240), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("deployment_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("data_context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=240), nullable=False),
        sa.Column("principal_type", sa.String(length=40), nullable=False),
        sa.Column("principal_id", sa.String(length=240), nullable=False),
        sa.Column("invocation_source", sa.String(length=20), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=180), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("request_document", _json_document_type(), nullable=False),
        sa.Column("result_document", _json_document_type(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment IN ('dev', 'staging', 'prod')",
            name="ck_capability_invocations_environment",
        ),
        sa.CheckConstraint(
            "invocation_source IN ('internal', 'agent', 'rest', 'mcp')",
            name="ck_capability_invocations_source",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_confirmation', "
            "'succeeded', 'failed', 'cancelled', 'rejected', 'timed_out')",
            name="ck_capability_invocations_status",
        ),
        sa.CheckConstraint(
            "length(capability_kind) BETWEEN 1 AND 40 "
            "AND capability_kind = lower(capability_kind) "
            "AND length(capability_key) BETWEEN 1 AND 240",
            name="ck_capability_invocations_capability_identity",
        ),
        sa.CheckConstraint(
            "length(correlation_id) BETWEEN 1 AND 240",
            name="ck_capability_invocations_correlation",
        ),
        sa.CheckConstraint(
            "length(principal_type) BETWEEN 1 AND 40 "
            "AND principal_type = lower(principal_type) "
            "AND length(principal_id) BETWEEN 1 AND 240",
            name="ck_capability_invocations_principal",
        ),
        sa.CheckConstraint(
            "(release_id IS NULL AND definition_snapshot_id IS NULL) OR "
            "(release_id IS NOT NULL AND definition_snapshot_id IS NOT NULL)",
            name="ck_capability_invocations_release_pair",
        ),
        sa.CheckConstraint(
            _sha256_check("input_hash"),
            name="ck_capability_invocations_input_hash",
        ),
        sa.CheckConstraint(
            _sha256_check("definition_hash"),
            name="ck_capability_invocations_definition_hash",
        ),
        sa.CheckConstraint(
            _sha256_check("deployment_fingerprint"),
            name="ck_capability_invocations_deployment_fingerprint",
        ),
        sa.CheckConstraint(
            _sha256_check("data_context_fingerprint"),
            name="ck_capability_invocations_data_context_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["release_id", "tenant_id", "scenario_id", "definition_snapshot_id"],
            [
                "ontology_releases.id",
                "ontology_releases.tenant_id",
                "ontology_releases.scenario_id",
                "ontology_releases.snapshot_id",
            ],
            name="fk_capability_invocations_release_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_capability_invocations_scenario_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "tenant_id", "scenario_id",
            name="uq_capability_invocations_id_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_id",
            name="uq_capability_invocations_request",
        ),
        sa.UniqueConstraint(
            "tenant_id", "scenario_id", "capability_kind", "capability_key",
            "definition_hash", "deployment_fingerprint", "idempotency_key",
            name="uq_capability_invocations_idempotency",
        ),
    )
    for column in (
        "agent_id",
        "definition_snapshot_id",
        "release_id",
        "requested_by_user_id",
        "scenario_id",
        "tenant_id",
    ):
        op.create_index(
            op.f(f"ix_capability_invocations_{column}"),
            "capability_invocations",
            [column],
        )
    op.create_index(
        "ix_capability_invocations_scenario_created",
        "capability_invocations",
        ["scenario_id", "created_at"],
    )
    op.create_index(
        "ix_capability_invocations_dispatch",
        "capability_invocations",
        ["environment", "status", "created_at"],
    )
    op.create_index(
        "ix_capability_invocations_capability_created",
        "capability_invocations",
        [
            "tenant_id", "scenario_id", "capability_kind", "capability_key",
            "created_at",
        ],
    )
    op.create_index(
        "ix_capability_invocations_principal_created",
        "capability_invocations",
        ["tenant_id", "principal_type", "principal_id", "created_at"],
    )
    op.create_index(
        "ix_capability_invocations_correlation",
        "capability_invocations",
        ["tenant_id", "correlation_id"],
    )
    op.create_index(
        "ix_capability_invocations_deployment",
        "capability_invocations",
        ["tenant_id", "deployment_fingerprint", "created_at"],
    )

    op.create_table(
        "run_input_bindings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("invocation_id", sa.String(length=32), nullable=False),
        sa.Column("capability_port_id", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=30), nullable=False),
        sa.Column("inline_document", _json_document_type(nullable_value=True), nullable=True),
        sa.Column("asset_version_id", sa.String(length=32), nullable=True),
        sa.Column("source_dataset_version_id", sa.String(length=32), nullable=True),
        sa.Column("dataset_head_id", sa.String(length=32), nullable=True),
        sa.Column("source_dataset_id", sa.String(length=32), nullable=True),
        sa.Column("connector_binding_id", sa.String(length=32), nullable=True),
        sa.Column("resolved_dataset_version_id", sa.String(length=32), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("binding_document", _json_document_type(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('inline', 'asset_version', 'dataset_version', "
            "'dataset_head', 'connector_binding')",
            name="ck_run_input_bindings_source_kind",
        ),
        sa.CheckConstraint(
            "(CASE WHEN inline_document IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN asset_version_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN source_dataset_version_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN dataset_head_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN connector_binding_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_run_input_bindings_one_source",
        ),
        sa.CheckConstraint(
            "(source_kind = 'inline' AND inline_document IS NOT NULL) OR "
            "(source_kind = 'asset_version' AND asset_version_id IS NOT NULL) OR "
            "(source_kind = 'dataset_version' AND source_dataset_version_id IS NOT NULL) OR "
            "(source_kind = 'dataset_head' AND dataset_head_id IS NOT NULL "
            "AND source_dataset_id IS NOT NULL) OR "
            "(source_kind = 'connector_binding' AND connector_binding_id IS NOT NULL)",
            name="ck_run_input_bindings_source_matches_kind",
        ),
        sa.CheckConstraint(
            "(dataset_head_id IS NULL AND source_dataset_id IS NULL) OR "
            "(dataset_head_id IS NOT NULL AND source_dataset_id IS NOT NULL)",
            name="ck_run_input_bindings_head_pair",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_run_input_bindings_ordinal"
        ),
        sa.CheckConstraint(
            "status IN ('provided', 'resolving', 'ready', 'failed')",
            name="ck_run_input_bindings_status",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id", "tenant_id"],
            ["data_asset_versions.id", "data_asset_versions.tenant_id"],
            name="fk_run_input_bindings_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["capability_port_id", "tenant_id", "scenario_id"],
            [
                "scenario_capability_ports.id",
                "scenario_capability_ports.tenant_id",
                "scenario_capability_ports.scenario_id",
            ],
            name="fk_run_input_bindings_port_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connector_binding_id", "tenant_id", "scenario_id"],
            [
                "connector_bindings.id",
                "connector_bindings.tenant_id",
                "connector_bindings.scenario_id",
            ],
            name="fk_run_input_bindings_connector_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_head_id", "source_dataset_id", "tenant_id"],
            ["dataset_heads.id", "dataset_heads.dataset_id", "dataset_heads.tenant_id"],
            name="fk_run_input_bindings_head_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id", "tenant_id", "scenario_id"],
            [
                "capability_invocations.id",
                "capability_invocations.tenant_id",
                "capability_invocations.scenario_id",
            ],
            name="fk_run_input_bindings_invocation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_run_input_bindings_resolved_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_run_input_bindings_source_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id", "capability_port_id", "ordinal",
            name="uq_run_input_bindings_port_ordinal",
        ),
    )
    for column in (
        "asset_version_id",
        "capability_port_id",
        "connector_binding_id",
        "dataset_head_id",
        "invocation_id",
        "resolved_dataset_version_id",
        "scenario_id",
        "source_dataset_id",
        "source_dataset_version_id",
        "tenant_id",
    ):
        op.create_index(
            op.f(f"ix_run_input_bindings_{column}"),
            "run_input_bindings",
            [column],
        )
    op.create_index(
        "ix_run_input_bindings_invocation_status",
        "run_input_bindings",
        ["invocation_id", "status", "ordinal"],
    )

    op.add_column(
        "agent_mcp_services",
        sa.Column(
            "publication_mode",
            sa.String(length=30),
            server_default="legacy_agent",
            nullable=False,
        ),
    )
    op.add_column(
        "agent_mcp_services",
        sa.Column("capability_release_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_mcp_services_capability_release",
        "agent_mcp_services",
        "ontology_releases",
        ["capability_release_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_agent_mcp_services_publication_mode",
        "agent_mcp_services",
        "publication_mode IN ('legacy_agent', 'capability_release')",
    )
    op.create_check_constraint(
        "ck_agent_mcp_services_capability_target",
        "agent_mcp_services",
        "publication_mode = 'legacy_agent' OR capability_release_id IS NOT NULL",
    )
    op.create_index(
        op.f("ix_agent_mcp_services_capability_release_id"),
        "agent_mcp_services",
        ["capability_release_id"],
    )

    op.add_column(
        "agent_mcp_invocations",
        sa.Column("capability_invocation_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_mcp_invocations_capability_invocation",
        "agent_mcp_invocations",
        "capability_invocations",
        ["capability_invocation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_agent_mcp_invocations_capability_invocation_id"),
        "agent_mcp_invocations",
        ["capability_invocation_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("GRANT"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_runtime_role_statement("REVOKE"))

    op.drop_index(
        op.f("ix_agent_mcp_invocations_capability_invocation_id"),
        table_name="agent_mcp_invocations",
    )
    op.drop_constraint(
        "fk_agent_mcp_invocations_capability_invocation",
        "agent_mcp_invocations",
        type_="foreignkey",
    )
    op.drop_column("agent_mcp_invocations", "capability_invocation_id")

    op.drop_index(
        op.f("ix_agent_mcp_services_capability_release_id"),
        table_name="agent_mcp_services",
    )
    op.drop_constraint(
        "ck_agent_mcp_services_capability_target",
        "agent_mcp_services",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_mcp_services_publication_mode",
        "agent_mcp_services",
        type_="check",
    )
    op.drop_constraint(
        "fk_agent_mcp_services_capability_release",
        "agent_mcp_services",
        type_="foreignkey",
    )
    op.drop_column("agent_mcp_services", "capability_release_id")
    op.drop_column("agent_mcp_services", "publication_mode")

    op.drop_table("run_input_bindings")
    op.drop_table("capability_invocations")
    op.drop_table("scenario_capability_ports")

    op.drop_constraint(
        "uq_connector_bindings_id_scope",
        "connector_bindings",
        type_="unique",
    )

    op.drop_index(
        "ix_scenario_dataset_bindings_scenario_environment",
        table_name="scenario_dataset_bindings",
    )
    op.drop_constraint(
        "ck_scenario_dataset_bindings_environment",
        "scenario_dataset_bindings",
        type_="check",
    )
    op.drop_constraint(
        "ck_scenario_dataset_bindings_role",
        "scenario_dataset_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scenario_dataset_bindings_role",
        "scenario_dataset_bindings",
        "role IN ('input', 'reference', 'rules', 'output')",
    )
    op.drop_constraint(
        "uq_scenario_dataset_binding_key",
        "scenario_dataset_bindings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_dataset_binding_key",
        "scenario_dataset_bindings",
        ["scenario_id", "binding_key"],
    )
    op.drop_column("scenario_dataset_bindings", "environment")

    op.drop_constraint(
        "ck_agents_runtime_binding_mode",
        "agents",
        type_="check",
    )
    op.drop_column("agents", "runtime_binding_mode")
