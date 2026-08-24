from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    ActionExecutionLog,
    Agent,
    AssistantAttachment,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    Conversation,
    LLMConfig,
    Message,
    DataMapping,
    DataSource,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    Tenant,
    User,
)
from app.routers import agents, assistant, scenarios
from app.schemas import ActionExecuteRequest, AssistantChatRequest, AssistantProposalApplyRequest, ChatRequest
from app.services import (
    datasource_service,
    operations_service,
    permission_service,
    scenario_model_compiler,
    workflow_service,
)


class AssistantGovernedProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp_dir.name) / "assistant-fk.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-assistant-proposal", name="助手提案租户")
        self.user = User(
            id="user-assistant-proposal",
            tenant_id=self.tenant.id,
            email="assistant-proposal@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add_all([self.tenant, self.user])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id
        # Some Windows SQLite drivers release file handles asynchronously even
        # after Engine.dispose(); do not turn that platform cleanup timing into
        # a product-test failure.
        self.runtime_sources: list[DataSource] = []

    def tearDown(self) -> None:
        for source in self.runtime_sources:
            datasource_service.invalidate_engine(source)
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _proposal_message(
        self,
        *,
        kind: str,
        proposal: dict,
        scenario: BusinessScenario | None = None,
    ) -> tuple[AssistantThread, AssistantMessage]:
        thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scenario_id=scenario.id if scenario else None,
            scope_key=(
                f"scenario:{scenario.id}|path:/scenarios/{scenario.id}"
                if scenario
                else "scenario:global|path:/scenarios"
            ),
            title=f"{kind} proposal",
        )
        self.db.add(thread)
        self.db.flush()
        message = AssistantMessage(
            thread_id=thread.id,
            role="assistant",
            content="待确认草稿",
            proposal=proposal,
        )
        self.db.add(message)
        self.db.commit()
        return thread, message

    @staticmethod
    async def _consume_until(response, marker: str = "") -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            chunks.append(text)
            if marker and marker in "".join(chunks):
                await response.body_iterator.aclose()
                break
        return "".join(chunks)

    def test_global_scenario_proposal_requires_confirmation_and_keeps_attachment_temporary(self) -> None:
        attachment = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            filename="业务说明.md",
            status="parsed",
            parsed_text="临时业务资料",
        )
        self.db.add(attachment)
        proposal = assistant._build_proposal(
            "scenario",
            {
                "name": "采购协同",
                "description": "统一采购申请、审批和执行边界。",
                "industry": "供应链",
                "status": "draft",
            },
        )
        thread, _message = self._proposal_message(kind="scenario", proposal=proposal)

        with self.assertRaises(Exception):
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="scenario",
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    confirm=False,
                ),
                self.db,
            )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(BusinessScenario)),
            0,
        )

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario",
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                confirm=True,
            ),
            self.db,
        )
        scenario = self.db.get(BusinessScenario, result["data"]["scenario_id"])
        self.assertIsNotNone(scenario)
        self.assertEqual(scenario.status, "draft")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DataSource)),
            0,
        )
        # The attachment remains isolated in the assistant store.  Applying a
        # scene never promotes it into a bucket/data source.
        self.assertIsNotNone(self.db.get(AssistantAttachment, attachment.id))

        replay = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario",
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(BusinessScenario)),
            1,
        )

    def test_mapping_proposal_revalidates_schema_and_saves_definition_without_import(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="采购映射",
            status="draft",
        )
        self.db.add(scenario)
        self.db.flush()
        entity = OntologyEntity(scenario_id=scenario.id, name="采购申请")
        self.db.add(entity)
        self.db.flush()
        self.db.add_all(
            [
                OntologyProperty(
                    entity_id=entity.id,
                    name="申请编号",
                    data_type="string",
                    is_key=True,
                    is_required=True,
                ),
                OntologyProperty(
                    entity_id=entity.id,
                    name="金额",
                    data_type="number",
                    is_required=True,
                ),
            ]
        )
        source_path = Path(self.temp_dir.name) / "source.sqlite3"
        with sqlite3.connect(source_path) as connection:
            connection.execute(
                "CREATE TABLE purchase_requests (request_no TEXT PRIMARY KEY, amount REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO purchase_requests(request_no, amount) VALUES ('P-1', 1200)"
            )
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="采购数据库",
            type="sqlite",
            config={"path": str(source_path)},
            status="ok",
        )
        self.db.add(source)
        self.runtime_sources.append(source)
        self.db.commit()
        self.db.refresh(scenario)
        data = {
            "entity_id": entity.id,
            "entity_name": entity.name,
            "data_source_id": source.id,
            "data_source_name": source.name,
            "table_name": "purchase_requests",
            "column_map": {"申请编号": "request_no", "金额": "amount"},
        }
        proposal = assistant._build_proposal("mapping", data, scenario)
        thread, _message = self._proposal_message(
            kind="mapping",
            proposal=proposal,
            scenario=scenario,
        )

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="mapping",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                confirm=True,
            ),
            self.db,
        )
        mapping = self.db.get(DataMapping, result["data"]["mapping_id"])
        self.assertEqual(mapping.column_map, data["column_map"])
        self.assertTrue(result["data"]["refresh_required"])
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyInstance)),
            0,
        )

    def test_compound_scenario_model_proposal_uses_confirmed_atomic_apply_path(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="建筑项目履约",
            namespace="construction",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        source_bundle = scenario_model_compiler.build_source_bundle(
            "",
            [{
                "id": "construction-apply",
                "filename": "建筑项目模型.md",
                "text": "项目以项目编号唯一标识。",
            }],
        )
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            scenario,
            {
                "schema_version": "scenario_model.v1",
                "entities": [{
                    "key": "entity.project",
                    "name": "项目",
                    "properties": [{
                        "name": "项目编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_required": True,
                    }],
                    "evidence_refs": ["construction-apply:p0001"],
                    "confidence": 1.0,
                }],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "unresolved": [],
                "coverage": [{
                    "source_ref": "construction-apply:p0001",
                    "status": "modeled",
                    "reason": "项目对象与主键",
                    "change_keys": ["entity.project"],
                }],
            },
            source_bundle=source_bundle,
        )
        proposal = assistant._build_proposal("scenario_model", payload, scenario)
        thread, _message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        with self.assertRaises(Exception):
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="scenario_model",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    confirm=False,
                ),
                self.db,
            )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(result["data"]["counts"]["entities_added"], 1)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )
        replay = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )

    def test_mapping_apply_rejects_stale_or_invented_columns(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="失效映射",
            status="draft",
        )
        entity = OntologyEntity(scenario=scenario, name="订单")
        self.db.add_all([scenario, entity])
        self.db.flush()
        self.db.add(
            OntologyProperty(
                entity_id=entity.id,
                name="订单号",
                is_key=True,
                is_required=True,
            )
        )
        source_path = Path(self.temp_dir.name) / "stale.sqlite3"
        with sqlite3.connect(source_path) as connection:
            connection.execute("CREATE TABLE orders (order_no TEXT PRIMARY KEY)")
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="订单源",
            type="sqlite",
            config={"path": str(source_path)},
        )
        self.db.add(source)
        self.runtime_sources.append(source)
        self.db.commit()
        self.db.refresh(scenario)
        proposal = assistant._build_proposal(
            "mapping",
            {
                "entity_id": entity.id,
                "entity_name": entity.name,
                "data_source_id": source.id,
                "data_source_name": source.name,
                "table_name": "orders",
                "column_map": {"订单号": "invented_column"},
            },
            scenario,
        )
        thread, _message = self._proposal_message(
            kind="mapping",
            proposal=proposal,
            scenario=scenario,
        )
        with self.assertRaisesRegex(ValueError, "不存在的源字段"):
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="mapping",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    confirm=True,
                ),
                self.db,
            )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DataMapping)),
            0,
        )

    def test_mapping_update_preserves_omitted_transforms_and_validates_explicit_rules(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="转换保留",
            status="draft",
        )
        entity = OntologyEntity(scenario=scenario, name="订单")
        self.db.add_all([scenario, entity])
        self.db.flush()
        self.db.add(
            OntologyProperty(
                entity_id=entity.id,
                name="订单号",
                is_key=True,
                is_required=True,
            )
        )
        source_path = Path(self.temp_dir.name) / "transform-preserve.sqlite3"
        with sqlite3.connect(source_path) as connection:
            connection.execute("CREATE TABLE orders (order_no TEXT PRIMARY KEY)")
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="订单源",
            type="sqlite",
            config={"path": str(source_path)},
        )
        mapping = DataMapping(
            scenario=scenario,
            entity=entity,
            data_source=source,
            table_name="orders",
            column_map={"订单号": "order_no"},
            transform_rules={"订单号": [{"op": "trim"}]},
        )
        self.db.add_all([source, mapping])
        self.runtime_sources.append(source)
        self.db.commit()
        self.db.refresh(scenario)
        data = {
            "entity_id": entity.id,
            "data_source_id": source.id,
            "table_name": "orders",
            "column_map": {"订单号": "order_no"},
        }

        updated, operation = assistant._apply_mapping_draft(
            self.db, scenario, data
        )
        self.assertEqual(operation, "update")
        self.assertEqual(updated.transform_rules, {"订单号": [{"op": "trim"}]})

        data["transform_rules"] = {"订单号": [{"op": "upper"}]}
        updated, _operation = assistant._apply_mapping_draft(self.db, scenario, data)
        self.assertEqual(updated.transform_rules, {"订单号": [{"op": "upper"}]})
        data["transform_rules"] = {"订单号": [{"op": "python"}]}
        with self.assertRaisesRegex(ValueError, "不支持的声明式转换"):
            assistant._apply_mapping_draft(self.db, scenario, data)

    def test_chat_apply_and_execute_modes_only_return_governance_guidance(self) -> None:
        before_scenarios = self.db.scalar(
            select(func.count()).select_from(BusinessScenario)
        )
        apply_reply = assistant.chat(
            AssistantChatRequest(
                message="立即应用并创建一个采购场景",
                path="/scenarios",
                mode="apply",
            ),
            self.db,
        )
        execute_reply = assistant.chat(
            AssistantChatRequest(
                message="立即执行这个工作流",
                path="/tasks",
                mode="execute",
            ),
            self.db,
        )

        self.assertEqual(apply_reply.proposal, {})
        self.assertIn("confirm=true", apply_reply.reply)
        self.assertEqual(execute_reply.proposal, {})
        self.assertIn("不会直接触发", execute_reply.reply)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(BusinessScenario)),
            before_scenarios,
        )

    def test_legacy_name_only_proposal_is_rejected_as_unverifiable(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="旧提案",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        proposal = assistant._build_proposal(
            "workflow",
            {
                "name": "旧流程",
                "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                "edges": [{"source": "start", "target": "end"}],
            },
            scenario,
        )
        proposal["base_snapshot"].pop("revision")
        thread, _message = self._proposal_message(
            kind="workflow", proposal=proposal, scenario=scenario
        )
        with self.assertRaisesRegex(Exception, "重新生成"):
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="workflow",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    confirm=True,
                ),
                self.db,
            )

    def test_explain_mode_overrides_draft_keywords_and_remains_read_only(self) -> None:
        reply = assistant.chat(
            AssistantChatRequest(
                message="解释如何创建场景、建立实体并执行工作流",
                path="/scenarios",
                mode="explain",
            ),
            self.db,
        )
        self.assertEqual(reply.proposal, {})
        self.assertIn("只读取", reply.reply)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(BusinessScenario)),
            0,
        )

    def test_execute_mode_resolves_action_and_only_persists_a_dry_run(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="助手预演",
            status="active",
        )
        entity = OntologyEntity(scenario=scenario, name="审批单")
        action = OntologyAction(
            scenario=scenario,
            entity=entity,
            name="提交审批",
            description="将审批单提交到外部审批系统",
            input_schema={
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
            executor_type="http",
            executor_config={"method": "POST", "url": "https://example.test/approve"},
            requires_confirmation=True,
            idempotency_required=True,
        )
        self.db.add_all([scenario, entity, action])
        self.db.commit()
        self.db.refresh(scenario)

        reply = assistant.chat(
            AssistantChatRequest(
                message="预演提交审批",
                scenario_id=scenario.id,
                path=f"/scenarios/{scenario.id}",
                selection={"action_id": action.id, "params": {"reference": "REQ-1"}},
                mode="execute",
            ),
            self.db,
        )
        self.assertEqual(reply.proposal, {})
        self.assertEqual(reply.action_preview["target"]["id"], action.id)
        self.assertTrue(reply.action_preview["permission"]["allowed"])
        self.assertTrue(reply.action_preview["impact"]["side_effects_skipped"])
        self.assertTrue(reply.action_preview["requires_approval"])
        self.assertEqual(reply.action_preview["preview"]["status"], "dry_run")
        self.assertIn("action_preview", {item["name"] for item in reply.evidence.tools_called})
        logs = self.db.execute(
            select(ActionExecutionLog).where(ActionExecutionLog.target_id == action.id)
        ).scalars().all()
        self.assertEqual([log.mode for log in logs], ["dry_run"])
        self.assertEqual(logs[0].assistant_message_id, history_message_id := self.db.execute(
            select(AssistantMessage.id).where(
                AssistantMessage.thread_id == reply.thread_id,
                AssistantMessage.role == "assistant",
            )
        ).scalar_one())
        history = assistant.list_thread_messages(
            reply.thread_id,
            scenario_id=scenario.id,
            path=f"/scenarios/{scenario.id}",
            db=self.db,
        )
        saved_answer = history[-1]
        self.assertEqual(saved_answer.id, history_message_id)
        self.assertEqual(saved_answer.evidence["confidence"], 0.9)
        self.assertEqual(saved_answer.action_preview["preview"]["log_id"], logs[0].id)

    def test_execute_stream_persists_parent_and_preview_before_event_is_exposed(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="助手流式预演",
            status="active",
        )
        entity = OntologyEntity(scenario=scenario, name="流式审批单")
        action = OntologyAction(
            scenario=scenario,
            entity=entity,
            name="流式提交审批",
            input_schema={
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
            executor_type="http",
            executor_config={"method": "POST", "url": "https://example.test/approve"},
            requires_confirmation=True,
        )
        self.db.add_all([scenario, entity, action])
        self.db.commit()
        self.db.refresh(scenario)
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

        with patch.object(assistant, "SessionLocal", factory):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="预演流式提交审批",
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    selection={"action_id": action.id, "params": {"reference": "REQ-SSE"}},
                    mode="execute",
                ),
                self.db,
            )
            body = asyncio.run(self._consume_until(response, '"type": "action_preview"'))

        self.assertIn('"type": "action_preview"', body)
        self.db.expire_all()
        log = self.db.execute(
            select(ActionExecutionLog).where(ActionExecutionLog.target_id == action.id)
        ).scalar_one()
        parent = self.db.get(AssistantMessage, log.assistant_message_id)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.context["action_preview"]["preview"]["log_id"], log.id)

    def test_scenario_model_stream_reloads_detached_scenario_in_owned_session(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="流式复合编译",
            status="active",
        )
        function = FunctionDefinition(
            scenario=scenario,
            name="计算风险分",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
        )
        self.db.add_all([scenario, function])
        self.db.commit()
        tenant_id = self.tenant.id
        user_id = self.user.id
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        observed: dict[str, object] = {}

        def fake_compile(db, streamed_scenario, **_kwargs):
            # This relationship was not touched by _scenario_context.  It can
            # only load here when the SSE turn reloaded the scenario in its
            # explicitly-owned session.
            observed["functions"] = [
                item.name for item in streamed_scenario.function_definitions
            ]
            observed["tenant_id"] = db.info.get("tenant_id")
            observed["user_id"] = db.info.get("user_id")
            return {
                "schema_version": "scenario_model.v1",
                "source_manifest": [],
                "entities": [],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "unresolved": [],
                "coverage": [],
                "coverage_summary": {
                    "total": 0,
                    "modeled": 0,
                    "context": 0,
                    "irrelevant": 0,
                    "ambiguous": 0,
                },
                "changes": [],
                "fingerprint": "stream-session-regression",
            }

        with (
            patch.object(assistant, "SessionLocal", factory),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="编译完整业务模型",
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                ),
                self.db,
            )
            # Reproduce the production lifecycle: request-scoped ORM objects
            # are detached before StreamingResponse starts consuming its body.
            self.db.expunge_all()
            body = asyncio.run(self._consume_until(response))

        self.assertNotIn("not bound to a Session", body)
        self.assertIn('"type": "proposal"', body)
        self.assertEqual(observed["functions"], ["计算风险分"])
        self.assertEqual(observed["tenant_id"], tenant_id)
        self.assertEqual(observed["user_id"], user_id)

    def test_temporary_attachment_is_bound_to_one_thread_and_expired_rows_are_purged(self) -> None:
        first = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scope_key="scenario:global|path:/one",
            title="one",
        )
        second = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scope_key="scenario:global|path:/two",
            title="two",
        )
        live = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            filename="live.txt",
            status="parsed",
            parsed_text="temporary",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        expired = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            filename="expired.txt",
            status="parsed",
            parsed_text="expired",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.db.add_all([first, second, live, expired])
        self.db.commit()
        selected = assistant._safe_attachment_ids(
            self.db, [live.id], thread_id=first.id
        )
        self.assertEqual(selected[0].thread_id, first.id)
        self.assertIsNotNone(selected[0].consumed_at)
        with self.assertRaisesRegex(Exception, "不可用"):
            assistant._safe_attachment_ids(
                self.db, [live.id], thread_id=second.id
            )
        with self.assertRaisesRegex(Exception, "不可用"):
            assistant._safe_attachment_ids(
                self.db, [expired.id], thread_id=first.id
            )
        assistant._purge_expired_attachments(self.db)
        self.assertIsNone(self.db.get(AssistantAttachment, expired.id))

    def test_nonempty_unavailable_attachment_fails_closed_and_global_ttl_is_bounded(self) -> None:
        thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scope_key="scenario:global|path:/expired",
            title="expired",
        )
        other = User(
            tenant_id=self.tenant.id,
            email="expired-owner@example.test",
            password_hash="test-only",
            status="active",
        )
        expired_owned = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            filename="owned-expired.txt",
            parsed_text="must-not-be-used",
            status="parsed",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        expired_other = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=other.id,
            filename="other-expired.txt",
            parsed_text="must-be-purged",
            status="parsed",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        expired_legacy = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=None,
            filename="legacy-expired.txt",
            parsed_text="must-be-purged",
            status="parsed",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.db.add_all([thread, other, expired_owned, expired_other, expired_legacy])
        self.db.commit()

        before_messages = self.db.scalar(select(func.count()).select_from(AssistantMessage))
        with self.assertRaisesRegex(Exception, "不可用"):
            assistant.chat(
                AssistantChatRequest(
                    message="必须使用过期附件生成草稿",
                    path="/expired",
                    attachment_ids=[expired_owned.id],
                    mode="draft",
                ),
                self.db,
            )
        self.db.rollback()
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantMessage)),
            before_messages,
        )
        self.assertEqual(
            operations_service.purge_expired_assistant_attachments(self.db, limit=2),
            2,
        )
        self.assertEqual(
            operations_service.purge_expired_assistant_attachments(self.db, limit=2),
            1,
        )
        self.db.commit()
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantAttachment)),
            0,
        )

    def test_agent_stream_action_preview_has_a_durable_parent_before_tool_result(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="Agent FK 预演",
            status="active",
        )
        entity = OntologyEntity(scenario=scenario, name="Agent 审批单")
        action = OntologyAction(
            scenario=scenario,
            entity=entity,
            name="Agent 提交审批",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            executor_type="http",
            executor_config={"method": "POST", "url": "https://example.test/agent"},
            requires_confirmation=True,
        )
        source = DataSource(
            id="source-agent-fk",
            tenant_id=self.tenant.id,
            scenario=scenario,
            name="Agent 审批数据",
            type="file_bucket",
            status="ok",
        )
        mapping = DataMapping(
            id="mapping-agent-fk",
            scenario=scenario,
            entity=entity,
            data_source=source,
            table_name="approval_records",
            column_map={},
            status="ready",
        )
        llm = LLMConfig(
            tenant_id=self.tenant.id,
            name="Agent 测试模型",
            provider="openai",
            model="test-model",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        agent = Agent(
            tenant_id=self.tenant.id,
            scenario=scenario,
            llm_config=llm,
            name="FK Agent",
            data_source_ids=[source.id],
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": [action.id]},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": []},
                "workflows": {"mode": "explicit", "selected_ids": []},
            },
        )
        self.db.add_all([scenario, entity, action, source, mapping, llm, agent])
        self.db.commit()
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

        def fake_run_agent(db, routed_agent, routed_llm, *_args, trace_context=None, **_kwargs):
            previous_trace = db.info.get("llm_trace_context")
            previous_audit = db.info.get("action_audit_context")
            db.info["llm_trace_context"] = dict(trace_context or {})
            db.info["action_audit_context"] = {
                "agent_id": routed_agent.id,
                "llm_config_id": routed_llm.id,
                "model_name": routed_llm.model,
            }
            try:
                yield {"type": "tool_call", "data": {"id": "call-1", "name": "execute_action", "arguments": {}}}
                preview = workflow_service.execute_action(db, action, {}, dry_run=True)
                yield {"type": "tool_result", "data": {"id": "call-1", "name": "execute_action", "result": json.dumps(preview)}}
                yield {"type": "token", "data": "预演完成"}
            finally:
                if previous_trace is None:
                    db.info.pop("llm_trace_context", None)
                else:
                    db.info["llm_trace_context"] = previous_trace
                if previous_audit is None:
                    db.info.pop("action_audit_context", None)
                else:
                    db.info["action_audit_context"] = previous_audit

        with (
            patch.object(agents, "SessionLocal", factory),
            patch.object(agents.agent_engine, "run_agent", fake_run_agent),
        ):
            response = agents.chat(agent.id, ChatRequest(message="请预演 Action"), self.db)
            body = asyncio.run(self._consume_until(response, '"type": "tool_result"'))

        self.assertIn('"type": "tool_result"', body)
        self.db.expire_all()
        log = self.db.execute(
            select(ActionExecutionLog).where(ActionExecutionLog.target_id == action.id)
        ).scalar_one()
        parent = self.db.get(Message, log.agent_message_id)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.tool_results[0]["name"], "execute_action")
        self.assertTrue(parent.stream_finalized)


class ActionDecisionChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        tenant = Tenant(id="tenant-action-audit", name="Action 审计租户")
        user = User(
            id="user-action-audit",
            tenant_id=tenant.id,
            email="action-audit@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-action-audit",
            tenant_id=tenant.id,
            name="Action 审计场景",
            status="active",
        )
        entity = OntologyEntity(
            id="entity-action-audit",
            scenario_id=self.scenario.id,
            name="审计对象",
        )
        source = DataSource(
            id="source-action-audit",
            tenant_id=tenant.id,
            scenario_id=self.scenario.id,
            name="Action 审计数据源",
            type="sqlite",
            config={"path": "not-opened-in-preview.sqlite3"},
            status="ok",
        )
        self.action = OntologyAction(
            id="action-audit",
            scenario_id=self.scenario.id,
            entity_id=entity.id,
            name="审计预演",
            input_schema={
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
            executor_type="sql",
            executor_config={
                "data_source_id": source.id,
                "sql": "SELECT '{reference}' AS reference",
            },
            requires_confirmation=True,
            enabled=True,
        )
        self.db.add_all([tenant, user, self.scenario, entity, source, self.action])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id
        self.db.info["action_audit_context"] = {
            "agent_id": None,
            "llm_config_id": None,
            "model_name": "",
        }

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_action_log_persists_real_actor_permission_data_and_result_chain(self) -> None:
        response = workflow_service.preview_action(
            self.db,
            self.action,
            {"reference": "REQ-1"},
        )
        log = self.db.get(ActionExecutionLog, response["log_id"])
        self.assertEqual(log.actor_type, "user")
        self.assertEqual(log.actor_user_id, "user-action-audit")
        self.assertIsNone(log.agent_id)
        self.assertIsNone(log.llm_config_id)
        self.assertEqual(log.model_name, "")
        self.assertTrue(log.permission_decision["allowed"])
        self.assertEqual(log.input_params, {"reference": "REQ-1"})
        self.assertEqual(log.status, "dry_run")
        self.assertIn("plan", log.result)

        rows = scenarios.list_execution_logs(self.scenario.id, 50, self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.actor_user_id, "user-action-audit")
        self.assertEqual(row.actor_type, "user")
        self.assertTrue(row.permission_decision["allowed"])
        self.assertEqual(row.data_context, {})

    def test_confirmed_agent_action_replaces_dry_run_in_durable_message(self) -> None:
        agent = Agent(
            id="agent-action-audit",
            tenant_id="tenant-action-audit",
            scenario_id=self.scenario.id,
            name="审计 Agent",
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": [self.action.id]},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": []},
                "workflows": {"mode": "explicit", "selected_ids": []},
            },
        )
        conversation = Conversation(
            id="conversation-action-audit",
            agent=agent,
            created_by_user_id="user-action-audit",
            title="附件生成",
        )
        message = Message(
            id="message-action-audit",
            conversation=conversation,
            role="assistant",
            content="已生成安全预演。",
            tool_calls=[{"id": "call-1", "name": "execute_action"}],
            tool_results=[],
        )
        self.db.add_all([agent, conversation, message])
        self.db.commit()
        self.db.info["llm_trace_context"] = {"assistant_message_id": message.id}
        self.db.info["action_audit_context"] = {
            "agent_id": agent.id,
            "llm_config_id": None,
            "model_name": "test-model",
        }

        preview = scenarios.execute_action(
            self.action.id,
            ActionExecuteRequest(params={"reference": "REQ-AGENT"}, dry_run=True),
            self.db,
        )
        message.tool_results = [{
            "id": "call-1",
            "name": "execute_action",
            "result": json.dumps(preview, ensure_ascii=False),
        }]
        self.db.commit()
        final_response = {
            "status": "success",
            "result": {
                "artifact": {
                    "id": "a" * 32,
                    "filename": "项目报告.docx",
                    "format": "docx",
                    "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "size": 1024,
                    "sha256": "a" * 64,
                    "download_url": f"/api/data-sources/files/{'a' * 32}/download",
                }
            },
        }
        with patch.object(
            scenarios.workflow_service,
            "execute_action",
            return_value=final_response,
        ):
            response = scenarios.execute_action(
                self.action.id,
                ActionExecuteRequest(
                    params={"reference": "REQ-AGENT"},
                    confirm=True,
                    idempotency_key="agent-artifact-req-1",
                    preview_log_id=preview["log_id"],
                    correlation_id=preview["correlation_id"],
                    expected_environment=preview["environment"],
                    expected_definition_snapshot_id=preview["definition_snapshot_id"],
                    expected_release_id=preview["release_id"],
                    expected_definition_hash=preview["definition_hash"],
                ),
                self.db,
            )

        self.assertEqual(response, final_response)
        self.db.expire_all()
        persisted = self.db.get(Message, message.id)
        stored = json.loads(persisted.tool_results[0]["result"])
        self.assertEqual(stored["status"], "success")
        self.assertEqual(stored["result"]["artifact"]["format"], "docx")

    def test_confirmation_is_blocked_until_agent_sse_message_is_final(self) -> None:
        agent = Agent(
            id="agent-stream-race",
            tenant_id="tenant-action-audit",
            scenario_id=self.scenario.id,
            name="流式确认 Agent",
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": [self.action.id]},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": []},
                "workflows": {"mode": "explicit", "selected_ids": []},
            },
        )
        conversation = Conversation(
            id="conversation-stream-race",
            agent=agent,
            created_by_user_id="user-action-audit",
            title="流式竞态",
        )
        message = Message(
            id="message-stream-race",
            conversation=conversation,
            role="assistant",
            content="正在生成最终说明。",
            tool_calls=[{"id": "call-race", "name": "execute_action"}],
            tool_results=[],
            stream_finalized=False,
        )
        self.db.add_all([agent, conversation, message])
        self.db.commit()
        self.db.info["llm_trace_context"] = {"assistant_message_id": message.id}
        self.db.info["action_audit_context"] = {
            "agent_id": agent.id,
            "llm_config_id": None,
            "model_name": "test-model",
        }
        preview = scenarios.execute_action(
            self.action.id,
            ActionExecuteRequest(params={"reference": "REQ-RACE"}, dry_run=True),
            self.db,
        )
        message.tool_results = [{
            "id": "call-race",
            "name": "execute_action",
            "result": json.dumps(preview, ensure_ascii=False),
        }]
        self.db.commit()

        payload = ActionExecuteRequest(
            params={"reference": "REQ-RACE"},
            confirm=True,
            idempotency_key="agent-race-1",
            preview_log_id=preview["log_id"],
            correlation_id=preview["correlation_id"],
            expected_environment=preview["environment"],
            expected_definition_snapshot_id=preview["definition_snapshot_id"],
            expected_release_id=preview["release_id"],
            expected_definition_hash=preview["definition_hash"],
        )
        with patch.object(scenarios.workflow_service, "execute_action") as execute:
            with self.assertRaises(HTTPException) as raised:
                scenarios.execute_action(self.action.id, payload, self.db)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("仍在生成", str(raised.exception.detail))
        execute.assert_not_called()
