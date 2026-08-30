from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    ActionExecutionLog,
    Agent,
    AssistantAttachment,
    AssistantCompilationJob,
    AssistantMessage,
    AssistantRouteDecision,
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
    OntologyWorkflow,
    Tenant,
    User,
)
from app.routers import agents, assistant, scenarios
from app.schemas import ActionExecuteRequest, AssistantChatRequest, AssistantProposalApplyRequest, ChatRequest
from app.services import (
    assistant_orchestrator,
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
    def _model_task(
        task_id: str,
        order: int,
        *,
        depends_on: list[str] | None = None,
        change_count: int = 1,
        safe_change_count: int | None = None,
        issues: list[dict] | None = None,
        generation_status: str = "generated",
    ) -> dict:
        safe_count = change_count if safe_change_count is None else safe_change_count
        task_issues = list(issues or [])
        return {
            "id": task_id,
            "order": order,
            "title": task_id,
            "description": f"{task_id} task",
            "sections": [task_id],
            "depends_on": list(depends_on or []),
            "status": "pending",
            "generation_status": generation_status,
            "change_keys": [f"{task_id}.{index}" for index in range(change_count)],
            "safe_change_keys": [
                f"{task_id}.{index}" for index in range(min(change_count, safe_count))
            ],
            "change_count": change_count,
            "safe_change_count": safe_count,
            "compiled_safe_change_count": safe_count,
            "blocked_issue_count": sum(
                item.get("blocking", True) is not False for item in task_issues
            ),
            "compiled_blocked_issue_count": sum(
                item.get("blocking", True) is not False for item in task_issues
            ),
            "draft_status": "generated" if change_count or task_issues else "empty",
            "issues": task_issues,
        }

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
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="采购数据库",
            type="postgres",
            config={},
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

        with patch.object(
            assistant.datasource_service,
            "list_tables",
            return_value=[{
                "name": "purchase_requests",
                "columns": [
                    {"name": "request_no", "type": "TEXT", "pk": True},
                    {"name": "amount", "type": "NUMERIC", "pk": False},
                ],
            }],
        ):
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
        legacy_proposal = json.loads(json.dumps(proposal))
        for key in (
            "tasks",
            "current_task_id",
            "execution_status",
            "execution_summary",
            "execution_revision",
            "next_action",
            "run_id",
        ):
            legacy_proposal["payload"].pop(key, None)
        legacy_proposal["status"] = "pending"
        legacy_proposal.pop("run_revision", None)
        thread, legacy_message = self._proposal_message(
            kind="scenario_model",
            proposal=legacy_proposal,
            scenario=scenario,
        )

        recovered = assistant.list_thread_messages(
            thread.id,
            scenario_id=scenario.id,
            path=f"/scenarios/{scenario.id}",
            db=self.db,
        )
        recovered_proposal = recovered[-1].proposal
        self.assertEqual(recovered_proposal["status"], "in_progress")
        self.assertEqual(recovered_proposal["payload"]["current_task_id"], "ontology")
        self.assertEqual(len(recovered_proposal["payload"]["tasks"]), 6)
        self.assertEqual(
            [item["id"] for item in recovered_proposal["payload"]["tasks"]],
            [
                "ontology",
                "instances",
                "mapping",
                "capabilities",
                "rules",
                "workflows",
            ],
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantMessage)),
            1,
        )
        with Session(self.engine) as reopened_db:
            reopened = reopened_db.get(AssistantMessage, legacy_message.id)
            self.assertEqual(reopened.proposal["status"], "in_progress")
            self.assertEqual(reopened.context["status"], "waiting_confirmation")
            self.assertEqual(
                reopened.proposal["payload"]["current_task_id"],
                "ontology",
            )
        proposal = recovered_proposal

        with self.assertRaises(HTTPException) as task_guard:
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="scenario_model",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    confirm=True,
                ),
                self.db,
            )
        self.assertEqual(task_guard.exception.status_code, 409)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
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
                task_id="ontology",
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
                task_id="ontology",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )

    def test_compound_scenario_model_can_apply_safe_subset_and_keep_blocker(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="农民工欠薪预警",
            namespace="wage-warning",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        source_bundle = scenario_model_compiler.build_source_bundle(
            "",
            [{
                "id": "wage-apply",
                "filename": "欠薪预警业务说明.md",
                "text": "项目以项目编号唯一标识。\n\n人员需要补充唯一编号后才能建模。",
            }],
        )
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            scenario,
            {
                "schema_version": "scenario_model.v1",
                "entities": [
                    {
                        "key": "entity.project",
                        "name": "项目",
                        "properties": [{
                            "name": "项目编号",
                            "data_type": "string",
                            "is_key": True,
                            "is_title": True,
                            "is_required": True,
                        }],
                        "evidence_refs": ["wage-apply:p0001"],
                        "confidence": 1.0,
                    },
                    {
                        "key": "entity.worker",
                        "name": "人员",
                        "properties": [{
                            "name": "姓名",
                            "data_type": "string",
                            "is_title": True,
                            "is_required": True,
                        }],
                        "evidence_refs": ["wage-apply:p0002"],
                        "confidence": 0.8,
                    },
                ],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "unresolved": [],
                "coverage": [
                    {
                        "source_ref": "wage-apply:p0001",
                        "status": "modeled",
                        "reason": "项目对象与主键",
                        "change_keys": ["entity.project"],
                    },
                    {
                        "source_ref": "wage-apply:p0002",
                        "status": "modeled",
                        "reason": "人员对象缺少唯一编号",
                        "change_keys": ["entity.worker"],
                    },
                ],
            },
            source_bundle=source_bundle,
        )
        proposal = assistant._build_proposal("scenario_model", payload, scenario)
        self.assertIn("entity.project", payload["applyability"]["safe_change_keys"])
        self.assertNotIn("entity.worker", payload["applyability"]["safe_change_keys"])
        self.assertIn("entity.worker", payload["applyability"]["blocked_change_keys"])
        thread, _message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                confirm=True,
            ),
            self.db,
        )
        self.assertTrue(result["data"]["partial"])
        self.assertEqual(result["data"]["safe_change_count"], 2)
        self.assertGreaterEqual(result["data"]["blocked_issue_count"], 1)
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
                task_id="ontology",
                confirm=True,
                allow_partial=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )
        saved = self.db.get(AssistantMessage, _message.id)
        self.assertEqual(saved.proposal["status"], "completed_with_gaps")

    def test_model_task_runner_always_exposes_one_action_or_a_final_summary(self) -> None:
        payload = {
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task(
                    "instances", 2, depends_on=["ontology"], change_count=0
                ),
                self._model_task("mapping", 3, depends_on=["ontology"]),
                self._model_task(
                    "capabilities", 4, depends_on=["ontology"], change_count=0
                ),
                self._model_task(
                    "rules",
                    5,
                    depends_on=["ontology", "capabilities"],
                    change_count=0,
                ),
                self._model_task(
                    "workflows",
                    6,
                    depends_on=["ontology", "capabilities", "rules"],
                    change_count=0,
                ),
            ],
            "unresolved": [],
        }

        first = assistant._refresh_model_task_states(payload)
        first_by_id = {item["id"]: item for item in first["tasks"]}
        self.assertEqual(first["current_task_id"], "ontology")
        self.assertEqual(first["execution_status"], "waiting_for_confirmation")
        self.assertEqual(first["next_action"]["type"], "confirm_task")
        self.assertEqual(first_by_id["mapping"]["status"], "waiting")
        self.assertEqual(first_by_id["capabilities"]["status"], "empty")

        second = assistant._refresh_model_task_states(
            first,
            applied_task_id="ontology",
            applied_status="applied",
        )
        self.assertEqual(second["current_task_id"], "mapping")
        self.assertEqual(second["next_action"]["task_id"], "mapping")

        final = assistant._refresh_model_task_states(
            second,
            applied_task_id="mapping",
            applied_status="applied",
        )
        self.assertEqual(final["current_task_id"], "")
        self.assertEqual(final["execution_status"], "completed")
        self.assertTrue(final["execution_summary"]["final"])
        self.assertEqual(final["next_action"]["type"], "refine_model")
        self.assertTrue(all(
            item["status"] in assistant._MODEL_TASK_TERMINAL_STATUSES
            for item in final["tasks"]
        ))

    def test_staged_model_task_waits_for_an_explicit_next_generation(self) -> None:
        payload = {
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task(
                    "instances",
                    2,
                    depends_on=["ontology"],
                    generation_status="pending",
                ),
                self._model_task(
                    "mapping",
                    3,
                    depends_on=["ontology"],
                    generation_status="pending",
                ),
                self._model_task(
                    "capabilities",
                    4,
                    depends_on=["ontology"],
                    generation_status="pending",
                ),
                self._model_task(
                    "rules",
                    5,
                    depends_on=["ontology", "capabilities"],
                    generation_status="pending",
                ),
                self._model_task(
                    "workflows",
                    6,
                    depends_on=["ontology", "capabilities", "rules"],
                    generation_status="pending",
                ),
            ],
            "unresolved": [],
        }

        initial = assistant._refresh_model_task_states(payload)
        self.assertEqual(initial["current_task_id"], "ontology")
        self.assertEqual(initial["next_action"]["type"], "confirm_task")

        after_ontology = assistant._refresh_model_task_states(
            initial,
            applied_task_id="ontology",
            applied_status="applied",
        )
        by_id = {item["id"]: item for item in after_ontology["tasks"]}
        self.assertEqual(after_ontology["execution_status"], "waiting_for_generation")
        self.assertEqual(after_ontology["current_task_id"], "instances")
        self.assertEqual(after_ontology["next_action"]["type"], "generate_task")
        self.assertFalse(after_ontology["next_action"]["requires_confirmation"])
        self.assertEqual(by_id["instances"]["status"], "awaiting_generation")
        self.assertEqual(by_id["mapping"]["status"], "waiting")

    def test_staged_merge_preserves_prior_stage_coverage_and_evidence(self) -> None:
        base = {
            section: [] for section in assistant._SCENARIO_MODEL_RESOURCE_SECTIONS
        }
        current = {
            **base,
            "entities": [{
                "key": "entity.project",
                "evidence_refs": ["brief:p0001"],
            }],
            "changes": [{"change_id": "entity.project", "operation": "add"}],
            "unresolved": [],
            "source_refs": ["brief:p0001", "brief:p0002"],
            "source_paragraph_count": 2,
            "coverage": [
                {
                    "source_ref": "brief:p0001",
                    "status": "modeled",
                    "reason": "项目对象定义",
                    "change_keys": ["entity.project"],
                },
                {
                    "source_ref": "brief:p0002",
                    "status": "context",
                    "reason": "留待后续实例任务",
                    "change_keys": [],
                },
            ],
            "generation": {"mode": "staged", "generated_task_ids": ["ontology"]},
        }
        stage = {
            **base,
            "instances": [{
                "key": "instance.project.001",
                "entity_ref": "entity.project",
                "evidence_refs": ["brief:p0001"],
            }],
            "changes": [],
            "unresolved": [],
            "source_refs": ["brief:p0001", "brief:p0002"],
            "source_paragraph_count": 2,
            "coverage": [
                {
                    "source_ref": "brief:p0001",
                    "status": "modeled",
                    "reason": "项目实例候选",
                    "change_keys": ["instance.project.001"],
                },
                {
                    "source_ref": "brief:p0002",
                    "status": "context",
                    "reason": "仍留待映射任务",
                    "change_keys": [],
                },
            ],
            "generation": {"mode": "staged", "generated_task_ids": ["instances"]},
        }

        merged = assistant._merge_staged_compilation_payload(
            current,
            stage,
            task_id="instances",
        )

        coverage = {item["source_ref"]: item for item in merged["coverage"]}
        self.assertEqual(
            coverage["brief:p0001"]["change_keys"],
            ["entity.project", "instance.project.001"],
        )
        self.assertEqual(coverage["brief:p0001"]["status"], "modeled")
        self.assertEqual(merged["coverage_summary"]["modeled"], 1)
        self.assertEqual(merged["generation"]["generated_task_ids"], ["ontology", "instances"])

        ontology_apply_payload = scenario_model_compiler.task_payload_for_apply(
            merged,
            "ontology",
        )
        ontology_coverage = {
            item["source_ref"]: item for item in ontology_apply_payload["coverage"]
        }
        self.assertEqual(ontology_coverage["brief:p0001"]["status"], "modeled")
        self.assertEqual(ontology_coverage["brief:p0001"]["change_keys"], ["entity.project"])

    def test_all_empty_model_tasks_finish_without_claiming_any_write(self) -> None:
        result = assistant._refresh_model_task_states({
            "tasks": [
                self._model_task(task_id, order, change_count=0)
                for order, task_id in enumerate(
                    ("ontology", "instances", "mapping", "capabilities", "rules", "workflows"),
                    start=1,
                )
            ],
            "unresolved": [],
        })

        summary = result["execution_summary"]
        self.assertTrue(summary["final"])
        self.assertEqual(result["execution_status"], "completed_no_changes")
        self.assertEqual(summary["status"], "completed_no_changes")
        self.assertEqual(summary["applied_task_count"], 0)
        self.assertEqual(summary["partially_applied_task_count"], 0)
        self.assertEqual(summary["empty_task_count"], 6)
        self.assertIn("没有正式定义写入当前场景", summary["message"])
        self.assertNotIn("已应用", summary["message"])

    def test_mapping_deferred_issues_are_grouped_by_reported_root_cause(self) -> None:
        issues = [
            {
                "code": "document_reported_issue",
                "reported_code": "MAPPING_DEFERRED_NO_DATA_SOURCE",
                "message": f"逻辑映射 {index} 尚未绑定物理数据源",
                "blocking": False,
                "source_refs": [f"mapping-brief:p{index:04d}"],
                "resolution_hint": "接入数据源后绑定现有逻辑映射。",
            }
            for index in range(1, 81)
        ]
        plan_payload = {
            section: [] for section in assistant._SCENARIO_MODEL_RESOURCE_SECTIONS
        }
        plan_payload.update({
            "conceptual_mappings": [{
                "key": "conceptual_mapping.unbound_project",
            }],
            "changes": [],
            "unresolved": issues,
        })
        tasks = scenario_model_compiler.build_model_task_plan(plan_payload)
        mapping_task = next(item for item in tasks if item["id"] == "mapping")
        self.assertEqual(mapping_task["output_count"], 1)
        self.assertEqual(mapping_task["draft_output_count"], 1)
        self.assertEqual(len(mapping_task["issues"]), 20)
        self.assertTrue(all(
            item["reported_code"] == "MAPPING_DEFERRED_NO_DATA_SOURCE"
            for item in mapping_task["issues"]
        ))

        result = assistant._refresh_model_task_states({
            **plan_payload,
            "tasks": tasks,
        })

        self.assertEqual(result["execution_status"], "waiting_for_confirmation")
        self.assertEqual(result["current_task_id"], "mapping")
        self.assertFalse(result["execution_summary"]["final"])

        # Draft-only work is no longer auto-accepted.  The explicit decision
        # records that no governed definition was written before the plan may
        # finish with gaps.
        result = assistant._refresh_model_task_states(
            result,
            applied_task_id="mapping",
            applied_status="drafted_with_gaps",
        )
        summary = result["execution_summary"]
        self.assertEqual(result["execution_status"], "completed_with_gaps")
        self.assertTrue(summary["final"])
        self.assertEqual(summary["total_task_count"], 6)
        self.assertEqual(summary["completed_task_count"], 6)
        self.assertEqual(summary["remaining_issue_count"], 80)
        self.assertEqual(summary["remaining_issue_group_count"], 1)
        self.assertEqual(len(summary["issue_groups"]), 1)
        self.assertEqual(summary["issue_groups"][0]["code"], "DATA_SOURCE_DEPENDENCY")
        self.assertEqual(summary["issue_groups"][0]["count"], 80)
        self.assertEqual(summary["issue_groups"][0]["blocking_count"], 0)
        self.assertTrue(summary["issue_groups"][0]["requires_followup"])

    def test_zero_safe_blocker_preserves_all_drafts_and_finishes_with_gaps(self) -> None:
        blocker = {
            "code": "MISSING_REQUIRED_PROPERTY",
            "message": "缺少项目主键定义",
            "blocking": True,
            "source_refs": ["brief:p0001"],
            "resolution_hint": "确认项目编号字段后重新编译。",
        }
        payload = {
            "tasks": [
                self._model_task(
                    "ontology",
                    1,
                    safe_change_count=0,
                    issues=[blocker],
                ),
                self._model_task(
                    "instances", 2, depends_on=["ontology"], change_count=0
                ),
                self._model_task("mapping", 3, depends_on=["ontology"]),
                self._model_task("capabilities", 4, change_count=0),
                self._model_task("rules", 5, change_count=0),
                self._model_task("workflows", 6, change_count=0),
            ],
            "unresolved": [blocker],
        }

        result = assistant._refresh_model_task_states(payload)
        by_id = {item["id"]: item for item in result["tasks"]}
        self.assertEqual(by_id["ontology"]["status"], "blocked")
        self.assertEqual(by_id["mapping"]["status"], "waiting")
        self.assertEqual(by_id["capabilities"]["status"], "empty")
        self.assertEqual(result["current_task_id"], "ontology")
        self.assertFalse(result["execution_summary"]["final"])

        result = assistant._refresh_model_task_states(
            result,
            applied_task_id="ontology",
            applied_status="drafted_with_gaps",
        )
        self.assertEqual(result["current_task_id"], "mapping")
        result = assistant._refresh_model_task_states(
            result,
            applied_task_id="mapping",
            applied_status="drafted_with_gaps",
        )
        self.assertEqual(result["execution_status"], "completed_with_gaps")
        self.assertTrue(result["execution_summary"]["final"])
        self.assertGreaterEqual(result["execution_summary"]["remaining_issue_count"], 1)
        self.assertIn(
            "确认项目编号字段后重新编译。",
            result["execution_summary"]["resolution_hints"],
        )
        self.assertEqual(result["next_action"]["type"], "refine_model")

    def test_malformed_task_identity_or_dependency_finishes_as_recoverable_draft(self) -> None:
        duplicate = assistant._refresh_model_task_states({
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task("ontology", 2),
            ],
            "unresolved": [],
        })
        self.assertEqual(duplicate["execution_status"], "completed_with_gaps")
        self.assertEqual(duplicate["current_task_id"], "")
        self.assertTrue(duplicate["execution_summary"]["final"])
        self.assertEqual(duplicate["next_action"]["type"], "refine_model")
        self.assertTrue(all(
            item["status"] == "drafted_with_gaps" for item in duplicate["tasks"]
        ))
        self.assertTrue(any(
            item["code"] == "INVALID_TASK_PLAN"
            for item in duplicate["execution_summary"]["remaining_issues"]
        ))

        cycle = assistant._refresh_model_task_states({
            "tasks": [
                self._model_task("ontology", 1, depends_on=["mapping"]),
                self._model_task("mapping", 2, depends_on=["ontology"]),
            ],
            "unresolved": [],
        })
        self.assertEqual(cycle["execution_status"], "completed_with_gaps")
        self.assertEqual(cycle["current_task_id"], "")
        self.assertTrue(cycle["execution_summary"]["final"])
        self.assertTrue(any(
            item["code"] == "INVALID_TASK_DEPENDENCY"
            for item in cycle["execution_summary"]["remaining_issues"]
        ))

    def test_malformed_scalar_lifecycle_fields_finish_with_a_recoverable_summary(self) -> None:
        result = assistant._refresh_model_task_states({
            "tasks": "not-a-task-list",
            "current_task_id": 7,
            "execution_status": ["running"],
            "execution_summary": "not-a-summary",
            "execution_revision": "NaN",
            "next_action": 42,
            "unresolved": "not-an-issue-list",
            "changes": [],
        })

        self.assertEqual(result["execution_status"], "completed_with_gaps")
        self.assertEqual(result["current_task_id"], "")
        self.assertTrue(result["execution_summary"]["final"])
        self.assertEqual(result["execution_revision"], 1)
        self.assertEqual(result["next_action"]["type"], "refine_model")
        self.assertTrue(any(
            issue["code"] == "INVALID_TASK_PLAN"
            for issue in result["execution_summary"]["remaining_issues"]
        ))

    def test_malformed_task_scalar_fields_are_lazily_persisted_as_gaps(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="畸形任务字段恢复",
            namespace="malformed-task-scalars",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        malformed_task = self._model_task("ontology", 1)
        malformed_task.update({
            "depends_on": 7,
            "issues": 42,
            "change_count": "NaN",
            "safe_change_count": "NaN",
            "compiled_safe_change_count": "NaN",
        })
        proposal = {
            "proposal_id": "malformed-task-scalars-proposal",
            "kind": "scenario_model",
            "title": "畸形任务字段",
            "summary": "历史生命周期 JSON 含标量漂移",
            "payload": {
                "schema_version": "scenario_model.v1",
                "tasks": [malformed_task],
                "current_task_id": 7,
                "execution_status": "running",
                "execution_summary": "not-a-summary",
                "execution_revision": "NaN",
                "next_action": 42,
                "unresolved": 99,
                "changes": [],
            },
            "changes": [],
            "base_snapshot": assistant._scenario_snapshot(scenario),
            "requires_confirmation": True,
            "status": "in_progress",
        }
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        recovered = assistant.list_thread_messages(
            thread.id,
            scenario_id=scenario.id,
            path=f"/scenarios/{scenario.id}",
            db=self.db,
        )[-1].proposal

        self.assertEqual(recovered["status"], "completed_with_gaps")
        self.assertEqual(
            recovered["payload"]["execution_status"],
            "completed_with_gaps",
        )
        self.assertEqual(recovered["payload"]["current_task_id"], "")
        self.assertTrue(recovered["payload"]["execution_summary"]["final"])
        self.assertEqual(recovered["payload"]["next_action"]["type"], "refine_model")
        with Session(self.engine) as reopened_db:
            saved = reopened_db.get(AssistantMessage, message.id)
            self.assertEqual(
                saved.proposal["payload"]["execution_status"],
                "completed_with_gaps",
            )
            self.assertEqual(saved.context["status"], "no_changes")

    def test_complete_looking_deadlock_is_lazily_repaired_to_one_current_task(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="无当前任务的旧计划",
            namespace="deadlocked-complete-shape",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        proposal = {
            "proposal_id": "deadlocked-complete-shape-proposal",
            "kind": "scenario_model",
            "title": "外形完整但无法继续的计划",
            "summary": "final=false 但 current_task_id 为空",
            "payload": {
                "schema_version": "scenario_model.v1",
                "tasks": [
                    self._model_task("ontology", 1),
                    self._model_task(
                        "mapping",
                        2,
                        depends_on=["ontology"],
                        change_count=0,
                    ),
                ],
                "current_task_id": "",
                "execution_status": "waiting_for_confirmation",
                "execution_summary": {
                    "final": False,
                    "status": "waiting_for_confirmation",
                    "current_task_id": "",
                },
                "execution_revision": 6,
                "next_action": {
                    "type": "confirm_task",
                    "task_id": "",
                    "requires_confirmation": True,
                },
                "unresolved": [],
                "changes": [],
            },
            "changes": [],
            "base_snapshot": assistant._scenario_snapshot(scenario),
            "requires_confirmation": True,
            "status": "in_progress",
        }
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        recovered = assistant.list_thread_messages(
            thread.id,
            scenario_id=scenario.id,
            path=f"/scenarios/{scenario.id}",
            db=self.db,
        )[-1].proposal

        self.assertEqual(recovered["payload"]["current_task_id"], "ontology")
        self.assertEqual(
            recovered["payload"]["execution_status"],
            "waiting_for_confirmation",
        )
        self.assertFalse(recovered["payload"]["execution_summary"]["final"])
        self.assertEqual(recovered["payload"]["next_action"]["task_id"], "ontology")
        self.assertEqual(recovered["run_revision"], 7)
        with Session(self.engine) as reopened_db:
            saved = reopened_db.get(AssistantMessage, message.id)
            self.assertEqual(saved.proposal["payload"]["current_task_id"], "ontology")
            self.assertEqual(saved.context["run_revision"], 7)

    def test_transitional_task_plan_is_upgraded_with_summary_and_next_action(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="过渡任务计划",
            namespace="transitional-task-plan",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        transitional_payload = {
            "schema_version": "scenario_model.v1",
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task(
                    "mapping", 2, depends_on=["ontology"], change_count=0
                ),
            ],
            "current_task_id": "ontology",
            "execution_status": "ready",
            "unresolved": [],
            "changes": [],
        }
        proposal = {
            "proposal_id": "transitional-task-proposal",
            "kind": "scenario_model",
            "title": "旧版任务清单",
            "summary": "已有任务但缺少持续执行元数据",
            "payload": transitional_payload,
            "changes": [],
            "base_snapshot": assistant._scenario_snapshot(scenario),
            "requires_confirmation": True,
            "status": "in_progress",
        }
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        recovered = assistant.list_thread_messages(
            thread.id,
            scenario_id=scenario.id,
            path=f"/scenarios/{scenario.id}",
            db=self.db,
        )[-1].proposal

        self.assertEqual(recovered["payload"]["current_task_id"], "ontology")
        self.assertEqual(
            recovered["payload"]["execution_status"],
            "waiting_for_confirmation",
        )
        self.assertFalse(recovered["payload"]["execution_summary"]["final"])
        self.assertEqual(recovered["payload"]["next_action"]["type"], "confirm_task")
        self.assertEqual(recovered["payload"]["next_action"]["task_id"], "ontology")
        self.db.refresh(message)
        self.assertEqual(message.context["status"], "waiting_confirmation")

    def test_defer_task_persists_final_summary_instead_of_ending_silently(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="保留草稿计划",
            namespace="deferred-draft-plan",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        model_payload = assistant._refresh_model_task_states({
            "schema_version": "scenario_model.v1",
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task(
                    "instances", 2, depends_on=["ontology"], change_count=0
                ),
                self._model_task("mapping", 3, depends_on=["ontology"]),
                self._model_task("capabilities", 4, change_count=0),
                self._model_task("rules", 5, change_count=0),
                self._model_task("workflows", 6, change_count=0),
            ],
            "unresolved": [],
            "changes": [],
        })
        proposal = {
            "proposal_id": "deferred-draft-proposal",
            "kind": "scenario_model",
            "title": "持续建模计划",
            "summary": "测试保留草稿后继续推进",
            "payload": model_payload,
            "changes": [],
            "base_snapshot": assistant._scenario_snapshot(scenario),
            "requires_confirmation": True,
            "status": "in_progress",
        }
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        with self.assertRaises(HTTPException) as blocked:
            assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="scenario_model",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    task_id="mapping",
                    confirm=True,
                ),
                self.db,
            )
        self.assertEqual(blocked.exception.status_code, 409)

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                task_action="defer",
                confirm=True,
            ),
            self.db,
        )

        self.assertEqual(result["data"]["task_status"], "deferred")
        self.assertFalse(result["execution_summary"]["final"])
        self.assertEqual(result["execution_summary"]["status"], "waiting_for_confirmation")
        self.assertEqual(result["next_action"]["task_id"], "mapping")

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="mapping",
                task_action="defer",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(result["data"]["task_status"], "deferred")
        self.assertTrue(result["execution_summary"]["final"])
        self.assertEqual(result["execution_summary"]["status"], "completed_with_gaps")
        self.assertEqual(result["next_action"]["type"], "refine_model")
        saved = self.db.get(AssistantMessage, message.id)
        self.assertEqual(saved.proposal["status"], "completed_with_gaps")
        self.assertEqual(saved.proposal["payload"]["current_task_id"], "")
        self.assertEqual(saved.context["status"], "no_changes")
        self.assertIn("全部 6 项任务均已推进", saved.content)
        self.assertIn("没有正式定义写入当前场景", saved.content)
        self.assertIn("可继续优化的草稿", saved.content)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )

        replay = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                task_action="defer",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            replay["proposal"]["payload"]["execution_status"],
            "completed_with_gaps",
        )

    def test_compound_scenario_model_applies_one_task_and_replays_that_task(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="任务化建模",
            namespace="task-model",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        source_bundle = scenario_model_compiler.build_source_bundle(
            "",
            [{
                "id": "task-model-apply",
                "filename": "任务化建模.md",
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
                        "is_title": True,
                        "is_required": True,
                    }],
                    "evidence_refs": ["task-model-apply:p0001"],
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
                    "source_ref": "task-model-apply:p0001",
                    "status": "modeled",
                    "reason": "项目对象与主键",
                    "change_keys": ["entity.project"],
                }],
            },
            source_bundle=source_bundle,
        )
        proposal = assistant._build_proposal("scenario_model", payload, scenario)
        ontology_task = next(item for item in proposal["payload"]["tasks"] if item["id"] == "ontology")
        self.assertEqual(ontology_task["status"], "ready")
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )

        result = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(result["data"]["task_status"], "applied")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )
        saved = self.db.get(AssistantMessage, message.id)
        self.assertEqual(saved.proposal["payload"]["current_task_id"], "")
        self.assertEqual(saved.proposal["payload"]["execution_status"], "completed")
        self.assertTrue(saved.proposal["payload"]["execution_summary"]["final"])
        self.assertEqual(saved.proposal["status"], "applied")
        self.assertIn("全部 6 项任务均已推进", saved.content)
        self.assertIn("1 项任务的正式定义已写入当前场景", saved.content)
        self.assertEqual(
            next(item for item in saved.proposal["payload"]["tasks"] if item["id"] == "ontology")["status"],
            "applied",
        )
        self.assertTrue(all(
            item["status"] in {"applied", "empty"}
            for item in saved.proposal["payload"]["tasks"]
        ))

        replay = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["proposal"]["payload"]["execution_status"], "completed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )

        scenario.description = "任务应用后由另一个请求补充了场景说明"
        self.db.commit()
        replay_after_external_change = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                confirm=True,
            ),
            self.db,
        )
        self.assertEqual(replay_after_external_change["status"], "replayed")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            1,
        )

    def test_scenario_model_without_task_id_is_rejected_even_for_an_empty_plan(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="拒绝整体应用",
            namespace="reject-whole-model-apply",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        active_payload = assistant._refresh_model_task_states({
            "schema_version": "scenario_model.v1",
            "tasks": [self._model_task("ontology", 1)],
            "unresolved": [],
            "changes": [],
        })
        proposals = [
            {
                "proposal_id": "taskful-model-without-task-id",
                "kind": "scenario_model",
                "title": "仍有当前任务的计划",
                "summary": "必须逐项确认",
                "payload": active_payload,
                "changes": [],
                "base_snapshot": assistant._scenario_snapshot(scenario),
                "requires_confirmation": True,
                "status": "in_progress",
            },
            {
                "proposal_id": "empty-model-without-task-id",
                "kind": "scenario_model",
                "title": "历史空任务计划",
                "summary": "即使为空也不能退回旧整体应用路径",
                "payload": {
                    "schema_version": "scenario_model.v1",
                    "tasks": [],
                    "unresolved": [],
                    "changes": [],
                },
                "changes": [],
                "base_snapshot": assistant._scenario_snapshot(scenario),
                "requires_confirmation": False,
                "status": "applied",
                "apply_result": {"kind": "scenario_model"},
            },
        ]

        for proposal in proposals:
            with self.subTest(proposal_id=proposal["proposal_id"]):
                thread, _message = self._proposal_message(
                    kind="scenario_model",
                    proposal=proposal,
                    scenario=scenario,
                )
                with self.assertRaises(HTTPException) as rejected:
                    assistant.apply_proposal(
                        AssistantProposalApplyRequest(
                            kind="scenario_model",
                            scenario_id=scenario.id,
                            thread_id=thread.id,
                            proposal_id=proposal["proposal_id"],
                            confirm=True,
                        ),
                        self.db,
                    )
                self.assertEqual(rejected.exception.status_code, 409)

    def test_claim_conflict_replay_returns_latest_run_revision_and_current_task(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="并发任务重放",
            namespace="concurrent-task-replay",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        initial_payload = assistant._refresh_model_task_states({
            "schema_version": "scenario_model.v1",
            "tasks": [
                self._model_task("ontology", 1),
                self._model_task("mapping", 2),
            ],
            "unresolved": [],
            "changes": [],
        })
        proposal = {
            "proposal_id": "concurrent-task-replay-proposal",
            "kind": "scenario_model",
            "title": "并发任务计划",
            "summary": "第二个请求必须读取第一个请求提交后的状态",
            "payload": initial_payload,
            "changes": [],
            "base_snapshot": assistant._scenario_snapshot(scenario),
            "requires_confirmation": True,
            "status": "in_progress",
            "run_revision": initial_payload["execution_revision"],
        }
        thread, message = self._proposal_message(
            kind="scenario_model",
            proposal=proposal,
            scenario=scenario,
        )
        latest_payload = assistant._refresh_model_task_states(
            initial_payload,
            applied_task_id="ontology",
            applied_status="deferred",
        )
        latest_proposal = json.loads(json.dumps(proposal))
        latest_proposal.update({
            "payload": latest_payload,
            "run_revision": latest_payload["execution_revision"],
            "requires_confirmation": True,
            "status": "in_progress",
            "apply_result": {
                "kind": "scenario_model",
                "task_id": "ontology",
                "task_status": "deferred",
                "run_revision": latest_payload["execution_revision"],
            },
        })
        claim_result = {
            "kind": "scenario_model",
            "task_id": "ontology",
            "task_status": "deferred",
            "run_revision": latest_payload["execution_revision"],
        }

        def lose_claim_to_committed_request(*_args, **_kwargs):
            with Session(self.engine) as concurrent_db:
                concurrent_message = concurrent_db.get(AssistantMessage, message.id)
                concurrent_message.proposal = latest_proposal
                concurrent_db.commit()
            return SimpleNamespace(status="applied", result=claim_result), False

        with patch.object(
            assistant,
            "_claim_proposal_application",
            side_effect=lose_claim_to_committed_request,
        ):
            replay = assistant.apply_proposal(
                AssistantProposalApplyRequest(
                    kind="scenario_model",
                    scenario_id=scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    task_id="ontology",
                    task_action="defer",
                    confirm=True,
                ),
                self.db,
            )

        self.assertEqual(replay["status"], "replayed")
        self.assertGreaterEqual(
            replay["proposal"]["run_revision"],
            latest_payload["execution_revision"],
        )
        self.assertEqual(
            replay["proposal"]["run_revision"],
            replay["proposal"]["payload"]["execution_revision"],
        )
        self.assertEqual(
            replay["proposal"]["payload"]["current_task_id"],
            "mapping",
        )
        self.assertEqual(replay["execution_summary"]["current_task_id"], "mapping")
        self.assertEqual(replay["next_action"]["task_id"], "mapping")

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
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="订单源",
            type="postgres",
            config={},
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
        with (
            patch.object(
                assistant.datasource_service,
                "list_tables",
                return_value=[{
                    "name": "orders",
                    "columns": [{
                        "name": "order_no", "type": "TEXT", "pk": True,
                    }],
                }],
            ),
            self.assertRaisesRegex(ValueError, "不存在的源字段"),
        ):
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
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=scenario.id,
            name="订单源",
            type="postgres",
            config={},
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

        with patch.object(
            assistant.datasource_service,
            "list_tables",
            return_value=[{
                "name": "orders",
                "columns": [{
                    "name": "order_no", "type": "TEXT", "pk": True,
                }],
            }],
        ):
            updated, operation = assistant._apply_mapping_draft(
                self.db, scenario, data
            )
            self.assertEqual(operation, "update")
            self.assertEqual(updated.transform_rules, {"订单号": [{"op": "trim"}]})

            data["transform_rules"] = {"订单号": [{"op": "upper"}]}
            updated, _operation = assistant._apply_mapping_draft(
                self.db, scenario, data
            )
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

    def test_invalid_workflow_reference_returns_422_and_writes_nothing(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="无正式操作场景",
            status="draft",
        )
        self.db.add(scenario)
        self.db.commit()
        proposal = assistant._build_proposal(
            "workflow",
            {
                "name": "包含虚构操作的流程",
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
            scenario,
        )
        thread, _message = self._proposal_message(
            kind="workflow",
            proposal=proposal,
            scenario=scenario,
        )

        with self.assertRaises(HTTPException) as rejected:
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

        self.assertEqual(rejected.exception.status_code, 422)
        self.assertIn("没有可引用的正式操作", str(rejected.exception.detail))
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyWorkflow)),
            0,
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
        llm = LLMConfig(
            tenant_id=self.tenant.id,
            name="测试模型",
            provider="openai",
            model="test-model",
            api_key="test-key",
            enabled=True,
            is_default=True,
        )
        self.db.add_all([scenario, function, llm])
        self.db.commit()
        tenant_id = self.tenant.id
        user_id = self.user.id
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        observed: dict[str, object] = {}
        route_plan = assistant_orchestrator.AssistantRoutePlan(
            intent="scenario_model",
            decision=assistant_orchestrator.AssistantSemanticDecision(
                goal="create",
                scope="scenario_model",
                confidence="high",
                reason="用户明确要求编译完整业务模型",
            ),
            source="model",
        )

        def fake_compile(db, streamed_scenario, **_kwargs):
            # This relationship was not touched by _scenario_context.  It can
            # only load here when the SSE turn reloaded the scenario in its
            # explicitly-owned session.
            observed["functions"] = [
                item.name for item in streamed_scenario.function_definitions
            ]
            observed["tenant_id"] = db.info.get("tenant_id")
            observed["user_id"] = db.info.get("user_id")
            observed["task_scope"] = _kwargs.get("task_scope")
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
                assistant,
                "_request_route_plan",
                return_value=route_plan,
            ),
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

            # Keep the compiler patch active while the background worker
            # reaches the provider call; the request returns the job handle
            # before this durable result exists.
            deadline = time.monotonic() + 10
            job = None
            while time.monotonic() < deadline:
                self.db.expire_all()
                job = self.db.execute(
                    select(AssistantCompilationJob)
                    .order_by(AssistantCompilationJob.created_at.desc())
                ).scalars().first()
                if job and job.status in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)

        self.assertNotIn("not bound to a Session", body)
        self.assertIn('"type": "compilation_job"', body)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, "succeeded", job.error)
        message = self.db.get(AssistantMessage, job.message_id)
        self.assertIsNotNone(message)
        self.assertEqual(message.proposal.get("kind"), "scenario_model")
        self.assertEqual(observed["functions"], ["计算风险分"])
        self.assertEqual(observed["tenant_id"], tenant_id)
        self.assertEqual(observed["user_id"], user_id)
        self.assertEqual(observed["task_scope"], "")
        self.assertTrue(
            {"ontology", "mapping", "rules", "review", "result"}
            .issubset({item["id"] for item in (job.progress.get("steps") or [])})
        )

    def test_workflow_count_question_never_generates_a_workflow_proposal(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="薪莫愁",
            status="active",
        )
        llm = LLMConfig(
            tenant_id=self.tenant.id,
            name="语义路由测试模型",
            provider="openai",
            model="test-model",
            api_key="test-key",
            capabilities=["chat", "tool"],
            enabled=True,
            is_default=True,
        )
        self.db.add_all([scenario, llm])
        self.db.commit()
        route_response = {
            "content": "",
            "tool_calls": [{
                "id": "call-route",
                "type": "function",
                "function": {
                    "name": "answer_question",
                    "arguments": {
                        "goal": "answer",
                        "confidence": "high",
                        "reason": "用户正在询问已有工作流数量",
                    },
                },
            }],
        }

        def chat_side_effect(*args, **kwargs):
            if kwargs.get("operation") == "assistant_route":
                return route_response
            return {"content": "当前业务场景有 0 个正式工作流。"}

        with (
            patch.object(
                assistant.llm_service,
                "chat",
                side_effect=chat_side_effect,
            ) as model_chat,
            patch.object(
                assistant.workflow_service,
                "generate_workflow",
            ) as generate_workflow,
        ):
            reply = assistant.chat(
                AssistantChatRequest(
                    message="当前业务场景有多少个工作流？",
                    request_id="workflow-count-question",
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="draft",
                    draft_kind="workflow",
                ),
                self.db,
            )
            replay = assistant.chat(
                AssistantChatRequest(
                    message="当前业务场景有多少个工作流？",
                    request_id="workflow-count-question",
                    thread_id=reply.thread_id,
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="draft",
                    draft_kind="workflow",
                ),
                self.db,
            )

        self.assertEqual(reply.reply, "当前业务场景有 0 个正式工作流。")
        self.assertEqual(replay.reply, reply.reply)
        self.assertEqual(replay.thread_id, reply.thread_id)
        self.assertEqual(reply.proposal, {})
        self.assertEqual(
            sum(
                1
                for call in model_chat.call_args_list
                if call.kwargs.get("operation") == "assistant_route"
            ),
            1,
        )
        self.assertEqual(model_chat.call_count, 3)
        generate_workflow.assert_not_called()
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantRouteDecision)),
            1,
        )
        with self.assertRaises(HTTPException) as conflict:
            assistant.chat(
                AssistantChatRequest(
                    message="请创建一个工作流",
                    request_id="workflow-count-question",
                    thread_id=reply.thread_id,
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="draft",
                    draft_kind="workflow",
                ),
                self.db,
            )
        self.assertEqual(conflict.exception.status_code, 409)
        saved_users = self.db.execute(
            select(AssistantMessage)
            .where(
                AssistantMessage.thread_id == reply.thread_id,
                AssistantMessage.role == "user",
            )
        ).scalars().all()
        self.assertEqual(len(saved_users), 2)
        self.assertTrue(all(
            saved_user.context["routing"]["intent"] == "chat"
            and saved_user.context["routing"]["goal"] == "answer"
            for saved_user in saved_users
        ))

    def test_route_failure_stops_before_sync_chat_or_model_generation(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="薪莫愁",
            status="active",
        )
        llm = LLMConfig(
            tenant_id=self.tenant.id,
            name="失败语义路由模型",
            provider="openai",
            model="test-model",
            api_key="test-key",
            capabilities=["chat", "tool"],
            enabled=True,
            is_default=True,
        )
        self.db.add_all([scenario, llm])
        self.db.commit()

        with (
            patch.object(
                assistant.llm_service,
                "chat",
                side_effect=lambda *args, **kwargs: (
                    (_ for _ in ()).throw(TimeoutError("route timeout"))
                    if kwargs.get("operation") == "assistant_route"
                    else {"content": "已由普通回答链路恢复"}
                ),
            ) as ordinary_chat,
            patch.object(assistant.scenario_model_compiler, "compile_scenario_model") as compile_model,
        ):
            reply = assistant.chat(
                AssistantChatRequest(
                    message="请根据附件完成整个业务场景建模，做好后让我确认应用",
                    request_id="route-failure-sync",
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="ask",
                ),
                self.db,
            )

        self.assertEqual(ordinary_chat.call_count, 2)
        compile_model.assert_not_called()
        self.assertEqual(reply.proposal, {})
        self.assertEqual(reply.reply, "已由普通回答链路恢复")
        saved = self.db.execute(
            select(AssistantMessage)
            .where(
                AssistantMessage.thread_id == reply.thread_id,
                AssistantMessage.role == "assistant",
            )
            .order_by(AssistantMessage.created_at.desc())
        ).scalars().first()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.proposal, {})
        self.assertEqual(saved.context["routing"]["source"], "model_fallback")
        self.assertTrue(saved.context["routing"]["recovered"])

    def test_historic_route_fallback_content_is_serialized_from_route_evidence(self) -> None:
        thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scope_key="scenario:global|path:/scenarios",
            title="历史路由失败",
        )
        self.db.add(thread)
        self.db.flush()
        raw_content = "场景建模已完成并应用。"
        message = AssistantMessage(
            thread_id=thread.id,
            role="assistant",
            content=raw_content,
            context={
                "routing": {
                    "intent": "chat",
                    "source": "model_fallback",
                    "goal": "answer",
                    "scope": "general",
                    "confidence": "low",
                },
                "evidence": {"uncertainties": ["保持只读"]},
            },
            proposal={},
        )
        self.db.add(message)
        self.db.commit()

        public = assistant._assistant_message_out(self.db, thread, message)

        self.assertIn("语义规划没有完成", public.content)
        self.assertIn("没有生成、应用或保存任何变更", public.content)
        self.assertNotIn(raw_content, public.content)
        self.assertEqual(public.context["status"], "route_fallback")
        self.assertEqual(public.proposal, {})
        self.assertEqual(message.content, raw_content)

    def test_route_failure_recovers_into_streaming_chat(self) -> None:
        scenario = BusinessScenario(
            tenant_id=self.tenant.id,
            name="薪莫愁流式",
            status="active",
        )
        llm = LLMConfig(
            tenant_id=self.tenant.id,
            name="失败流式语义路由模型",
            provider="openai",
            model="test-model",
            api_key="test-key",
            capabilities=["chat", "tool"],
            enabled=True,
            is_default=True,
        )
        self.db.add_all([scenario, llm])
        self.db.commit()
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

        with (
            patch.object(assistant, "SessionLocal", factory),
            patch.object(
                assistant.llm_service,
                "chat",
                side_effect=TimeoutError("route timeout"),
            ),
            patch.object(
                assistant.llm_service,
                "chat_stream",
                return_value=iter([{"type": "token", "content": "已恢复回答"}]),
            ) as ordinary_chat_stream,
            patch.object(assistant.scenario_model_compiler, "compile_scenario_model") as compile_model,
        ):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="请完成当前业务场景建模并让我确认应用",
                    request_id="route-failure-stream",
                    scenario_id=scenario.id,
                    path=f"/scenarios/{scenario.id}",
                    mode="ask",
                ),
                self.db,
            )
            body = asyncio.run(self._consume_until(response))

        ordinary_chat_stream.assert_called_once()
        compile_model.assert_not_called()
        self.assertIn("已恢复回答", body)
        saved = self.db.execute(
            select(AssistantMessage)
            .where(
                AssistantMessage.role == "assistant",
                AssistantMessage.content.contains("已恢复回答"),
            )
            .order_by(AssistantMessage.created_at.desc())
        ).scalars().first()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.proposal, {})
        self.assertEqual(saved.context["routing"]["source"], "model_fallback")
        self.assertTrue(saved.context["routing"]["recovered"])

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
            type="postgres",
            config={},
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
