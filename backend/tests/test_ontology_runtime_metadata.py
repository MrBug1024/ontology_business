from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

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
from tests.postgresql_migration_contracts import (
    baseline_table_ddl,
    migration_heads,
    migration_revisions,
    render_postgresql_upgrade,
)


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
            type="postgres",
            config={"host": "unused", "database": "unused"},
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


class PostgreSQLMigrationContractsTests(unittest.TestCase):
    def test_baseline_declares_runtime_metadata_without_sqlite_rebuilds(self) -> None:
        entity_ddl = baseline_table_ddl("ontology_entities")
        relation_ddl = baseline_table_ddl("ontology_relations")
        mapping_ddl = baseline_table_ddl("data_mappings")
        action_log_ddl = baseline_table_ddl("action_execution_logs")
        schema_sql = render_postgresql_upgrade("20260827_01")

        self.assertIn("namespace VARCHAR(180) NOT NULL", entity_ddl)
        self.assertIn("state_property VARCHAR(200) NOT NULL", entity_ddl)
        self.assertIn("constraints JSON NOT NULL", relation_ddl)
        self.assertIn("transform_rules JSON NOT NULL", mapping_ddl)
        self.assertIn("permission_decision JSON NOT NULL", action_log_ddl)
        self.assertIn("actor_user_id VARCHAR(32)", action_log_ddl)
        self.assertIn(
            "CREATE INDEX ix_ontology_instances_state ON ontology_instances (state)",
            schema_sql,
        )

    def test_baseline_keeps_data_sources_independent_from_a_scenario(self) -> None:
        ddl = baseline_table_ddl("data_sources")
        self.assertIn("scenario_id VARCHAR(32),", ddl)
        self.assertNotIn("scenario_id VARCHAR(32) NOT NULL", ddl)
        self.assertIn(
            "FOREIGN KEY(scenario_id) REFERENCES business_scenarios (id) "
            "ON DELETE SET NULL",
            ddl,
        )

    def test_alembic_graph_has_one_postgresql_head(self) -> None:
        revisions = migration_revisions()
        self.assertEqual(migration_heads(), ("20260904_12",))
        from app.database import POSTGRESQL_SCHEMA_REVISION

        self.assertEqual(POSTGRESQL_SCHEMA_REVISION, migration_heads()[0])
        self.assertEqual(revisions["20260904_12"], "20260903_11")
        self.assertEqual(revisions["20260903_11"], "20260903_10")
        self.assertEqual(revisions["20260903_10"], "20260903_09")
        self.assertEqual(revisions["20260903_09"], "20260903_08")
        self.assertEqual(revisions["20260903_08"], "20260903_07")
        self.assertEqual(revisions["20260903_07"], "20260828_06")
        self.assertEqual(revisions["20260828_06"], "20260828_05")
        self.assertEqual(revisions["20260828_05"], "20260827_04")
        self.assertEqual(revisions["20260827_04"], "20260827_03")
        self.assertEqual(revisions["20260827_03"], "20260827_02")
        self.assertEqual(revisions["20260827_02"], "20260827_01")
        self.assertIsNone(revisions["20260827_01"])

    def test_head_migration_adds_resource_ownership_audit_fields(self) -> None:
        sql = render_postgresql_upgrade("20260903_07")
        for table_name in ("business_scenarios", "data_sources", "agents"):
            self.assertIn(
                f"ALTER TABLE {table_name} ADD COLUMN created_by_user_id VARCHAR(32)",
                sql,
            )
            self.assertIn(
                f"ALTER TABLE {table_name} ADD COLUMN owner_user_id VARCHAR(32)",
                sql,
            )
            self.assertIn(
                f"CREATE INDEX ix_{table_name}_created_by_user_id "
                f"ON {table_name} (created_by_user_id)",
                sql,
            )
            self.assertIn(
                f"CREATE INDEX ix_{table_name}_owner_user_id "
                f"ON {table_name} (owner_user_id)",
                sql,
            )

    def test_head_migration_persists_email_verification_guess_limits(self) -> None:
        sql = render_postgresql_upgrade("20260903_08")
        self.assertIn(
            "ALTER TABLE email_verification_codes ADD COLUMN failed_attempts "
            "INTEGER DEFAULT '0' NOT NULL",
            sql,
        )
        self.assertIn(
            "ALTER TABLE email_verification_codes ADD COLUMN locked_until "
            "TIMESTAMP WITH TIME ZONE",
            sql,
        )

    def test_head_migration_persists_agent_mcp_session_conversations(self) -> None:
        sql = render_postgresql_upgrade("20260903_09")
        self.assertIn("CREATE TABLE agent_mcp_conversations", sql)
        self.assertIn(
            "CONSTRAINT uq_agent_mcp_conversations_service_session UNIQUE "
            "(service_id, external_session_hash)",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_agent_mcp_conversations_service_updated "
            "ON agent_mcp_conversations (service_id, updated_at)",
            sql,
        )

    def test_head_migration_serializes_agent_mcp_session_turns(self) -> None:
        sql = render_postgresql_upgrade("20260903_10")
        self.assertIn(
            "ALTER TABLE agent_mcp_conversations ADD COLUMN turn_lease_token ",
            sql,
        )
        self.assertIn(
            "ALTER TABLE agent_mcp_invocations ADD COLUMN external_request_hash ",
            sql,
        )
        self.assertIn(
            "CREATE UNIQUE INDEX ix_agent_mcp_invocations_mcp_conversation_request ",
            sql,
        )

    def test_head_migration_binds_agent_workflow_runs_to_conversations(self) -> None:
        sql = render_postgresql_upgrade("20260903_11")
        self.assertIn(
            "ALTER TABLE workflow_runs ADD COLUMN agent_conversation_id VARCHAR(32)",
            sql,
        )
        self.assertIn(
            "FOREIGN KEY(agent_conversation_id) REFERENCES conversations (id) ON DELETE SET NULL",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_workflow_runs_agent_conversation_id "
            "ON workflow_runs (agent_conversation_id)",
            sql,
        )

    def test_head_migration_adds_cross_workspace_invitation_state(self) -> None:
        sql = render_postgresql_upgrade("20260904_12")
        self.assertIn(
            "ALTER TABLE auth_sessions ADD COLUMN active_tenant_id VARCHAR(32)",
            sql,
        )
        self.assertIn(
            "ALTER TABLE organization_members ADD COLUMN invited_by_user_id VARCHAR(32)",
            sql,
        )
        self.assertIn("CREATE TABLE organization_invitations", sql)
        self.assertIn(
            "CONSTRAINT ck_organization_invitations_status CHECK "
            "(status IN ('pending', 'accepted', 'declined', 'revoked', 'expired'))",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_organization_invitations_user_status "
            "ON organization_invitations (user_id, status)",
            sql,
        )

    def test_baseline_declares_attachment_and_history_retention_policies(self) -> None:
        attachment_ddl = baseline_table_ddl("assistant_attachments")
        thread_ddl = baseline_table_ddl("assistant_threads")
        audit_ddl = baseline_table_ddl("assistant_audit_logs")
        trace_ddl = baseline_table_ddl("llm_invocation_traces")

        self.assertIn("thread_id VARCHAR(32),", attachment_ddl)
        self.assertIn(
            "FOREIGN KEY(thread_id) REFERENCES assistant_threads (id) ON DELETE CASCADE",
            attachment_ddl,
        )
        self.assertIn("scenario_id VARCHAR(32),", thread_ddl)
        self.assertIn(
            "FOREIGN KEY(scenario_id) REFERENCES business_scenarios (id) "
            "ON DELETE SET NULL",
            thread_ddl,
        )
        self.assertIn(
            "FOREIGN KEY(thread_id) REFERENCES assistant_threads (id) ON DELETE SET NULL",
            audit_ddl,
        )
        self.assertIn(
            "FOREIGN KEY(llm_config_id) REFERENCES llm_configs (id) ON DELETE SET NULL",
            trace_ddl,
        )
        self.assertIn(
            "FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE SET NULL",
            trace_ddl,
        )


if __name__ == "__main__":
    unittest.main()
