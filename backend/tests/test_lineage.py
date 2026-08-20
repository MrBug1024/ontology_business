from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import app
from app.models import (
    ActionExecutionLog,
    Agent,
    BucketFile,
    BusinessScenario,
    Conversation,
    DataMapping,
    DocumentChunk,
    Message,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowRun,
    DataSource,
)
from app.services import permission_service
from app.services.lineage_service import build_scenario_lineage


class LineageRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-1", name="租户")
        self.owner = User(
            id="user-1",
            tenant_id="tenant-1",
            email="owner@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(id="scenario-1", tenant_id="tenant-1", name="血缘测试")
        self.source = DataSource(
            id="source-1",
            tenant_id="tenant-1",
            scenario_id="scenario-1",
            name="业务资料库",
            type="file_bucket",
            config={},
        )
        self.entity = OntologyEntity(id="entity-1", scenario_id="scenario-1", name="费用单")
        self.mapping = DataMapping(
            id="mapping-1",
            scenario_id="scenario-1",
            entity_id="entity-1",
            data_source_id="source-1",
            table_name="expense_items",
            column_map={},
        )
        self.instance = OntologyInstance(
            id="object-1",
            scenario_id="scenario-1",
            entity_id="entity-1",
            name="费用单 A",
            source="imported",
            source_ref="expense_items:1001",
            source_metadata={"mapping_id": "mapping-1", "data_source_id": "source-1", "record_key": "1001"},
        )
        self.file = BucketFile(
            id="file-1",
            data_source_id="source-1",
            filename="费用规则.md",
            stored_path="/tmp/fees.md",
            status="parsed",
            parsed_text="费用异常必须人工审批。",
        )
        self.chunk = DocumentChunk(
            id="chunk-1",
            bucket_file_id="file-1",
            data_source_id="source-1",
            ordinal=0,
            char_start=0,
            char_end=11,
            text="费用异常必须人工审批。",
            content_hash="hash-1",
            embedding=[0.0],
        )
        self.agent = Agent(id="agent-1", tenant_id="tenant-1", scenario_id="scenario-1", name="费用助手")
        self.conversation = Conversation(id="conversation-1", agent_id="agent-1", title="审批建议")
        self.message = Message(
            id="message-1",
            conversation_id="conversation-1",
            role="assistant",
            content="费用单应进入审批【C1】。",
            tool_results=[
                {
                    "id": "tool-search",
                    "name": "search_documents",
                    "result": json.dumps(
                        {
                            "citations": [
                                {
                                    "citation_id": "C1",
                                    "chunk_id": "chunk-1",
                                    "file_id": "file-1",
                                    "data_source_id": "source-1",
                                    "filename": "费用规则.md",
                                    "char_start": 0,
                                    "char_end": 11,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                },
                {"id": "tool-action", "name": "execute_action", "result": '{"log_id":"log-1"}'},
            ],
        )
        self.action = OntologyAction(
            id="action-1",
            scenario_id="scenario-1",
            entity_id="entity-1",
            name="提交审批",
            executor_type="http",
            executor_config={},
        )
        self.log = ActionExecutionLog(
            id="log-1",
            scenario_id="scenario-1",
            target_type="action",
            target_id="action-1",
            target_name="提交审批",
            status="success",
            result={"external_id": "approval-1"},
        )
        self.workflow = OntologyWorkflow(
            id="workflow-1",
            scenario_id="scenario-1",
            name="审批流程",
            status="active",
            enabled=True,
        )
        self.run = WorkflowRun(
            id="run-1",
            scenario_id="scenario-1",
            workflow_id="workflow-1",
            status="succeeded",
            result={"steps": [{"result": {"log_id": "log-1"}}]},
        )
        self.db.add_all(
            [
                self.tenant,
                self.owner,
                self.scenario,
                self.source,
                self.entity,
                self.mapping,
                self.instance,
                self.file,
                self.chunk,
                self.agent,
                self.conversation,
                self.message,
                self.action,
                self.log,
                self.workflow,
                self.run,
            ]
        )
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.owner.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_builds_safe_cross_system_lineage_graph(self) -> None:
        graph = build_scenario_lineage(self.db, self.scenario.id)
        node_ids = {node["id"] for node in graph["nodes"]}
        edge_kinds = {edge["kind"] for edge in graph["edges"]}

        self.assertTrue(
            {
                "data_source:source-1",
                "mapping:mapping-1",
                "object:object-1",
                "document:file-1",
                "document_chunk:chunk-1",
                "ai_answer:message-1",
                "action:action-1",
                "action_execution:log-1",
                "external_result:log-1",
                "workflow_run:run-1",
            }.issubset(node_ids)
        )
        self.assertTrue(
            {"mapped_as", "materialized_as", "cited_by", "requested_action", "executed_as", "returned", "orchestrated"}.issubset(
                edge_kinds
            )
        )
        # 图接口只返回执行结果存在与状态，不把可能携带敏感字段的原始 result 泄露出去。
        result = next(node for node in graph["nodes"] if node["id"] == "external_result:log-1")
        self.assertNotIn("external_id", result["meta"])

    def test_lineage_route_is_registered(self) -> None:
        self.assertIn("/api/lineage/scenarios/{scenario_id}", {route.path for route in app.routes})

    def test_restricted_object_and_action_are_not_an_acl_side_channel(self) -> None:
        organization = permission_service.ensure_organization(self.db, self.tenant.id)
        viewer = User(
            id="viewer-1",
            tenant_id=self.tenant.id,
            email="viewer@example.test",
            password_hash="test-only",
            status="active",
        )
        self.instance.access_scope = "restricted"
        self.action.access_scope = "restricted"
        self.db.add(viewer)
        self.db.flush()
        permission_service.assign_member_role(
            self.db, organization, user_id=viewer.id, role_key="viewer"
        )
        self.db.commit()
        self.db.info["user_id"] = viewer.id

        graph = build_scenario_lineage(self.db, self.scenario.id)
        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertNotIn("object:object-1", node_ids)
        self.assertNotIn("action:action-1", node_ids)
        self.assertNotIn("action_execution:log-1", node_ids)


if __name__ == "__main__":
    unittest.main()
