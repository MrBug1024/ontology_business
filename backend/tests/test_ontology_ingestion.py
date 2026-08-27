from __future__ import annotations

import asyncio
from contextlib import nullcontext
from io import BytesIO
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app.database import Base
from app.models import (
    BusinessScenario,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
)
from app.routers import assistant
from app.services import ontology_service, workflow_service


class AssistantOntologyIngestionTests(unittest.TestCase):
    def test_attachment_context_preserves_tail_beyond_legacy_twelve_thousand_chars(self) -> None:
        marker = "TAIL-MARKER-AFTER-LEGACY-LIMIT"
        body = "建筑业务正文" * 3_000 + marker
        attachment = SimpleNamespace(
            id="attachment-long",
            filename="建筑行业本体.md",
            status="parsed",
            parsed_text=body,
            error="",
        )

        context, sources = assistant._attachment_context([attachment])

        self.assertIn(marker, context)
        self.assertEqual(sources[0]["characters"], len(body))
        self.assertFalse(sources[0]["truncated"])

    def test_attachment_context_rejects_over_limit_instead_of_truncating(self) -> None:
        attachment = SimpleNamespace(
            id="attachment-too-long",
            filename="超长建筑资料.md",
            status="parsed",
            parsed_text="文" * (assistant.ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS + 1),
            error="",
        )

        with self.assertRaisesRegex(HTTPException, "不会静默截断"):
            assistant._attachment_context([attachment])

    def test_attachment_upload_keeps_text_beyond_legacy_twenty_four_thousand_chars(self) -> None:
        marker = "TAIL-MARKER-AFTER-LEGACY-UPLOAD-LIMIT"
        parsed_text = "施工资料" * 7_000 + marker
        captured: dict[str, object] = {}

        class FakeDb:
            def add(self, item) -> None:
                captured["attachment"] = item

            def flush(self) -> None:
                return None

            def commit(self) -> None:
                return None

            def refresh(self, _item) -> None:
                return None

        upload = UploadFile(
            BytesIO(b"placeholder"),
            filename="建筑行业完整资料.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
        with (
            patch.object(assistant, "_purge_expired_attachments"),
            patch.object(assistant, "_tenant", return_value="tenant-ingestion"),
            patch.object(assistant, "_current_user_id", return_value="user-ingestion"),
            patch.object(assistant.datasource_service, "save_assistant_attachment_object"),
            patch.object(
                assistant.object_deletion_service,
                "prepare_assistant_attachment_upload",
                return_value=SimpleNamespace(object_key="managed-upload-key"),
            ),
            patch.object(
                assistant.object_deletion_service,
                "heartbeat_upload_intent",
                return_value=nullcontext(
                    SimpleNamespace(assert_active=lambda: None)
                ),
            ),
            patch.object(
                assistant.object_deletion_service,
                "begin_upload_put",
            ),
            patch.object(
                assistant.object_deletion_service,
                "retain_assistant_attachment_upload",
            ),
            patch.object(
                assistant.doc_parser,
                "parse_bytes",
                return_value={"status": "success", "text": parsed_text, "message": "ok"},
            ),
        ):
            result = asyncio.run(assistant.upload_attachment(upload, FakeDb()))

        self.assertIn(marker, result.parsed_text)
        self.assertEqual(result.parsed_text, parsed_text)
        self.assertIs(captured["attachment"], result)

    def test_generate_ontology_passes_long_context_and_keeps_more_than_eight_entities(self) -> None:
        marker = "TAIL-MARKER-AFTER-LEGACY-THREE-THOUSAND"
        description = "建筑行业完整业务说明" * 600 + marker
        entities = [
            {
                "name": f"建筑对象{i}",
                "description": f"第{i}类业务对象",
                "properties": [
                    {
                        "name": "对象编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_required": True,
                    }
                ],
            }
            for i in range(12)
        ]
        relations = [
            {
                "name": f"关联{i}",
                "source": f"建筑对象{i}",
                "target": f"建筑对象{i + 1}",
                "relation_type": "1:N",
            }
            for i in range(11)
        ]
        captured: dict[str, object] = {}

        def fake_chat(_llm, messages, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            captured["max_tokens"] = kwargs["max_tokens"]
            return {"content": json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False)}

        db = SimpleNamespace(info={"tenant_id": "tenant-ingestion"})
        scenario = SimpleNamespace(llm_config_id=None)
        llm = SimpleNamespace(id="llm-ingestion")
        with (
            patch.object(ontology_service.llm_service, "routable_configs", return_value=[llm]),
            patch.object(ontology_service.llm_service, "chat", side_effect=fake_chat),
        ):
            result = ontology_service.generate_ontology(db, scenario, description)

        self.assertIn(marker, str(captured["prompt"]))
        self.assertNotIn("3~8 个核心业务对象", str(captured["prompt"]))
        self.assertGreaterEqual(int(captured["max_tokens"]), 8_192)
        self.assertEqual(len(result["entities"]), 12)
        self.assertEqual(len(result["relations"]), 11)

    def test_generate_ontology_rejects_over_limit_with_actionable_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "不会静默截断"):
            ontology_service._ontology_context(
                "文" * (ontology_service.ONTOLOGY_CONTEXT_MAX_CHARS + 1)
            )

    def test_generate_workflow_keeps_document_tail_beyond_legacy_limit(self) -> None:
        marker = "WORKFLOW-TAIL-AFTER-LEGACY-LIMIT"
        description = "建筑审批流程" * 1_000 + marker
        captured: dict[str, str] = {}

        class EmptyResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeDb:
            info = {"tenant_id": "tenant-workflow-ingestion"}

            def execute(self, _statement):
                return EmptyResult()

        def fake_chat(_llm, messages, **_kwargs):
            captured["prompt"] = messages[-1]["content"]
            return {
                "content": json.dumps(
                    {
                        "name": "审批流程",
                        "nodes": [
                            {"id": "start", "type": "start", "name": "开始", "data": {}},
                            {"id": "end", "type": "end", "name": "结束", "data": {}},
                        ],
                        "edges": [{"id": "e1", "source": "start", "target": "end"}],
                    },
                    ensure_ascii=False,
                )
            }

        scenario = SimpleNamespace(id="scenario-workflow", llm_config_id=None, description="")
        llm = SimpleNamespace(id="llm-workflow")
        with (
            patch.object(workflow_service.llm_service, "routable_configs", return_value=[llm]),
            patch.object(workflow_service.llm_service, "chat", side_effect=fake_chat),
        ):
            result = workflow_service.generate_workflow(FakeDb(), scenario, description)

        self.assertIn(marker, captured["prompt"])
        self.assertEqual(result["name"], "审批流程")

    def test_generate_workflow_canonicalizes_unique_resource_name_to_id(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = Session(engine)
        db.info["tenant_id"] = "tenant-workflow-reference"
        try:
            scenario = BusinessScenario(
                tenant_id="tenant-workflow-reference",
                name="工作流引用",
                status="draft",
            )
            entity = OntologyEntity(scenario=scenario, name="测试单")
            action = OntologyAction(
                scenario=scenario,
                entity=entity,
                name="初始化测试数据",
                enabled=True,
            )
            db.add_all([scenario, entity, action])
            db.commit()
            llm = SimpleNamespace(id="llm-workflow-reference")
            response = {
                "content": json.dumps(
                    {
                        "name": "测试流程",
                        "nodes": [
                            {"id": "start", "type": "start", "name": "开始", "data": {}},
                            {
                                "id": "n1",
                                "type": "action",
                                "name": "初始化测试数据",
                                "data": {"action_id": "test_init", "params": {}},
                            },
                            {"id": "end", "type": "end", "name": "结束", "data": {}},
                        ],
                        "edges": [
                            {"source": "start", "target": "n1"},
                            {"source": "n1", "target": "end"},
                        ],
                    },
                    ensure_ascii=False,
                )
            }
            with (
                patch.object(workflow_service.llm_service, "routable_configs", return_value=[llm]),
                patch.object(workflow_service.llm_service, "chat", return_value=response),
            ):
                result = workflow_service.generate_workflow(db, scenario, "创建测试流程")

            self.assertEqual(result["nodes"][1]["data"]["action_id"], action.id)
        finally:
            db.close()
            engine.dispose()

    def test_generate_workflow_retries_hallucinated_reference_before_returning_draft(self) -> None:
        class EmptyResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeDb:
            info = {"tenant_id": "tenant-workflow-retry"}

            def execute(self, _statement):
                return EmptyResult()

        invalid = {
            "name": "错误流程",
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "n1", "type": "action", "name": "虚构操作", "data": {"action_id": "made_up"}},
                {"id": "end", "type": "end", "data": {}},
            ],
            "edges": [
                {"source": "start", "target": "n1"},
                {"source": "n1", "target": "end"},
            ],
        }
        valid = {
            "name": "可保存流程",
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "end", "type": "end", "data": {}},
            ],
            "edges": [{"source": "start", "target": "end"}],
        }
        prompts: list[str] = []

        def fake_chat(_llm, messages, **_kwargs):
            prompts.append(messages[-1]["content"])
            payload = invalid if len(prompts) == 1 else valid
            return {"content": json.dumps(payload, ensure_ascii=False)}

        scenario = SimpleNamespace(id="scenario-workflow-retry", llm_config_id=None, description="")
        llm = SimpleNamespace(id="llm-workflow-retry")
        with (
            patch.object(workflow_service.llm_service, "routable_configs", return_value=[llm]),
            patch.object(workflow_service.llm_service, "chat", side_effect=fake_chat),
        ):
            result = workflow_service.generate_workflow(FakeDb(), scenario, "创建测试流程")

        self.assertEqual(result["name"], "可保存流程")
        self.assertEqual(len(prompts), 2)
        self.assertIn("没有可引用的正式操作", prompts[1])
        self.assertIn("严禁虚构", prompts[0])

    def test_generate_workflow_never_returns_persistently_invalid_reference(self) -> None:
        class EmptyResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeDb:
            info = {"tenant_id": "tenant-workflow-invalid"}

            def execute(self, _statement):
                return EmptyResult()

        response = {
            "content": json.dumps(
                {
                    "name": "错误流程",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {}},
                        {"id": "n1", "type": "action", "name": "虚构操作", "data": {"action_id": "made_up"}},
                        {"id": "end", "type": "end", "data": {}},
                    ],
                    "edges": [
                        {"source": "start", "target": "n1"},
                        {"source": "n1", "target": "end"},
                    ],
                },
                ensure_ascii=False,
            )
        }
        scenario = SimpleNamespace(id="scenario-workflow-invalid", llm_config_id=None, description="")
        llm = SimpleNamespace(id="llm-workflow-invalid")
        with (
            patch.object(workflow_service.llm_service, "routable_configs", return_value=[llm]),
            patch.object(workflow_service.llm_service, "chat", return_value=response) as chat_mock,
        ):
            with self.assertRaisesRegex(
                workflow_service.WorkflowGenerationError,
                "没有创建可确认草稿",
            ):
                workflow_service.generate_workflow(FakeDb(), scenario, "创建测试流程")

        self.assertEqual(chat_mock.call_count, 3)

    def test_generated_ontology_extends_existing_type_and_keeps_relation_to_it(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = Session(engine)
        try:
            scenario = BusinessScenario(
                tenant_id="tenant-ontology-merge",
                name="建筑项目",
                namespace="construction",
                status="draft",
            )
            project = OntologyEntity(
                scenario=scenario,
                name="项目",
                namespace="construction",
            )
            project.properties.append(
                OntologyProperty(
                    name="项目ID",
                    data_type="string",
                    is_key=True,
                    is_required=True,
                )
            )
            db.add(scenario)
            db.commit()

            normalized = ontology_service.normalize_generated_ontology(
                {
                    "entities": [
                        {
                            "name": "项目",
                            "properties": [
                                {"name": "项目ID", "data_type": "string", "is_key": True},
                                {"name": "项目名称", "data_type": "string", "is_required": True},
                            ],
                        },
                        {
                            "name": "合同",
                            "properties": [
                                {"name": "合同ID", "data_type": "string", "is_key": True},
                            ],
                        },
                    ],
                    "relations": [
                        {
                            "name": "项目包含合同",
                            "source": "项目",
                            "target": "合同",
                            "relation_type": "1:N",
                        }
                    ],
                },
                existing_entity_names={"项目"},
            )
            self.assertEqual(len(normalized["relations"]), 1)

            result = ontology_service.apply_generated_ontology(db, scenario, normalized)

            self.assertEqual(result["entities_added"], 1)
            self.assertEqual(result["entities_skipped"], 1)
            self.assertEqual(result["properties_added"], 2)
            self.assertEqual(result["properties_skipped"], 1)
            property_names = set(
                db.scalars(select(OntologyProperty).where(OntologyProperty.entity_id == project.id))
            )
            self.assertEqual({prop.name for prop in property_names}, {"项目ID", "项目名称"})
            relation = db.scalars(select(OntologyRelation)).one()
            self.assertEqual(relation.source_entity_id, project.id)
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
