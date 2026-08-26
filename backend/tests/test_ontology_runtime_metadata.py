from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session

from app import database
from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
)
from app.schemas import EntityIn, PropertyIn
from app.services import ontology_service


class OntologyDefinitionAndRuntimeMetadataTests(unittest.TestCase):
    def test_namespace_constraints_state_validity_and_quality_are_enforced(self) -> None:
        definition = EntityIn(
            name="采购申请",
            namespace="supply.procurement",
            state_property="状态",
            properties=[
                PropertyIn(
                    name="申请编号",
                    data_type="string",
                    is_key=True,
                    is_required=True,
                    constraints={"pattern": r"PR-[0-9]{4}"},
                ),
                PropertyIn(
                    name="状态",
                    data_type="string",
                    is_required=True,
                    is_enum=True,
                    enum_values=["草稿", "已审批"],
                ),
                PropertyIn(
                    name="金额",
                    data_type="number",
                    constraints={"minimum": 0, "maximum": 1_000_000},
                ),
                PropertyIn(
                    name="申请人邮箱",
                    data_type="string",
                    constraints={"format": "email"},
                ),
            ],
        )
        ontology_service.validate_entity_definition(definition)
        entity = SimpleNamespace(
            state_property=definition.state_property,
            properties=definition.properties,
        )
        start = datetime.now(timezone.utc)
        attributes, state, quality = ontology_service.validate_instance_payload(
            entity,
            {
                "申请编号": "PR-0001",
                "状态": "草稿",
                "金额": 1200.5,
                "申请人邮箱": "buyer@example.test",
            },
            valid_from=start,
            valid_to=start + timedelta(days=30),
            quality={"score": 0.98, "status": "valid", "issues": []},
        )
        self.assertEqual(attributes["金额"], 1200.5)
        self.assertEqual(state, "草稿")
        self.assertEqual(quality["score"], 0.98)

        with self.assertRaisesRegex(ValueError, "大于最大值"):
            ontology_service.validate_instance_payload(
                entity,
                {
                    "申请编号": "PR-0002",
                    "状态": "草稿",
                    "金额": 1_000_001,
                    "申请人邮箱": "buyer@example.test",
                },
            )
        with self.assertRaisesRegex(ValueError, "保持一致"):
            ontology_service.validate_instance_payload(
                entity,
                {
                    "申请编号": "PR-0003",
                    "状态": "草稿",
                    "申请人邮箱": "buyer@example.test",
                },
                state="已审批",
            )
        with self.assertRaisesRegex(ValueError, "valid_to"):
            ontology_service.validate_instance_payload(
                entity,
                {
                    "申请编号": "PR-0004",
                    "状态": "草稿",
                    "申请人邮箱": "buyer@example.test",
                },
                valid_from=start,
                valid_to=start,
            )
        with self.assertRaisesRegex(ValueError, "email"):
            ontology_service.validate_instance_payload(
                entity,
                {
                    "申请编号": "PR-0005",
                    "状态": "草稿",
                    "申请人邮箱": "not-an-email",
                },
            )

    def test_constraints_and_transforms_are_declarative_allowlists(self) -> None:
        with self.assertRaisesRegex(ValueError, "boolean 类型不支持"):
            ontology_service.normalize_property_constraints(
                "boolean", {"min_length": 1}
            )
        entity = SimpleNamespace(
            properties=[SimpleNamespace(name="编码")]
        )
        rules = ontology_service.normalize_transform_rules(
            entity,
            {"编码": [{"op": "trim"}, {"op": "upper"}]},
        )
        self.assertEqual(
            ontology_service.apply_transform_rules(" ab-1 ", rules["编码"]),
            "AB-1",
        )
        with self.assertRaisesRegex(ValueError, "不支持的声明式转换"):
            ontology_service.normalize_transform_rules(
                entity, {"编码": [{"op": "python", "code": "pass"}]}
            )
        with self.assertRaisesRegex(ValueError, "old 不能为空"):
            ontology_service.normalize_transform_rules(
                entity, {"编码": [{"op": "replace", "old": "", "new": "x"}]}
            )
        with self.assertRaisesRegex(ValueError, "量化分组"):
            ontology_service.normalize_property_constraints(
                "string", {"pattern": r"(a+)+$"}
            )
        with self.assertRaisesRegex(ValueError, "量化分组"):
            ontology_service.normalize_property_constraints(
                "string", {"pattern": r"^(a?)+$"}
            )
        self.assertEqual(
            ontology_service.normalize_property_constraints(
                "string", {"pattern": r"^(ab|cd)-[0-9]+$"}
            )["pattern"],
            r"^(ab|cd)-[0-9]+$",
        )
        fixed = ontology_service.normalize_property_constraints(
            "string", {"const": "欠薪风险"}
        )
        self.assertEqual(fixed, {"const": "欠薪风险"})
        fixed_property = SimpleNamespace(
            name="风险类型",
            data_type="string",
            is_required=True,
            is_enum=False,
            enum_values=[],
            constraints=fixed,
        )
        ontology_service._validate_property_value(fixed_property, "欠薪风险")
        with self.assertRaisesRegex(ValueError, "必须等于固定值"):
            ontology_service._validate_property_value(fixed_property, "成本风险")

    def test_typed_defaults_are_normalized_and_cannot_bypass_constraints(self) -> None:
        definition = EntityIn(
            name="默认值验证",
            properties=[
                PropertyIn(
                    name="数量",
                    data_type="integer",
                    default_value="2",
                    constraints={"minimum": 1},
                )
            ],
        )
        ontology_service.validate_entity_definition(definition)
        self.assertEqual(definition.properties[0].default_value, 2)
        entity = SimpleNamespace(state_property="", properties=definition.properties)
        attributes, _state, _quality = ontology_service.validate_instance_payload(
            entity, {}
        )
        self.assertEqual(attributes["数量"], 2)
        definition.properties[0].default_value = "not-an-int"
        with self.assertRaisesRegex(ValueError, "默认值不符合 integer"):
            ontology_service.validate_entity_definition(definition)


class MappingTransformPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.scenario = BusinessScenario(
            id="scenario-transform",
            tenant_id="tenant-transform",
            name="映射转换",
            namespace="supply.mapping",
        )
        self.entity = OntologyEntity(
            id="entity-transform",
            scenario=self.scenario,
            name="供应商",
            namespace="supply.mapping",
            state_property="状态",
        )
        self.entity.properties = [
            OntologyProperty(
                name="编码",
                data_type="string",
                is_key=True,
                is_required=True,
                constraints={"pattern": r"SUP-[0-9]+"},
            ),
            OntologyProperty(
                name="状态",
                data_type="string",
                is_enum=True,
                is_required=True,
                enum_values=["启用", "停用"],
            ),
            OntologyProperty(name="金额", data_type="number"),
        ]
        self.source = DataSource(
            id="source-transform",
            tenant_id="tenant-transform",
            scenario_id=self.scenario.id,
            name="供应商源",
            type="sqlite",
            config={"path": "unused-by-mocked-query"},
        )
        self.mapping = DataMapping(
            id="mapping-transform",
            scenario=self.scenario,
            entity=self.entity,
            data_source=self.source,
            table_name="suppliers",
            column_map={"编码": "code", "状态": "status", "金额": "amount"},
            transform_rules={
                "编码": [{"op": "trim"}, {"op": "upper"}],
                "金额": [{"op": "to_float"}],
            },
        )
        self.db.add_all([self.scenario, self.entity, self.source, self.mapping])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @patch("app.services.ontology_service.datasource_service.run_query")
    def test_preview_and_import_apply_the_same_transform_rules(self, run_query) -> None:
        run_query.return_value = {
            "columns": ["code", "status", "amount"],
            "rows": [[" sup-7 ", "启用", "12.50"]],
            "row_count": 1,
            "truncated": False,
        }
        preview = ontology_service.preview_mapping(
            self.db,
            self.scenario,
            self.mapping,
            data_source=self.source,
        )
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual(preview["transformed_rows"][0]["编码"], "SUP-7")
        self.assertEqual(preview["transformed_rows"][0]["金额"], 12.5)

        result = ontology_service.import_instances_from_mapping(
            self.db,
            self.scenario,
            self.mapping,
            data_source=self.source,
        )
        self.assertEqual(result["instances_created"], 1)
        instance = self.db.scalar(
            select(OntologyInstance).where(
                OntologyInstance.entity_id == self.entity.id
            )
        )
        self.assertIsNotNone(instance)
        self.assertEqual(instance.attributes["编码"], "SUP-7")
        self.assertEqual(instance.attributes["金额"], 12.5)
        self.assertEqual(instance.state, "启用")
        self.assertEqual(instance.quality["status"], "valid")
        self.assertEqual(
            instance.source_metadata["transform_rules"],
            self.mapping.transform_rules,
        )

    @patch("app.services.ontology_service.datasource_service.run_query")
    def test_mapping_rejects_unconverted_values_for_declared_types(self, run_query) -> None:
        self.mapping.transform_rules = {}
        run_query.return_value = {
            "columns": ["code", "status", "amount"],
            "rows": [["SUP-8", "启用", "not-a-number"]],
            "row_count": 1,
            "truncated": False,
        }
        preview = ontology_service.preview_mapping(
            self.db,
            self.scenario,
            self.mapping,
            data_source=self.source,
        )
        self.assertFalse(preview["ok"])
        self.assertIn("不符合 number 类型", preview["errors"][0])
        with self.assertRaisesRegex(ValueError, "不符合 number 类型"):
            ontology_service.import_instances_from_mapping(
                self.db,
                self.scenario,
                self.mapping,
                data_source=self.source,
            )


class SQLiteMigrationIdempotenceTests(unittest.TestCase):
    def test_existing_rows_are_upgraded_twice_without_overwrite(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id TEXT PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE agents (id TEXT PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE llm_configs (id TEXT PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE messages (id TEXT PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE assistant_messages (id TEXT PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE business_scenarios (id TEXT PRIMARY KEY, name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE ontology_entities (id TEXT PRIMARY KEY, scenario_id TEXT, name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE ontology_properties (id TEXT PRIMARY KEY, entity_id TEXT, name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE ontology_relations (id TEXT PRIMARY KEY, scenario_id TEXT, name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE ontology_instances (id TEXT PRIMARY KEY, scenario_id TEXT, entity_id TEXT, name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE data_mappings (id TEXT PRIMARY KEY, scenario_id TEXT, table_name TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE action_execution_logs (id TEXT PRIMARY KEY, parameters JSON, result JSON)"
            )
            connection.exec_driver_sql(
                "INSERT INTO business_scenarios(id, name) VALUES ('s-1', '不得覆盖')"
            )
            connection.exec_driver_sql(
                "INSERT INTO ontology_entities(id, scenario_id, name) VALUES ('e-1', 's-1', '对象')"
            )
            connection.exec_driver_sql(
                "INSERT INTO ontology_properties(id, entity_id, name) VALUES ('p-1', 'e-1', '编码')"
            )
            connection.exec_driver_sql(
                "INSERT INTO ontology_relations(id, scenario_id, name) VALUES ('r-1', 's-1', '关联')"
            )
            connection.exec_driver_sql(
                "INSERT INTO ontology_instances(id, scenario_id, entity_id, name) VALUES ('i-1', 's-1', 'e-1', '对象一')"
            )
            connection.exec_driver_sql(
                "INSERT INTO data_mappings(id, scenario_id, table_name) VALUES ('m-1', 's-1', 'source_table')"
            )
            connection.exec_driver_sql(
                "INSERT INTO action_execution_logs(id, parameters, result) VALUES ('a-1', '{\"sentinel\": 1}', '{\"ok\": true}')"
            )
            connection.exec_driver_sql("INSERT INTO users(id) VALUES ('u-1')")

        with patch.object(database, "engine", legacy_engine):
            database._migrate_ontology_runtime_metadata()
            database._migrate_action_decision_chain()
            database._migrate_ontology_runtime_metadata()
            database._migrate_action_decision_chain()

        inspector = inspect(legacy_engine)
        self.assertIn(
            "transform_rules",
            {column["name"] for column in inspector.get_columns("data_mappings")},
        )
        self.assertIn(
            "constraints",
            {column["name"] for column in inspector.get_columns("ontology_relations")},
        )
        self.assertIn(
            "permission_decision",
            {
                column["name"]
                for column in inspector.get_columns("action_execution_logs")
            },
        )
        self.assertIn(
            "ix_ontology_instances_state",
            {index["name"] for index in inspector.get_indexes("ontology_instances")},
        )
        with legacy_engine.connect() as connection:
            scenario_row = connection.exec_driver_sql(
                "SELECT name, namespace FROM business_scenarios WHERE id = 's-1'"
            ).one()
            entity_row = connection.exec_driver_sql(
                "SELECT name, namespace, state_property FROM ontology_entities WHERE id = 'e-1'"
            ).one()
            relation_row = connection.exec_driver_sql(
                "SELECT name, constraints FROM ontology_relations WHERE id = 'r-1'"
            ).one()
            action_row = connection.exec_driver_sql(
                "SELECT parameters, result, actor_type, actor_user_id FROM action_execution_logs WHERE id = 'a-1'"
            ).one()
        self.assertEqual(scenario_row, ("不得覆盖", "default"))
        self.assertEqual(entity_row, ("对象", "default", ""))
        self.assertEqual(relation_row, ("关联", "{}"))
        self.assertEqual(action_row[0], '{"sentinel": 1}')
        self.assertEqual(action_row[1], '{"ok": true}')
        self.assertEqual(action_row[2], "unknown")
        self.assertIsNone(action_row[3])
        with self.assertRaises(Exception):
            with legacy_engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO action_execution_logs(id, parameters, result, actor_user_id) "
                    "VALUES ('a-invalid', '{}', '{}', 'missing-user')"
                )
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO action_execution_logs(id, parameters, result, actor_user_id) "
                "VALUES ('a-valid', '{}', '{}', 'u-1')"
            )
            connection.exec_driver_sql("DELETE FROM users WHERE id = 'u-1'")
            actor = connection.exec_driver_sql(
                "SELECT actor_user_id FROM action_execution_logs WHERE id = 'a-valid'"
            ).scalar_one()
        self.assertIsNone(actor)
        legacy_engine.dispose()

    def test_application_sqlite_connections_enable_foreign_keys(self) -> None:
        if database.engine.dialect.name != "sqlite":
            self.skipTest("SQLite-specific invariant")
        with database.engine.connect() as connection:
            enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        self.assertEqual(enabled, 1)

    def test_data_source_nullable_rebuild_preserves_all_columns_children_and_fk_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            path = Path(temp_dir) / "legacy-data-sources.sqlite3"
            url = f"sqlite:///{path.as_posix()}"
            legacy_engine = create_engine(url)

            @event.listens_for(legacy_engine, "connect")
            def _enable_fk(connection, _record) -> None:
                connection.execute("PRAGMA foreign_keys=ON")

            with legacy_engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE tenants(id VARCHAR(32) PRIMARY KEY)")
                connection.exec_driver_sql(
                    "CREATE TABLE business_scenarios("
                    "id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(32), "
                    "FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE CASCADE)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE data_sources("
                    "id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(32), is_public BOOLEAN DEFAULT 0, "
                    "scenario_id VARCHAR(32) NOT NULL, name VARCHAR(200) NOT NULL, type VARCHAR(30) NOT NULL, "
                    "config JSON, connector_revision INTEGER NOT NULL DEFAULT 1, status VARCHAR(20), "
                    "last_error TEXT, created_at DATETIME, custom_marker TEXT, "
                    "FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE CASCADE, "
                    "FOREIGN KEY(scenario_id) REFERENCES business_scenarios(id) ON DELETE CASCADE)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_data_sources_scenario_id ON data_sources(scenario_id)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_data_sources_tenant_id ON data_sources(tenant_id)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE bucket_files("
                    "id VARCHAR(32) PRIMARY KEY, data_source_id VARCHAR(32) NOT NULL, filename TEXT, "
                    "FOREIGN KEY(data_source_id) REFERENCES data_sources(id) ON DELETE CASCADE)"
                )
                connection.exec_driver_sql("INSERT INTO tenants VALUES ('tenant-1')")
                connection.exec_driver_sql(
                    "INSERT INTO business_scenarios VALUES ('scenario-1', 'tenant-1')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO data_sources VALUES ("
                    "'source-1', 'tenant-1', 1, 'scenario-1', '保留源', 'sqlite', '{}', 9, "
                    "'ok', '', CURRENT_TIMESTAMP, 'sentinel')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO bucket_files VALUES ('file-1', 'source-1', 'evidence.txt')"
                )

            with (
                patch.object(database, "engine", legacy_engine),
                patch.object(database, "_settings", SimpleNamespace(database_url=url)),
            ):
                database._migrate_data_sources_nullable_scenario()
                database._migrate_data_sources_nullable_scenario()

            columns = {column["name"]: column for column in inspect(legacy_engine).get_columns("data_sources")}
            self.assertTrue(columns["scenario_id"]["nullable"])
            self.assertIn("custom_marker", columns)
            with legacy_engine.connect() as same_checkout:
                self.assertEqual(
                    same_checkout.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
                    1,
                )
                row = same_checkout.exec_driver_sql(
                    "SELECT tenant_id, is_public, connector_revision, custom_marker "
                    "FROM data_sources WHERE id = 'source-1'"
                ).one()
                self.assertEqual(row, ("tenant-1", 1, 9, "sentinel"))
                self.assertEqual(
                    same_checkout.exec_driver_sql("SELECT COUNT(*) FROM bucket_files").scalar_one(),
                    1,
                )
                self.assertEqual(
                    same_checkout.exec_driver_sql("PRAGMA foreign_key_check").fetchall(),
                    [],
                )
                with legacy_engine.connect() as new_checkout:
                    self.assertEqual(
                        new_checkout.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
                        1,
                    )
            legacy_engine.dispose()

    def test_legacy_attachment_thread_migration_enforces_fk_and_delete_cascade(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE assistant_threads(id VARCHAR(32) PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE assistant_attachments("
                "id VARCHAR(32) PRIMARY KEY, thread_id VARCHAR(32), expires_at DATETIME)"
            )
            connection.exec_driver_sql("INSERT INTO assistant_threads VALUES ('thread-1')")
            connection.exec_driver_sql(
                "INSERT INTO assistant_attachments VALUES ('attachment-1', 'thread-1', CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_attachments VALUES ('orphan', 'missing', CURRENT_TIMESTAMP)"
            )
        with patch.object(database, "engine", legacy_engine):
            database._migrate_assistant_attachment_lifecycle()
            database._migrate_assistant_attachment_lifecycle()
        with legacy_engine.begin() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM assistant_attachments WHERE id = 'orphan'"
                ).scalar_one(),
                0,
            )
            with self.assertRaises(Exception):
                connection.exec_driver_sql(
                    "INSERT INTO assistant_attachments(id, thread_id, expires_at) "
                    "VALUES ('invalid', 'missing', CURRENT_TIMESTAMP)"
                )
            connection.exec_driver_sql("DELETE FROM assistant_threads WHERE id = 'thread-1'")
            self.assertEqual(
                connection.exec_driver_sql("SELECT COUNT(*) FROM assistant_attachments").scalar_one(),
                0,
            )
        legacy_engine.dispose()

    def test_nullable_orphan_repair_preserves_history_and_is_idempotent(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE business_scenarios(id VARCHAR(32) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE assistant_threads("
                "id VARCHAR(32) PRIMARY KEY, scenario_id VARCHAR(32), title TEXT, "
                "FOREIGN KEY(scenario_id) REFERENCES business_scenarios(id) ON DELETE SET NULL)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE assistant_audit_logs("
                "id VARCHAR(32) PRIMARY KEY, scenario_id VARCHAR(32), thread_id VARCHAR(32), "
                "operation TEXT, FOREIGN KEY(scenario_id) REFERENCES business_scenarios(id) "
                "ON DELETE SET NULL, FOREIGN KEY(thread_id) REFERENCES assistant_threads(id) "
                "ON DELETE SET NULL)"
            )
            connection.exec_driver_sql("CREATE TABLE tenants(id VARCHAR(32) PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE llm_configs(id VARCHAR(32) PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE llm_invocation_traces("
                "id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(32), llm_config_id VARCHAR(32), "
                "status TEXT, FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE SET NULL, "
                "FOREIGN KEY(llm_config_id) REFERENCES llm_configs(id) ON DELETE SET NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_threads VALUES ('thread-1', 'missing-scene', '保留会话')"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_audit_logs VALUES "
                "('audit-1', 'missing-scene', 'missing-thread', '保留审计')"
            )
            connection.exec_driver_sql(
                "INSERT INTO llm_invocation_traces VALUES "
                "('trace-1', 'missing-tenant', 'missing-model', 'failed')"
            )

        with patch.object(database, "engine", legacy_engine):
            database._repair_nullable_orphan_references()
            database._repair_nullable_orphan_references()

        with legacy_engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT scenario_id, title FROM assistant_threads WHERE id='thread-1'"
                ).one(),
                (None, "保留会话"),
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT scenario_id, thread_id, operation FROM assistant_audit_logs "
                    "WHERE id='audit-1'"
                ).one(),
                (None, None, "保留审计"),
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT tenant_id, llm_config_id, status FROM llm_invocation_traces "
                    "WHERE id='trace-1'"
                ).one(),
                (None, None, "failed"),
            )
        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
