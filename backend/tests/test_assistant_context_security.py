from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRule,
    OntologyWorkflow,
    Tenant,
    User,
)
from app.routers import assistant
from app.services import permission_service, scenario_model_compiler


class AssistantContextSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.owner_tenant = Tenant(id="tenant-context-owner", name="所有方")
        self.reader_tenant = Tenant(id="tenant-context-reader", name="阅读方")
        self.owner = User(
            id="user-context-owner",
            tenant_id=self.owner_tenant.id,
            email="context-owner@example.test",
            password_hash="test-only",
            status="active",
        )
        self.reader = User(
            id="user-context-reader",
            tenant_id=self.reader_tenant.id,
            email="context-reader@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-context-public",
            tenant_id=self.owner_tenant.id,
            name="公开业务",
            is_public=True,
        )
        self.entity = OntologyEntity(
            id="entity-context",
            scenario_id=self.scenario.id,
            name="客户",
            state_property="内部授信密钥字段",
        )
        self.public_property = OntologyProperty(
            id="property-context-public",
            entity_id=self.entity.id,
            name="客户名称",
            data_type="string",
        )
        self.secret_property = OntologyProperty(
            id="property-context-secret",
            entity_id=self.entity.id,
            name="内部授信密钥字段",
            data_type="string",
            is_sensitive=True,
        )
        self.restricted_action = OntologyAction(
            id="action-context-restricted",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            name="受限扣款操作",
            access_scope="restricted",
            input_schema={
                "type": "object",
                "properties": {
                    "Authorization": {"default": "Bearer action-secret-value"},
                },
            },
            precondition="Authorization: Bearer action-precondition-secret",
            enabled=False,
        )
        self.function = FunctionDefinition(
            id="function-context",
            scenario_id=self.scenario.id,
            name="客户评分函数",
            input_schema={
                "type": "object",
                "properties": {
                    "credential": {"default": "Bearer function-secret-value"},
                },
            },
            output_schema={"type": "object", "properties": {}},
        )
        self.rule = OntologyRule(
            id="rule-context",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            name="授信规则",
            condition={"field": "客户名称", "op": "==", "value": "Bearer rule-secret-value"},
            action_on_match="Authorization: Bearer rule-action-secret",
        )
        self.event = OntologyEvent(
            id="event-context",
            scenario_id=self.scenario.id,
            name="授信事件",
            payload_schema={
                "type": "object",
                "properties": {
                    "token": {"default": "Bearer event-secret-value"},
                },
            },
            trigger_source="Authorization: Bearer event-trigger-secret",
        )
        self.restricted_workflow = OntologyWorkflow(
            id="workflow-context-restricted",
            scenario_id=self.scenario.id,
            name="受限审批流",
            access_scope="restricted",
            trigger_type="manual",
            trigger_config={"api_key": "workflow-secret-value"},
            nodes=[{"id": "start", "type": "start", "data": {"token": "node-secret-value"}}],
            edges=[],
            status="draft",
            enabled=False,
        )
        self.private_source = DataSource(
            id="source-context-private",
            tenant_id=self.owner_tenant.id,
            scenario_id=self.scenario.id,
            name="内部客户库",
            type="file_bucket",
        )
        self.mapping = DataMapping(
            id="mapping-context-private",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            data_source_id=self.private_source.id,
            table_name="customers",
            column_map={"客户名称": "name", "内部授信密钥字段": "credit_secret"},
        )
        self.db.add_all([
            self.owner_tenant,
            self.reader_tenant,
            self.owner,
            self.reader,
            self.scenario,
            self.entity,
            self.public_property,
            self.secret_property,
            self.restricted_action,
            self.function,
            self.rule,
            self.event,
            self.restricted_workflow,
            self.private_source,
            self.mapping,
        ])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.owner_tenant.id, owner_user_id=self.owner.id
        )
        permission_service.ensure_organization(
            self.db, self.reader_tenant.id, owner_user_id=self.reader.id
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def use_principal(self, tenant_id: str, user_id: str) -> None:
        self.db.info["tenant_id"] = tenant_id
        self.db.info["user_id"] = user_id

    def test_public_reader_context_excludes_restricted_children(self) -> None:
        self.use_principal(self.reader_tenant.id, self.reader.id)
        text = assistant._scenario_context(self.db, self.scenario)
        catalog = scenario_model_compiler._existing_catalog(self.scenario, self.db)
        serialized = json.dumps(catalog, ensure_ascii=False)

        self.assertIn("客户名称", text)
        for hidden in (
            "内部授信密钥字段",
            "受限扣款操作",
            "受限审批流",
            "内部客户库",
            "workflow-secret-value",
            "node-secret-value",
            "action-secret-value",
            "action-precondition-secret",
            "function-secret-value",
            "rule-secret-value",
            "rule-action-secret",
            "event-secret-value",
            "event-trigger-secret",
        ):
            self.assertNotIn(hidden, text)
            self.assertNotIn(hidden, serialized)
        with self.assertRaises(HTTPException) as denied:
            scenario_model_compiler.prepare_compilation_context(self.db, self.scenario)
        self.assertEqual(denied.exception.status_code, 403)

    def test_owner_catalog_and_context_recursively_redact_all_definition_credentials(self) -> None:
        self.use_principal(self.owner_tenant.id, self.owner.id)
        catalog = scenario_model_compiler._existing_catalog(self.scenario, self.db)
        serialized = json.dumps(catalog, ensure_ascii=False)
        context = assistant._scenario_context(self.db, self.scenario)
        self.assertIn("受限审批流", serialized)
        self.assertIn("内部授信密钥字段", serialized)
        for secret in (
            "workflow-secret-value",
            "node-secret-value",
            "action-secret-value",
            "action-precondition-secret",
            "function-secret-value",
            "rule-secret-value",
            "rule-action-secret",
            "event-secret-value",
            "event-trigger-secret",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, context)

    def test_compiler_rejects_an_oversized_existing_catalog_without_truncation(self) -> None:
        oversized = {"catalog": "x" * (scenario_model_compiler.MAX_EXISTING_CATALOG_CHARS + 1)}
        with (
            patch.object(scenario_model_compiler, "_existing_catalog", return_value=oversized),
            self.assertRaisesRegex(ValueError, "超过单次编译"),
        ):
            scenario_model_compiler._compiler_prompt(
                SimpleNamespace(),
                message="编译",
                paragraphs=[{"ref": "request:p0001", "text": "业务描述"}],
                mapping_catalog=[],
            )

    def test_compiler_rejects_oversized_mapping_catalog_before_prompting(self) -> None:
        self.use_principal(self.owner_tenant.id, self.owner.id)
        oversized = [{
            "schema": "x" * (scenario_model_compiler.MAX_MAPPING_CATALOG_CHARS + 1),
        }]
        with (
            patch.object(
                scenario_model_compiler,
                "_mapping_catalog",
                return_value=(oversized, {}),
            ),
            self.assertRaisesRegex(ValueError, "数据源表结构目录"),
        ):
            scenario_model_compiler.prepare_compilation_context(
                self.db, self.scenario
            )

    def test_compiler_rejects_total_prompt_over_budget(self) -> None:
        with (
            patch.object(scenario_model_compiler, "_existing_catalog", return_value={}),
            patch.object(scenario_model_compiler, "MAX_COMPILER_PROMPT_CHARS", 100),
            self.assertRaisesRegex(ValueError, "完整编译提示"),
        ):
            scenario_model_compiler._compiler_prompt(
                SimpleNamespace(),
                message="编译",
                paragraphs=[{"ref": "request:p0001", "text": "业务描述"}],
                mapping_catalog=[],
            )

    def test_reference_resolution_filters_acl_and_carries_safe_display_name(self) -> None:
        self.use_principal(self.reader_tenant.id, self.reader.id)
        unresolved: list[dict] = []
        hidden = scenario_model_compiler._resolve_ref(
            self.restricted_action.name,
            generated=[],
            existing=[self.restricted_action],
            resource_label="操作",
            unresolved=unresolved,
            source_refs=["request:p0001"],
            db=self.db,
        )
        visible = scenario_model_compiler._resolve_ref(
            self.entity.name,
            generated=[],
            existing=[self.entity],
            resource_label="对象",
            unresolved=unresolved,
            source_refs=["request:p0001"],
            db=self.db,
        )
        self.assertIsNone(hidden)
        self.assertEqual(visible["display_name"], "客户")
        self.assertNotIn("受限扣款操作", json.dumps(visible, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
