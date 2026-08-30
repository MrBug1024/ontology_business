from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app import database
from app.external_api_models import AgentMCPInvocation, AgentMCPService
from app.models import (
    Agent,
    Base,
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    RunInputBinding,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    Tenant,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260829_07_add_capability_runtime_contracts.py"
)
AGENT_TURN_AUDIT_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260829_08_add_agent_turn_input_audit.py"
)
WORKFLOW_PAYLOAD_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260829_09_encrypt_workflow_run_inputs.py"
)
CAPABILITY_PORT_SCOPE_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260829_10_scope_capability_ports.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location(
        "capability_runtime_revision", REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _named_constraint(table_name: str, name: str):
    matches = [
        constraint
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name == name
    ]
    assert len(matches) == 1, (table_name, name)
    return matches[0]


def test_revision_07_upgrade_downgrade_cover_all_tables() -> None:
    revision = _load_revision()
    assert revision.revision == "20260829_07"
    assert revision.down_revision == "20260828_06"
    assert revision.CAPABILITY_RUNTIME_TABLES == (
        "scenario_capability_ports",
        "capability_invocations",
        "run_input_bindings",
    )

    with patch.object(revision, "op") as migration_op:
        migration_op.f.side_effect = lambda value: value
        migration_op.get_bind.return_value.dialect.name = "sqlite"
        revision.upgrade()
        assert {
            call.args[0] for call in migration_op.create_table.call_args_list
        } == set(revision.CAPABILITY_RUNTIME_TABLES)

    with patch.object(revision, "op") as migration_op:
        migration_op.f.side_effect = lambda value: value
        migration_op.get_bind.return_value.dialect.name = "sqlite"
        revision.downgrade()
        assert {
            call.args[0] for call in migration_op.drop_table.call_args_list
        } == set(revision.CAPABILITY_RUNTIME_TABLES)

    grant = str(revision._runtime_role_statement("GRANT"))
    assert "TO ontology_app" in grant
    assert all(table_name in grant for table_name in revision.CAPABILITY_RUNTIME_TABLES)


def test_revision_08_chains_and_adds_safe_message_audit_columns() -> None:
    spec = importlib.util.spec_from_file_location(
        "agent_turn_audit_revision", AGENT_TURN_AUDIT_REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260829_08"
    assert revision.down_revision == "20260829_07"

    with patch.object(revision, "op") as migration_op:
        revision.upgrade()
        assert [
            call.args[1].name for call in migration_op.add_column.call_args_list
        ] == ["input_snapshot", "evidence_refs"]
        defaults = [
            str(call.args[1].server_default.arg)
            for call in migration_op.add_column.call_args_list
        ]
        assert defaults == ["'{}'::jsonb", "'[]'::jsonb"]

    with patch.object(revision, "op") as migration_op:
        revision.downgrade()
        assert [
            call.args[1] for call in migration_op.drop_column.call_args_list
        ] == ["evidence_refs", "input_snapshot"]


def test_revision_09_replaces_plaintext_workflow_input_column() -> None:
    spec = importlib.util.spec_from_file_location(
        "workflow_payload_revision", WORKFLOW_PAYLOAD_REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260829_09"
    assert revision.down_revision == "20260829_08"

    with patch.object(revision, "op") as migration_op, patch.object(
        revision, "_encrypt_existing_rows", return_value=0
    ):
        revision.upgrade()
        assert [
            call.args[1].name for call in migration_op.add_column.call_args_list
        ] == ["input_summary", "input_digest"]
        rename = migration_op.alter_column.call_args_list[0]
        assert rename.args[:2] == ("workflow_runs", "input_params")
        assert rename.kwargs["new_column_name"] == "input_payload"

    with patch.object(revision, "op") as migration_op, patch.object(
        revision, "_decrypt_existing_rows", return_value=0
    ):
        revision.downgrade()
        rename = migration_op.alter_column.call_args_list[0]
        assert rename.args[:2] == ("workflow_runs", "input_payload")
        assert rename.kwargs["new_column_name"] == "input_params"
        assert [
            call.args[1] for call in migration_op.drop_column.call_args_list
        ] == ["input_digest", "input_summary"]


def test_revision_10_chains_capability_port_ownership() -> None:
    spec = importlib.util.spec_from_file_location(
        "capability_port_scope_revision", CAPABILITY_PORT_SCOPE_REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260829_10"
    assert revision.down_revision == "20260829_09"

    with patch.object(revision, "op") as migration_op, patch.object(
        revision, "_backfill_ownership"
    ) as backfill:
        revision.upgrade()
        assert [
            call.args[1].name for call in migration_op.add_column.call_args_list
        ] == ["capability_kind", "capability_key"]
        backfill.assert_called_once_with(migration_op.get_bind.return_value)
        migration_op.create_unique_constraint.assert_called_once_with(
            "uq_scenario_capability_ports_owner_key",
            "scenario_capability_ports",
            ["scenario_id", "capability_kind", "capability_key", "port_key"],
        )


def _revision_10_backfill_database():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in ("function_definitions", "ontology_actions", "ontology_workflows"):
            connection.exec_driver_sql(
                f"CREATE TABLE {table} (id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL)"
            )
        connection.exec_driver_sql(
            "CREATE TABLE scenario_model_draft_resources ("
            "scenario_id TEXT NOT NULL, resource_kind TEXT NOT NULL, "
            "resource_key TEXT NOT NULL, draft_status TEXT NOT NULL, "
            "resolved_resource_id TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE scenario_capability_ports ("
            "id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, config TEXT NOT NULL, "
            "capability_kind TEXT, capability_key TEXT)"
        )
    return engine


def _load_revision_10():
    spec = importlib.util.spec_from_file_location(
        "capability_port_scope_backfill_revision",
        CAPABILITY_PORT_SCOPE_REVISION_PATH,
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_revision_10_backfills_only_exact_target_or_resolved_draft_evidence() -> None:
    revision = _load_revision_10()
    engine = _revision_10_backfill_database()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO function_definitions VALUES (?, ?)",
                ("function-direct", "scenario-a"),
            )
            connection.exec_driver_sql(
                "INSERT INTO function_definitions VALUES (?, ?)",
                ("function-resolved", "scenario-a"),
            )
            connection.exec_driver_sql(
                "INSERT INTO scenario_model_draft_resources VALUES (?, ?, ?, ?, ?)",
                (
                    "scenario-a",
                    "function",
                    "semantic.function.key",
                    "resolved",
                    "function-resolved",
                ),
            )
            for port_id, resource_key in (
                ("port-direct", "function-direct"),
                ("port-resolved", "semantic.function.key"),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO scenario_capability_ports VALUES (?, ?, ?, NULL, NULL)",
                    (
                        port_id,
                        "scenario-a",
                        json.dumps(
                            {
                                "contract_source": {
                                    "resource_kind": "function",
                                    "resource_key": resource_key,
                                }
                            }
                        ),
                    ),
                )
            revision._backfill_ownership(connection)
            rows = connection.exec_driver_sql(
                "SELECT id, capability_kind, capability_key "
                "FROM scenario_capability_ports ORDER BY id"
            ).all()
        assert rows == [
            ("port-direct", "function", "function-direct"),
            ("port-resolved", "function", "function-resolved"),
        ]
    finally:
        engine.dispose()


def test_revision_10_backfill_fails_closed_without_partial_updates() -> None:
    revision = _load_revision_10()
    engine = _revision_10_backfill_database()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO function_definitions VALUES (?, ?)",
                ("function-exact", "scenario-a"),
            )
            for target in ("function-a", "function-b"):
                connection.exec_driver_sql(
                    "INSERT INTO function_definitions VALUES (?, ?)",
                    (target, "scenario-a"),
                )
                connection.exec_driver_sql(
                    "INSERT INTO scenario_model_draft_resources VALUES (?, ?, ?, ?, ?)",
                    (
                        "scenario-a",
                        "function",
                        "ambiguous.semantic.key",
                        "resolved",
                        target,
                    ),
                )
            connection.exec_driver_sql(
                "INSERT INTO scenario_capability_ports VALUES (?, ?, ?, NULL, NULL)",
                (
                    "port-exact",
                    "scenario-a",
                    json.dumps(
                        {
                            "contract_source": {
                                "resource_kind": "function",
                                "resource_key": "function-exact",
                            }
                        }
                    ),
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO scenario_capability_ports VALUES (?, ?, ?, NULL, NULL)",
                ("port-unknown", "scenario-a", "{}"),
            )
            connection.exec_driver_sql(
                "INSERT INTO scenario_capability_ports VALUES (?, ?, ?, NULL, NULL)",
                (
                    "port-ambiguous",
                    "scenario-a",
                    json.dumps(
                        {
                            "contract_source": {
                                "resource_kind": "function",
                                "resource_key": "ambiguous.semantic.key",
                            }
                        }
                    ),
                ),
            )
            with pytest.raises(RuntimeError, match="port-unknown") as captured:
                revision._backfill_ownership(connection)
            assert "port-ambiguous (ambiguous governed targets)" in str(captured.value)
            rows = connection.exec_driver_sql(
                "SELECT capability_kind, capability_key "
                "FROM scenario_capability_ports ORDER BY id"
            ).all()
        assert rows == [(None, None), (None, None), (None, None)]
    finally:
        engine.dispose()


def test_capability_runtime_metadata_has_scoped_contracts_and_legacy_defaults() -> None:
    assert Agent.__table__.c.runtime_binding_mode.nullable is False
    assert Agent.__table__.c.runtime_binding_mode.default.arg == "legacy"
    assert Agent.__table__.c.runtime_binding_mode.server_default.arg == "legacy"
    assert ScenarioDatasetBinding.__table__.c.environment.default.arg == "dev"
    assert ScenarioDatasetBinding.__table__.c.environment.server_default.arg == "dev"
    assert AgentMCPService.__table__.c.publication_mode.default.arg == "legacy_agent"
    assert (
        AgentMCPService.__table__.c.publication_mode.server_default.arg
        == "legacy_agent"
    )
    assert AgentMCPService.__table__.c.capability_release_id.nullable is True
    assert AgentMCPInvocation.__table__.c.capability_invocation_id.nullable is True

    port_table = Base.metadata.tables["scenario_capability_ports"]
    assert port_table.c.capability_kind.nullable is False
    assert port_table.c.capability_key.nullable is False
    port_identity = _named_constraint(
        "scenario_capability_ports", "uq_scenario_capability_ports_owner_key"
    )
    assert isinstance(port_identity, UniqueConstraint)
    assert tuple(column.name for column in port_identity.columns) == (
        "scenario_id",
        "capability_kind",
        "capability_key",
        "port_key",
    )
    assert isinstance(
        _named_constraint(
            "scenario_capability_ports",
            "ck_scenario_capability_ports_capability_kind",
        ),
        CheckConstraint,
    )

    mode_check = _named_constraint("agents", "ck_agents_runtime_binding_mode")
    mode_sql = str(mode_check.sqltext)
    for mode in (
        "legacy",
        "shadow",
        "prefer_capability",
        "capability_only",
    ):
        assert f"'{mode}'" in mode_sql

    binding_identity = _named_constraint(
        "scenario_dataset_bindings", "uq_scenario_dataset_binding_key"
    )
    assert isinstance(binding_identity, UniqueConstraint)
    assert tuple(column.name for column in binding_identity.columns) == (
        "scenario_id",
        "environment",
        "binding_key",
    )

    role_check = _named_constraint(
        "scenario_dataset_bindings", "ck_scenario_dataset_bindings_role"
    )
    assert isinstance(role_check, CheckConstraint)
    role_sql = str(role_check.sqltext)
    for role in (
        "input",
        "modeling_evidence",
        "test_fixture",
        "invocation_input",
        "reference",
        "rules",
        "output",
    ):
        assert f"'{role}'" in role_sql

    for table_name, constraint_name in (
        (
            "scenario_capability_ports",
            "fk_scenario_capability_ports_scenario_tenant",
        ),
        ("capability_invocations", "fk_capability_invocations_release_scope"),
        ("run_input_bindings", "fk_run_input_bindings_invocation_scope"),
        ("run_input_bindings", "fk_run_input_bindings_port_scope"),
        ("run_input_bindings", "fk_run_input_bindings_connector_scope"),
    ):
        assert isinstance(
            _named_constraint(table_name, constraint_name), ForeignKeyConstraint
        )

    service_release_fk = _named_constraint(
        "agent_mcp_services", "fk_agent_mcp_services_capability_release"
    )
    assert isinstance(service_release_fk, ForeignKeyConstraint)
    assert service_release_fk.elements[0].column.table.name == "ontology_releases"
    invocation_fk = _named_constraint(
        "agent_mcp_invocations",
        "fk_agent_mcp_invocations_capability_invocation",
    )
    assert isinstance(invocation_fk, ForeignKeyConstraint)
    assert invocation_fk.elements[0].column.table.name == "capability_invocations"

    invocation_table = Base.metadata.tables["capability_invocations"]
    for column_name in (
        "capability_kind",
        "capability_key",
        "definition_hash",
        "deployment_fingerprint",
        "data_context_fingerprint",
        "correlation_id",
        "principal_type",
        "principal_id",
    ):
        assert invocation_table.c[column_name].nullable is False
    idempotency = _named_constraint(
        "capability_invocations", "uq_capability_invocations_idempotency"
    )
    assert isinstance(idempotency, UniqueConstraint)
    assert tuple(column.name for column in idempotency.columns) == (
        "tenant_id",
        "scenario_id",
        "capability_kind",
        "capability_key",
        "definition_hash",
        "deployment_fingerprint",
        "idempotency_key",
    )
    for constraint_name in (
        "ck_capability_invocations_definition_hash",
        "ck_capability_invocations_deployment_fingerprint",
        "ck_capability_invocations_data_context_fingerprint",
        "ck_capability_invocations_input_hash",
    ):
        assert isinstance(
            _named_constraint("capability_invocations", constraint_name),
            CheckConstraint,
        )


def test_capability_runtime_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table_name in (
        "scenario_capability_ports",
        "capability_invocations",
        "run_input_bindings",
        "agent_mcp_services",
        "agent_mcp_invocations",
    ):
        ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect))
        assert "FOREIGN KEY" in ddl
        assert "CHECK" in ddl or table_name == "agent_mcp_invocations"
    run_input_ddl = str(
        CreateTable(Base.metadata.tables["run_input_bindings"]).compile(
            dialect=dialect
        )
    )
    assert "ck_run_input_bindings_one_source" in run_input_ddl
    assert "resolved_dataset_version_id" in run_input_ddl


def test_runtime_binding_sources_and_environment_roles_are_enforced() -> None:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(id="tenant-capability", name="Capability tenant")
        scenario = BusinessScenario(
            id="scenario-capability",
            tenant_id=tenant.id,
            name="Capability scenario",
        )
        agent = Agent(
            id="agent-capability",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Legacy compatible Agent",
        )
        port = ScenarioCapabilityPort(
            id="port-input",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            capability_kind="function",
            capability_key="function-capability",
            port_key="claims",
            name="Claims",
            direction="input",
            role="invocation_input",
        )
        invocation = CapabilityInvocation(
            id="invocation-capability",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            agent_id=agent.id,
            environment="dev",
            capability_kind="provider",
            capability_key="medical-audit",
            definition_hash="d" * 64,
            deployment_fingerprint="e" * 64,
            data_context_fingerprint="f" * 64,
            correlation_id="correlation-capability",
            principal_type="service",
            principal_id="service-capability",
            request_id="request-capability",
            idempotency_key="idempotency-capability",
            input_hash="a" * 64,
        )
        connector = ConnectorBinding(
            id="connector-capability",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            environment="dev",
            binding_key="claims-db",
            connector_kind="data_source",
            connector_id="legacy-source-id",
        )
        dataset = LogicalDataset(
            id="dataset-capability",
            tenant_id=tenant.id,
            key="claims",
            name="Claims",
        )
        dataset_schema = DatasetSchema(
            id="schema-capability",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="b" * 64,
            compatibility="none",
        )
        dataset_version = DatasetVersion(
            id="version-capability",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=dataset_schema.id,
            version_number=1,
            status="ready",
            content_hash="c" * 64,
        )
        db.add(tenant)
        db.commit()
        db.add(scenario)
        db.commit()
        db.add_all([agent, port, connector, dataset])
        db.commit()
        db.add(dataset_schema)
        db.commit()
        db.add_all([invocation, dataset_version])
        db.commit()
        assert agent.runtime_binding_mode == "legacy"

        distinct_capability = CapabilityInvocation(
            id="invocation-distinct-capability",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            environment="dev",
            capability_kind="provider",
            capability_key="project-manager",
            definition_hash="d" * 64,
            deployment_fingerprint="e" * 64,
            data_context_fingerprint="0" * 64,
            correlation_id="correlation-distinct-capability",
            principal_type="service",
            principal_id="service-capability",
            request_id="request-distinct-capability",
            idempotency_key="idempotency-capability",
            input_hash="1" * 64,
        )
        db.add(distinct_capability)
        db.commit()

        duplicate_scope = CapabilityInvocation(
            id="invocation-duplicate-scope",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            environment="dev",
            capability_kind="provider",
            capability_key="medical-audit",
            definition_hash="d" * 64,
            deployment_fingerprint="e" * 64,
            data_context_fingerprint="2" * 64,
            correlation_id="correlation-duplicate-scope",
            principal_type="service",
            principal_id="service-capability",
            request_id="request-duplicate-scope",
            idempotency_key="idempotency-capability",
            input_hash="3" * 64,
        )
        db.add(duplicate_scope)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        roles = (
            "input",
            "modeling_evidence",
            "test_fixture",
            "invocation_input",
            "reference",
            "rules",
            "output",
        )
        for ordinal, role in enumerate(roles):
            db.add(
                ScenarioDatasetBinding(
                    id=f"binding-role-{ordinal}",
                    tenant_id=tenant.id,
                    scenario_id=scenario.id,
                    dataset_id=dataset.id,
                    binding_key=f"role-{role}",
                    role=role,
                    environment="dev",
                    binding_mode="pinned",
                    dataset_version_id=dataset_version.id,
                )
            )
        db.add_all(
            [
                ScenarioDatasetBinding(
                    id="binding-shared-dev",
                    tenant_id=tenant.id,
                    scenario_id=scenario.id,
                    dataset_id=dataset.id,
                    binding_key="shared-key",
                    role="reference",
                    environment="dev",
                    binding_mode="pinned",
                    dataset_version_id=dataset_version.id,
                ),
                ScenarioDatasetBinding(
                    id="binding-shared-prod",
                    tenant_id=tenant.id,
                    scenario_id=scenario.id,
                    dataset_id=dataset.id,
                    binding_key="shared-key",
                    role="reference",
                    environment="prod",
                    binding_mode="pinned",
                    dataset_version_id=dataset_version.id,
                ),
            ]
        )
        db.commit()

        valid = RunInputBinding(
            id="binding-valid",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            invocation_id=invocation.id,
            capability_port_id=port.id,
            source_kind="inline",
            inline_document={"claim_id": "claim-1"},
            resolved_dataset_version_id=dataset_version.id,
        )
        db.add(valid)
        db.commit()
        assert valid.resolved_dataset_version_id == dataset_version.id

        missing_source = RunInputBinding(
            id="binding-missing",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            invocation_id=invocation.id,
            capability_port_id=port.id,
            ordinal=1,
            source_kind="inline",
        )
        db.add(missing_source)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        two_sources = RunInputBinding(
            id="binding-two-sources",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            invocation_id=invocation.id,
            capability_port_id=port.id,
            ordinal=1,
            source_kind="inline",
            inline_document={"claim_id": "claim-2"},
            connector_binding_id=connector.id,
        )
        db.add(two_sources)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        mismatched_kind = RunInputBinding(
            id="binding-mismatch",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            invocation_id=invocation.id,
            capability_port_id=port.id,
            ordinal=1,
            source_kind="asset_version",
            inline_document={"claim_id": "claim-3"},
        )
        db.add(mismatched_kind)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    engine.dispose()
