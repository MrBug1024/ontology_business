from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AssistantAttachment,
    AssistantCompilationJob,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    LLMConfig,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import assistant
from app.schemas import (
    AssistantChatRequest,
    AssistantCompilationGuidanceRequest,
    AssistantModelTaskContinuationRequest,
)
from app.services import (
    assistant_compilation_job_service as job_service,
    llm_service,
    permission_service,
    scenario_model_compiler,
)
from tests.postgresql_migration_contracts import (
    baseline_table_ddl,
    render_postgresql_upgrade,
)


class AssistantCompilationJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp_dir.name) / "compilation-jobs.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.factory()
        self.tenant = Tenant(id="tenant-compilation-job", name="编译任务租户")
        self.user = User(
            id="user-compilation-job",
            tenant_id=self.tenant.id,
            email="compilation-job@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-compilation-job",
            tenant_id=self.tenant.id,
            name="建筑项目管理",
            status="active",
        )
        self.llm = LLMConfig(
            id="llm-compilation-job",
            tenant_id=self.tenant.id,
            name="测试模型",
            provider="openai",
            base_url="https://model.example.test/v1",
            api_key="test-only",
            model="test-model",
            is_default=True,
            enabled=True,
            capabilities=["chat"],
        )
        self.db.add_all([self.tenant, self.user, self.scenario, self.llm])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id
        self.route_patcher = patch.object(
            assistant,
            "_request_route_plan",
            return_value=assistant.assistant_orchestrator.AssistantRoutePlan(
                intent="scenario_model",
                decision=assistant.assistant_orchestrator.AssistantSemanticDecision(
                    goal="create",
                    scope="scenario_model",
                    confidence="high",
                    reason="测试明确要求完整场景建模",
                ),
                source="model",
            ),
        )
        self.route_patcher.start()
        self.addCleanup(self.route_patcher.stop)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _identity(
        self,
        *,
        message: str = "编译完整业务模型",
        attachment_text: str = "项目、合同与审批规则",
        baseline: str = "baseline-v1",
        request_id: str = "",
        model: str = "test-model",
        compiler_version: str = scenario_model_compiler.COMPILER_VERSION,
        mapping_context: str = "mapping-context-v1",
        call_budget: int = 4,
        assistant_scope_key: str = "scenario:test|path:/default",
    ) -> job_service.CompilationIdentity:
        return job_service.build_compilation_identity(
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            message=message,
            attachments=[{
                "filename": "domain.txt",
                "mime": "text/plain",
                "status": "parsed",
                "content_hash": job_service._sha256(attachment_text),
                "parsed_text": attachment_text,
                "error": "",
            }],
            llm=SimpleNamespace(
                id=self.llm.id,
                connector_revision=1,
                provider="openai",
                base_url="https://model.example.test/v1",
                model=model,
                temperature=0.1,
                max_tokens=20_000,
                capabilities=["chat"],
                enabled=True,
            ),
            compiler_version=compiler_version,
            scenario_baseline=baseline,
            request_id=request_id,
            mapping_context_fingerprint=mapping_context,
            execution_policy={
                "llm_call_budget": call_budget,
                "request_timeout": 600.0,
                "assistant_scope_key": assistant_scope_key,
            },
        )

    def _claim_fixture(self) -> tuple[str, str]:
        thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scenario_id=self.scenario.id,
            scope_key=f"scenario:{self.scenario.id}|path:/scenarios/{self.scenario.id}",
            title="并发编译",
        )
        self.db.add(thread)
        self.db.flush()
        message = AssistantMessage(
            thread_id=thread.id,
            role="user",
            content="编译完整业务模型",
        )
        self.db.add(message)
        self.db.commit()
        return thread.id, message.id

    def _claim(self, identity, thread_id: str, message_id: str):
        with self.factory() as db:
            return job_service.claim_compilation(
                db,
                identity=identity,
                tenant_id=self.tenant.id,
                user_id=self.user.id,
                scenario_id=self.scenario.id,
                thread_id=thread_id,
                message_id=message_id,
                compiler_version=scenario_model_compiler.COMPILER_VERSION,
                scenario_baseline="baseline-v1",
                llm_call_budget=4,
            )

    def test_quiet_compilation_stream_emits_public_liveness_event(self) -> None:
        thread_id, message_id = self._claim_fixture()
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id="stream-liveness"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
        )
        self.assertTrue(acquired)
        job.started_at = job.started_at - timedelta(seconds=65)
        job.progress = {
            "phase": "ontology",
            "current_step": "ontology",
            "calls_used": 2,
            "call_budget": 4,
            "draft_resource_count": 3,
            "steps": [{
                "id": "ontology",
                "title": "建设本体模型",
                "detail": "正在分析当前文档分段。",
                "status": "running",
            }],
            "activities": [{
                "id": "model-2",
                "kind": "model",
                "step_id": "ontology",
                "title": "模型分析",
                "detail": "等待当前模型调用返回。",
                "status": "running",
            }],
        }
        self.db.commit()

        iterator = assistant._iter_compilation_stream_events(
            job_id=job.id,
            tenant_id=self.tenant.id,
            user_id=self.user.id,
        )
        try:
            with (
                patch.object(assistant, "SessionLocal", self.factory),
                patch.object(
                    assistant.assistant_compilation_stream_service,
                    "wait",
                    side_effect=lambda subscription, **_kwargs: subscription,
                ),
                patch.object(assistant, "_COMPILATION_STREAM_LIVENESS_SECONDS", 0.0),
            ):
                events = [next(iterator) for _ in range(3)]
        finally:
            iterator.close()

        event_type, payload = next(
            event for event in events if event[0] == "compilation_liveness"
        )
        self.assertEqual(event_type, "compilation_liveness")
        self.assertEqual(payload["job_id"], job.id)
        self.assertEqual(payload["scenario_id"], self.scenario.id)
        self.assertEqual(payload["stream_state"], "connected")
        self.assertEqual(payload["stage_title"], "建设本体模型")
        self.assertGreaterEqual(payload["elapsed_seconds"], 65)
        self.assertEqual(payload["draft_resource_count"], 3)
        self.assertIn("当前模型调用仍在运行", payload["message"])
        self.assertNotIn("等待当前模型调用返回。", payload["message"])

    def test_provider_call_preserves_latest_draft_checkpoint_metadata(self) -> None:
        thread_id, message_id = self._claim_fixture()
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id="checkpoint-survives-provider-call"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
        )
        self.assertTrue(acquired)
        job_service.record_draft_checkpoint(
            self.db,
            job.id,
            resource_count=17,
            resource_kinds=["entity", "property", "relation"],
            detail="已同步首批草稿",
            lease_token=job.lease_token,
            lease_attempt=job.lease_attempt,
        )
        job_service.record_provider_call(
            self.db,
            job.id,
            used=1,
            budget=4,
            phase="chunk_provider_call",
            lease_token=job.lease_token,
            lease_attempt=job.lease_attempt,
        )

        refreshed = self.db.get(AssistantCompilationJob, job.id)
        self.assertEqual(refreshed.progress["draft_checkpoint_revision"], 1)
        self.assertEqual(refreshed.progress["draft_resource_count"], 17)
        self.assertEqual(
            refreshed.progress["draft_resource_kinds"],
            ["entity", "property", "relation"],
        )

    def _succeeded_canonical_job(
        self,
    ) -> tuple[AssistantCompilationJob, AssistantThread, AssistantMessage]:
        thread_id, message_id = self._claim_fixture()
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id=f"canonical-{message_id}"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
        )
        self.assertTrue(acquired)
        thread = self.db.get(AssistantThread, thread_id)
        message = self.db.get(AssistantMessage, message_id)
        proposal = assistant._build_proposal(
            "scenario_model",
            self._compiled_payload(),
            self.scenario,
        )
        message.role = "assistant"
        message.content = "权威建模草稿"
        message.context = {"compilation_job_id": job.id}
        message.attachments = []
        message.proposal = copy.deepcopy(proposal)
        job.thread_id = thread.id
        job.message_id = message.id
        job_service.mark_succeeded(
            self.db,
            job.id,
            result=copy.deepcopy(proposal),
        )
        return job, thread, message

    @staticmethod
    async def _consume(response) -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(
                chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            )
        return "".join(chunks)

    @staticmethod
    async def _disconnect_after_job_claim(response) -> str:
        chunks: list[str] = []
        iterator = response.body_iterator
        async for chunk in iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            chunks.append(text)
            if '"type": "compilation_job"' in text:
                break
        await iterator.aclose()
        return "".join(chunks)

    @staticmethod
    def _compiled_payload() -> dict:
        return {
            "schema_version": "scenario_model.v1",
            "source_manifest": [],
            "entities": [],
            "relations": [],
            "instances": [],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "conceptual_mappings": [],
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
            "fingerprint": "compiled-result",
        }

    def _stream(
        self,
        message: str = "编译完整业务模型",
        *,
        request_id: str = "",
        path: str | None = None,
    ) -> str:
        request_kwargs = {"request_id": request_id} if request_id else {}
        response = assistant.stream_chat(
            AssistantChatRequest(
                message=message,
                scenario_id=self.scenario.id,
                path=path or f"/scenarios/{self.scenario.id}",
                mode="draft",
                draft_kind="scenario_model",
                **request_kwargs,
            ),
            self.db,
        )
        body = asyncio.run(self._consume(response))
        # One assistant SSE now contains the durable job handle, live tool
        # checkpoints and the terminal proposal/error. The wait only reloads
        # the database fixture for assertions; it does not manufacture events.
        job = self._wait_for_terminal_job()
        self.assertIn(f'"job_id": "{job.id}"', body)
        return body

    def _wait_for_terminal_job(self, timeout: float = 10.0) -> AssistantCompilationJob:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.db.expire_all()
            job = self.db.execute(
                select(AssistantCompilationJob)
                .order_by(AssistantCompilationJob.created_at.desc())
            ).scalars().first()
            if job and job.status in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("后台编译任务未在测试超时内进入终态")

    @staticmethod
    def _formal_model_types() -> tuple[type, ...]:
        return (
            OntologyEntity,
            OntologyRelation,
            OntologyInstance,
            FunctionDefinition,
            OntologyAction,
            OntologyRule,
            OntologyEvent,
            OntologyWorkflow,
            DataMapping,
        )

    def _formal_model_counts(self) -> dict[type, int]:
        return {
            model: self.db.scalar(select(func.count()).select_from(model))
            for model in self._formal_model_types()
        }

    def _assert_inert_editable_completion(
        self,
        job: AssistantCompilationJob,
        *,
        formal_counts_before: dict[type, int] | None = None,
        issue_code: str = "COMPILER_EXECUTION_INTERRUPTED",
    ) -> None:
        self.db.expire_all()
        job = self.db.get(AssistantCompilationJob, job.id)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.error, "")
        proposal = job.result
        payload = proposal["payload"]
        self.assertEqual(proposal["kind"], "scenario_model")
        self.assertEqual(proposal["status"], "completed_with_gaps")
        self.assertFalse(proposal["requires_confirmation"])
        self.assertEqual(proposal["changes"], [])
        self.assertIn("6 个候选（6 类）", proposal["summary"])
        self.assertEqual(payload["changes"], [])
        self.assertTrue(all(
            payload.get(section) == []
            for section in assistant._SCENARIO_MODEL_RESOURCE_SECTIONS
        ))
        self.assertEqual(payload["current_task_id"], "")
        self.assertEqual(payload["execution_status"], "completed_with_gaps")
        self.assertTrue(payload["execution_summary"]["final"])
        self.assertEqual(
            payload["execution_summary"]["completed_task_count"],
            payload["execution_summary"]["total_task_count"],
        )
        self.assertTrue(all(
            task["status"] == "drafted_with_gaps"
            for task in payload["tasks"]
        ))
        self.assertEqual(
            [task["id"] for task in payload["tasks"]],
            [
                "ontology",
                "instances",
                "mapping",
                "capabilities",
                "rules",
                "workflows",
            ],
        )
        self.assertTrue(all(
            task["output_count"] > 0 and task["draft_output_count"] > 0
            for task in payload["tasks"]
        ))
        self.assertIn(
            issue_code,
            {item.get("code") for item in payload["unresolved"]},
        )
        self.assertEqual(len(payload["draft_candidates"]), 6)
        self.assertTrue(all(
            candidate.get("validation_status") == "blocked"
            and candidate.get("enabled") is False
            and candidate.get("publishable") is False
            for candidate in payload["draft_candidates"]
        ))
        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.compilation_job_id == job.id
            )
        ).all())
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            {row.resource_kind for row in rows},
            {
                "entity",
                "instance",
                "conceptual_mapping",
                "function",
                "rule",
                "workflow",
            },
        )
        self.assertTrue(all(
            row.draft_status == "needs_attention"
            and row.enabled is False
            and row.publishable is False
            and row.revision == 0
            and isinstance(row.payload, dict)
            and bool(row.payload)
            for row in rows
        ))
        self.assertTrue(all(
            any(issue.get("code") == issue_code for issue in row.validation_issues)
            for row in rows
        ))
        if formal_counts_before is not None:
            self.assertEqual(formal_counts_before, self._formal_model_counts())

    def test_staged_source_input_is_private_until_final_task_or_expiry(self) -> None:
        thread_id, message_id = self._claim_fixture()
        source_input = {
            "version": 1,
            "compiler_message": "根据附件先建设本体模型",
            "compiler_documents": [{
                "id": "brief",
                "filename": "建筑业务说明.md",
                "text": "项目以项目编号唯一标识。",
            }],
            "prepared_context": {},
            "llm_config_id": self.llm.id,
            "context": {},
            "sources": [],
            "execution_policy": {"task_scope": "ontology"},
        }
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id="staged-source-retention"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
            execution_input=source_input,
        )
        self.assertTrue(acquired)
        job_service.mark_succeeded(
            self.db,
            job.id,
            result={"proposal_id": "staged-source-retention"},
            retain_execution_input=True,
        )

        retained = job_service.load_owner_continuation_input(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
        )
        self.assertEqual(retained["compiler_message"], source_input["compiler_message"])
        self.assertEqual(retained["compiler_documents"], source_input["compiler_documents"])
        self.assertTrue(job_service.discard_owner_execution_input(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
        ))
        with self.assertRaises(LookupError):
            job_service.load_owner_continuation_input(
                self.db,
                job.id,
                tenant_id=self.tenant.id,
                created_by_user_id=self.user.id,
            )

        expired_job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id="staged-source-expiry"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
            execution_input=source_input,
        )
        self.assertTrue(acquired)
        job_service.mark_succeeded(
            self.db,
            expired_job.id,
            result={"proposal_id": "staged-source-expiry"},
            retain_execution_input=True,
        )
        self.assertEqual(
            job_service.purge_expired_completed_execution_inputs(
                self.db,
                now=(
                    expired_job.completed_at
                    + job_service.CONTINUATION_INPUT_RETENTION
                    + timedelta(seconds=1)
                ),
            ),
            1,
        )
        with self.assertRaises(LookupError):
            job_service.load_owner_continuation_input(
                self.db,
                expired_job.id,
                tenant_id=self.tenant.id,
                created_by_user_id=self.user.id,
            )

    def test_continue_model_task_reuses_retained_source_and_scopes_next_job(self) -> None:
        thread_id, message_id = self._claim_fixture()
        thread = self.db.get(AssistantThread, thread_id)
        message = self.db.get(AssistantMessage, message_id)
        assert thread is not None and message is not None

        source_input = {
            "version": 1,
            "compiler_message": "根据附件建设建筑项目预警模型",
            "compiler_documents": [{
                "id": "brief",
                "filename": "建筑预警说明.md",
                "text": "项目以项目编号唯一标识，并记录欠薪预警实例。",
            }],
            "prepared_context": {},
            "llm_config_id": self.llm.id,
            "context": {},
            "sources": [],
            "execution_policy": {"task_scope": "ontology"},
        }
        source_job, acquired = job_service.claim_compilation(
            self.db,
            identity=self._identity(request_id="continue-model-root"),
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread.id,
            message_id=message.id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
            execution_input=source_input,
        )
        self.assertTrue(acquired)

        initial = self._compiled_payload()
        initial["entities"] = [{"key": "entity.project", "name": "项目"}]
        initial["changes"] = [{"change_id": "entity.project", "operation": "add"}]
        initial["generation"] = {
            "mode": "staged",
            "generated_task_ids": ["ontology"],
        }
        proposal = assistant._build_proposal("scenario_model", initial, self.scenario)
        proposal["payload"] = assistant._refresh_model_task_states(
            proposal["payload"],
            applied_task_id="ontology",
            applied_status="applied",
        )
        proposal["status"] = "in_progress"
        proposal["requires_confirmation"] = False
        self.assertEqual(proposal["payload"]["next_action"]["type"], "generate_task")
        self.assertEqual(proposal["payload"]["next_action"]["task_id"], "instances")
        message.role = "assistant"
        message.content = "本体模型已确认，等待开始实例任务。"
        message.context = {"compilation_job_id": source_job.id}
        message.proposal = copy.deepcopy(proposal)
        self.db.commit()
        job_service.mark_succeeded(
            self.db,
            source_job.id,
            result=copy.deepcopy(proposal),
            retain_execution_input=True,
        )

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_submit_compilation_job") as submit,
        ):
            status = assistant.continue_model_task(
                AssistantModelTaskContinuationRequest(
                    scenario_id=self.scenario.id,
                    thread_id=thread.id,
                    proposal_id=proposal["proposal_id"],
                    task_id="instances",
                ),
                self.db,
            )

        self.assertEqual(status.status, "running")
        self.db.expire_all()
        continuation = self.db.execute(
            select(AssistantCompilationJob)
            .where(AssistantCompilationJob.id != source_job.id)
            .order_by(AssistantCompilationJob.created_at.desc())
        ).scalars().first()
        self.assertIsNotNone(continuation)
        assert continuation is not None
        self.assertEqual(continuation.thread_id, thread.id)
        self.assertEqual(continuation.message_id, message.id)
        self.assertEqual(continuation.execution_input["execution_policy"]["task_scope"], "instances")
        self.assertEqual(
            continuation.execution_input["execution_policy"]["continuation_proposal_id"],
            proposal["proposal_id"],
        )
        self.assertEqual(
            self.db.get(AssistantMessage, message.id).context["root_compilation_job_id"],
            source_job.id,
        )
        submit.assert_called_once_with(job_id=continuation.id)
        self.assertEqual(
            job_service.load_owner_continuation_input(
                self.db,
                source_job.id,
                tenant_id=self.tenant.id,
                created_by_user_id=self.user.id,
            )["compiler_documents"],
            source_input["compiler_documents"],
        )

    def _assert_inert_compiler_payload(
        self,
        payload: dict,
        *,
        issue_code: str,
    ) -> None:
        self.assertEqual(payload["changes"], [])
        self.assertTrue(all(
            payload.get(section) == []
            for section in assistant._SCENARIO_MODEL_RESOURCE_SECTIONS
        ))
        self.assertIn(
            issue_code,
            {item.get("code") for item in payload["unresolved"]},
        )
        self.assertEqual(len(payload["draft_candidates"]), 6)
        self.assertTrue(all(
            candidate.get("validation_status") == "blocked"
            and candidate.get("enabled") is False
            and candidate.get("publishable") is False
            and isinstance(candidate.get("payload"), dict)
            for candidate in payload["draft_candidates"]
        ))

    def test_concurrent_claim_has_exactly_one_owner(self) -> None:
        identity = self._identity()
        thread_id, message_id = self._claim_fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _index: self._claim(identity, thread_id, message_id),
                range(2),
            ))
        self.assertEqual(sum(1 for _job, acquired in results if acquired), 1)
        self.assertEqual(len({job.id for job, _acquired in results}), 1)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantCompilationJob)),
            1,
        )

    def test_compilation_job_schema_is_declared_by_postgresql_migration(self) -> None:
        ddl = baseline_table_ddl("assistant_compilation_jobs")
        self.assertIn("request_fingerprint VARCHAR(64) NOT NULL", ddl)
        self.assertIn("execution_input JSON NOT NULL", ddl)
        self.assertIn("lease_token VARCHAR(64) NOT NULL", ddl)
        self.assertIn("lease_expires_at TIMESTAMP WITH TIME ZONE", ddl)
        self.assertIn("lease_attempt INTEGER NOT NULL", ddl)
        self.assertIn(
            "CONSTRAINT uq_assistant_compilation_jobs_fingerprint "
            "UNIQUE (request_fingerprint)",
            ddl,
        )
        schema_sql = render_postgresql_upgrade("20260827_01")
        self.assertIn(
            "CREATE INDEX ix_assistant_compilation_jobs_status_lease_expiry "
            "ON assistant_compilation_jobs (status, lease_expires_at)",
            schema_sql,
        )
        self.assertIn(
            "CREATE INDEX ix_assistant_attachments_content_hash "
            "ON assistant_attachments (content_hash)",
            schema_sql,
        )

    def test_fingerprint_changes_for_every_output_affecting_input(self) -> None:
        base = self._identity()
        # Transport newline forms converge, while paragraph-significant
        # whitespace remains part of the exact compiler input identity.
        normalized_a = self._identity(message="A\r\n\r\nB")
        normalized_b = self._identity(message="A\n\nB")
        self.assertEqual(
            normalized_a.request_fingerprint,
            normalized_b.request_fingerprint,
        )
        self.assertNotEqual(
            self._identity(message="A B").request_fingerprint,
            self._identity(message="A\n\nB").request_fingerprint,
        )
        variants = [
            self._identity(message="编译另一个完整业务模型"),
            self._identity(attachment_text="不同的附件内容"),
            self._identity(baseline="baseline-v2"),
            self._identity(model="different-model"),
            self._identity(compiler_version="scenario_model.compiler.v999"),
            self._identity(mapping_context="mapping-context-v2"),
            self._identity(call_budget=5),
            self._identity(
                assistant_scope_key="scenario:test|path:/another-page"
            ),
        ]
        self.assertTrue(all(
            item.request_fingerprint != base.request_fingerprint
            for item in variants
        ))

    def test_explicit_request_id_starts_a_new_compilation_intent(self) -> None:
        first = self._identity(request_id="send-1")
        second = self._identity(request_id="send-2")
        self.assertNotEqual(first.request_fingerprint, second.request_fingerprint)

    def test_canonical_documents_are_content_addressed_not_upload_addressed(self) -> None:
        first = SimpleNamespace(
            id="upload-one",
            filename="domain.txt",
            mime="text/plain",
            status="parsed",
            content_hash="a" * 64,
            parsed_text="same business content",
            error="",
        )
        second = SimpleNamespace(
            id="upload-two",
            filename="domain.txt",
            mime="text/plain",
            status="parsed",
            content_hash="a" * 64,
            parsed_text="same business content",
            error="",
        )
        first_docs = job_service.canonical_compiler_documents([first])
        second_docs = job_service.canonical_compiler_documents([second])
        self.assertEqual(first_docs, second_docs)
        self.assertNotIn("upload-one", first_docs[0]["id"])
        self.assertNotIn("upload-two", second_docs[0]["id"])

    def test_live_mapping_schema_is_part_of_frozen_compilation_context(self) -> None:
        source = DataSource(
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目库",
            type="postgres",
            config={},
            status="ok",
        )
        self.db.add(source)
        self.db.commit()

        def schema(column_name: str):
            return [{
                "name": "projects",
                "columns": [{"name": column_name, "type": "text", "pk": True}],
            }]

        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            return_value=schema("project_id"),
        ):
            first = scenario_model_compiler.prepare_compilation_context(
                self.db, self.scenario
            )
        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            return_value=schema("project_code"),
        ):
            second = scenario_model_compiler.prepare_compilation_context(
                self.db, self.scenario
            )
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertIn((source.id, "projects"), first["columns_by_table"])

    def test_successful_duplicate_stream_replays_without_second_provider(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            first = self._stream(request_id="successful-retry")
            second = self._stream(request_id="successful-retry")

        self.assertEqual(provider_calls, 1)
        self.assertIn('"type": "proposal"', first)
        self.assertIn('"type": "proposal"', second)
        self.assertIn("没有重复调用模型", second)
        jobs = self.db.execute(select(AssistantCompilationJob)).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "succeeded")
        self.assertEqual(jobs[0].llm_calls_used, 1)

    def test_same_request_id_on_different_scopes_is_rejected(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            self._stream(
                request_id="same-send-id",
                path=f"/scenarios/{self.scenario.id}/ontology",
            )
            with self.assertRaises(HTTPException) as conflict:
                self._stream(
                    request_id="same-send-id",
                    path=f"/scenarios/{self.scenario.id}/workflows",
                )

        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(provider_calls, 1)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantCompilationJob)),
            1,
        )

    def test_separate_sends_do_not_replay_by_message_text_alone(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            first = self._stream()
            second = self._stream()

        self.assertEqual(provider_calls, 2)
        self.assertIn('"type": "proposal"', first)
        self.assertIn('"type": "proposal"', second)
        self.assertNotIn("没有再次调用模型", second)
        jobs = self.db.execute(select(AssistantCompilationJob)).scalars().all()
        self.assertEqual(len(jobs), 2)

    def test_non_stream_endpoint_replays_stream_result_without_provider(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            stream_body = self._stream(request_id="cross-transport-retry")
            sync_reply = assistant.chat(
                AssistantChatRequest(
                    message="编译完整业务模型",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                    request_id="cross-transport-retry",
                ),
                self.db,
            )

        self.assertEqual(provider_calls, 1)
        self.assertIn('"type": "proposal"', stream_body)
        self.assertEqual(sync_reply.proposal.get("kind"), "scenario_model")
        self.assertIn("没有重复调用模型", sync_reply.reply)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantCompilationJob)),
            1,
        )

    def test_concurrent_stream_and_non_stream_share_one_provider_chain(self) -> None:
        provider_started = threading.Event()
        release_provider = threading.Event()
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            provider_started.set()
            if not release_provider.wait(timeout=10):
                raise RuntimeError("test provider release timeout")
            return self._compiled_payload()

        request = AssistantChatRequest(
            message="并发编译完整业务模型",
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}",
            mode="draft",
            draft_kind="scenario_model",
            request_id="concurrent-retry",
        )

        def request_session() -> Session:
            db = self.factory()
            db.info["tenant_id"] = self.tenant.id
            db.info["user_id"] = self.user.id
            return db

        def run_stream() -> str:
            with request_session() as db:
                return asyncio.run(
                    self._consume(assistant.stream_chat(request, db))
                )

        def run_sync():
            with request_session() as db:
                return assistant.chat(request, db)

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(
                assistant,
                "_llm",
                side_effect=lambda db: db.get(LLMConfig, self.llm.id),
            ),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            stream_future = pool.submit(run_stream)
            self.assertTrue(provider_started.wait(timeout=10))
            sync_future = pool.submit(run_sync)
            sync_reply = sync_future.result(timeout=10)
            release_provider.set()
            stream_body = stream_future.result(timeout=10)

        self.assertEqual(provider_calls, 1)
        self.assertIn("没有启动第二套模型调用", sync_reply.reply)
        self.assertIn('"type": "compilation_job"', stream_body)
        job = self._wait_for_terminal_job()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(
            self.db.get(AssistantMessage, job.message_id).proposal.get("kind"),
            "scenario_model",
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(AssistantCompilationJob)),
            1,
        )

    def test_disconnect_after_claim_keeps_job_recoverable_and_builds_drafts(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            raise RuntimeError("provider interrupted after durable claim")

        counts_before = self._formal_model_counts()
        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="领取任务后立即断开",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                ),
                self.db,
            )
            body = asyncio.run(self._disconnect_after_job_claim(response))
            job = self._wait_for_terminal_job()

        self.assertIn('"type": "compilation_job"', body)
        self.assertEqual(provider_calls, 1)
        self._assert_inert_editable_completion(
            job,
            formal_counts_before=counts_before,
        )

    def test_chat_stream_carries_tool_progress_and_terminal_result_without_status_poll(self) -> None:
        def fake_compile(*_args, call_budget=None, **_kwargs):
            call_budget.consume("streamed_provider_call")
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="在同一条对话流中完成场景建模",
                    request_id="single-sse-capability-stream",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                ),
                self.db,
            )
            body = asyncio.run(self._consume(response))

        job_index = body.index('"type": "compilation_job"')
        progress_index = body.index('"type": "compilation_progress"')
        tool_index = body.index('"type": "tool_event"')
        checkpoint_index = body.index('"type": "draft_checkpoint"')
        result_index = body.index('"type": "compilation_result"')
        proposal_index = body.index('"type": "proposal"')
        done_index = body.index('"type": "done"')
        self.assertLess(job_index, tool_index)
        self.assertLess(tool_index, progress_index)
        self.assertLess(progress_index, checkpoint_index)
        self.assertLess(checkpoint_index, result_index)
        self.assertLess(progress_index, result_index)
        self.assertLess(result_index, proposal_index)
        self.assertLess(proposal_index, done_index)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))
        self.assertNotIn("/compilation-jobs/", body)

    def test_running_compilation_accepts_guidance_and_reuses_the_same_job(self) -> None:
        first_call_started = threading.Event()
        release_first_call = threading.Event()
        compiler_messages: list[str] = []
        stream_body: list[str] = []

        def fake_compile(*_args, message="", call_budget=None, **_kwargs):
            call_budget.consume("guided_provider_call")
            compiler_messages.append(message)
            if len(compiler_messages) == 1:
                first_call_started.set()
                self.assertTrue(release_first_call.wait(timeout=5))
            return self._compiled_payload()

        def consume_stream() -> None:
            with self.factory() as request_db:
                request_db.info["tenant_id"] = self.tenant.id
                request_db.info["user_id"] = self.user.id
                response = assistant.stream_chat(
                    AssistantChatRequest(
                        message="先建立建筑项目场景模型",
                        request_id="guided-live-compilation",
                        scenario_id=self.scenario.id,
                        path=f"/scenarios/{self.scenario.id}",
                        mode="draft",
                        draft_kind="scenario_model",
                    ),
                    request_db,
                )
                stream_body.append(asyncio.run(self._consume(response)))

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            stream_thread = threading.Thread(target=consume_stream)
            stream_thread.start()
            self.assertTrue(first_call_started.wait(timeout=5))
            with self.factory() as guidance_db:
                guidance_db.info["tenant_id"] = self.tenant.id
                guidance_db.info["user_id"] = self.user.id
                job = guidance_db.scalar(select(AssistantCompilationJob))
                self.assertIsNotNone(job)
                response = assistant.submit_compilation_guidance(
                    job.id,
                    AssistantCompilationGuidanceRequest(
                        request_id="guidance-add-contractor",
                        message="补充施工企业对象，并建立其承建项目关系。",
                    ),
                    guidance_db,
                )
                self.assertTrue(response.accepted)
                self.assertEqual(response.job.id, job.id)
            release_first_call.set()
            stream_thread.join(timeout=15)
            self.assertFalse(stream_thread.is_alive())

        self.assertEqual(len(compiler_messages), 2)
        self.assertIn("补充施工企业对象", compiler_messages[1])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AssistantCompilationJob)), 1)
        body = stream_body[0]
        self.assertIn('"type": "draft_checkpoint"', body)
        self.assertIn("采纳补充指导", body)
        self.db.expire_all()
        saved_guidance = self.db.scalars(
            select(AssistantMessage).where(
                AssistantMessage.role == "user",
                AssistantMessage.content.like("补充施工企业对象%"),
            )
        ).all()
        self.assertEqual(len(saved_guidance), 1)

    def test_stream_and_sync_source_preview_errors_create_durable_inert_drafts(
        self,
    ) -> None:
        counts_before = self._formal_model_counts()
        private_error = "PREVIEW-SENTINEL: attachment body is invalid"
        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "prepare_source_bundle_preview",
                side_effect=ValueError(private_error),
            ) as preview,
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                return_value=self._compiled_payload(),
            ) as compile_model,
        ):
            stream_body = self._stream(
                "流式来源预览失败仍需建立草稿",
                request_id="stream-source-preview-error",
            )
            self.db.expire_all()
            stream_job = self.db.execute(
                select(AssistantCompilationJob)
            ).scalar_one()
            self._assert_inert_editable_completion(
                stream_job,
                formal_counts_before=counts_before,
                issue_code="SOURCE_INPUT_INVALID",
            )
            sync_reply = assistant.chat(
                AssistantChatRequest(
                    message="同步来源预览失败仍需建立草稿",
                    request_id="sync-source-preview-error",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                ),
                self.db,
            )

        compile_model.assert_not_called()
        self.assertGreaterEqual(preview.call_count, 4)
        self.assertIn('"type": "compilation_job"', stream_body)
        self.assertNotIn(private_error, stream_body)
        self.assertEqual(sync_reply.proposal.get("kind"), "scenario_model")
        self.assertNotIn(private_error, sync_reply.reply)
        self.db.expire_all()
        jobs = list(self.db.scalars(
            select(AssistantCompilationJob).order_by(
                AssistantCompilationJob.created_at,
                AssistantCompilationJob.id,
            )
        ).all())
        self.assertEqual(len(jobs), 2)
        sync_job = next(job for job in jobs if job.id != stream_job.id)
        self._assert_inert_editable_completion(
            sync_job,
            formal_counts_before=counts_before,
            issue_code="SOURCE_INPUT_INVALID",
        )

    def test_stream_claim_persists_private_input_and_recovery_uses_frozen_body(
        self,
    ) -> None:
        frozen_attachment_text = (
            "RECOVERY-ONLY-ATTACHMENT-BODY: 项目必须关联合同并经过审批"
        )
        changed_attachment_text = "附件行已被后续请求修改，不得用于旧任务恢复"
        attachment = AssistantAttachment(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            filename="recovery-domain.txt",
            mime="text/plain",
            size=len(frozen_attachment_text.encode("utf-8")),
            content_hash=job_service._sha256(
                frozen_attachment_text.encode("utf-8")
            ),
            status="parsed",
            parsed_text=frozen_attachment_text,
            error="",
        )
        self.db.add(attachment)
        self.db.commit()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant,
                "_submit_compilation_job",
                return_value=False,
            ) as submit,
        ):
            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="从冻结附件恢复完整场景建模",
                    request_id="persisted-recovery-input",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                    attachment_ids=[attachment.id],
                ),
                self.db,
            )
            stream_body = asyncio.run(self._consume(response))

        self.assertIn('"type": "compilation_job"', stream_body)
        self.assertNotIn(frozen_attachment_text, stream_body)
        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        submit.assert_called_once_with(job_id=job.id)
        self.assertEqual(job.status, "running")
        self.assertEqual(job.lease_token, "")
        self.assertIsNone(job.lease_expires_at)
        private_input = job_service.load_owner_execution_input(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
        )
        self.assertEqual(private_input["version"], 1)
        self.assertEqual(
            private_input["compiler_message"],
            "从冻结附件恢复完整场景建模",
        )
        self.assertEqual(
            private_input["compiler_documents"][0]["text"],
            frozen_attachment_text,
        )

        public_status = assistant.get_compilation_job(
            job.id,
            response=Response(),
            db=self.db,
        )
        public_thread_jobs = assistant.list_thread_compilation_jobs(
            job.thread_id,
            response=Response(),
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}",
            db=self.db,
        )
        public_projection = json.dumps(
            {
                "status": public_status.model_dump(mode="json"),
                "thread_jobs": [
                    item.model_dump(mode="json") for item in public_thread_jobs
                ],
            },
            ensure_ascii=False,
        )
        self.assertNotIn(frozen_attachment_text, public_projection)
        for hidden in (
            "execution_input",
            "compiler_documents",
            "lease_token",
            "request_fingerprint",
            "attachment_content_hash",
        ):
            self.assertNotIn(hidden, public_projection)

        attachment.parsed_text = changed_attachment_text
        self.db.commit()
        captured: dict[str, object] = {}

        def fake_recovered_compile(
            _compile_db,
            _scenario,
            *,
            message,
            documents,
            llm,
            call_budget,
            prepared_context,
            request_timeout,
            on_progress,
            on_checkpoint,
            task_scope,
        ):
            captured.update({
                "message": message,
                "documents": copy.deepcopy(documents),
                "llm_id": llm.id,
                "prepared_context": copy.deepcopy(prepared_context),
                "request_timeout": request_timeout,
                "has_progress": callable(on_progress),
                "has_checkpoint": callable(on_checkpoint),
                "task_scope": task_scope,
            })
            call_budget.consume("recovered_provider_call")
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_recovered_compile,
            ),
        ):
            self.assertEqual(
                assistant.recover_expired_compilation_jobs(limit=1),
                1,
            )
            terminal = self._wait_for_terminal_job()

        self.assertEqual(terminal.status, "succeeded")
        self.assertEqual(terminal.lease_attempt, 1)
        self.assertEqual(terminal.lease_token, "")
        self.assertIsNone(terminal.lease_expires_at)
        self.assertEqual(captured["message"], private_input["compiler_message"])
        self.assertEqual(captured["task_scope"], "")
        self.assertEqual(
            captured["documents"],
            private_input["compiler_documents"],
        )
        self.assertEqual(
            captured["documents"][0]["text"],
            frozen_attachment_text,
        )
        self.assertNotEqual(
            captured["documents"][0]["text"],
            changed_attachment_text,
        )
        self.assertEqual(captured["llm_id"], self.llm.id)
        self.assertGreater(float(captured["request_timeout"]), 0)
        self.assertTrue(captured["has_progress"])

        self.db.expire_all()
        terminal = self.db.get(AssistantCompilationJob, terminal.id)
        canonical_message = self.db.get(AssistantMessage, terminal.message_id)
        self.assertEqual(canonical_message.proposal, terminal.result)
        recovered = assistant.get_compilation_job_result(
            terminal.id,
            response=Response(),
            db=self.db,
        )
        recovered_public = json.dumps(
            recovered.model_dump(mode="json"),
            ensure_ascii=False,
        )
        self.assertNotIn(frozen_attachment_text, recovered_public)
        self.assertNotIn(changed_attachment_text, recovered_public)
        self.assertNotIn("execution_input", recovered_public)

    def test_recovery_claim_error_releases_the_reserved_worker_slot(self) -> None:
        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(
                job_service,
                "claim_expired_running_jobs",
                side_effect=RuntimeError("scan failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "scan failed"):
                assistant.recover_expired_compilation_jobs(limit=1)

        acquired = [
            assistant._COMPILATION_SUBMISSION_SLOTS.acquire(blocking=False)
            for _ in range(assistant._COMPILATION_WORKER_COUNT)
        ]
        try:
            self.assertTrue(all(acquired))
        finally:
            # Restore the complete bounded capacity even when an assertion
            # fails, so this regression cannot poison later executor tests.
            for _ in range(assistant._COMPILATION_WORKER_COUNT):
                assistant._COMPILATION_SUBMISSION_SLOTS.release()

    def test_failed_duplicate_does_not_automatically_retry(self) -> None:
        provider_calls = 0
        counts_before = self._formal_model_counts()
        frozen_context = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            raise RuntimeError("provider failed")

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
            patch.object(
                assistant.scenario_model_compiler,
                "prepare_compilation_context",
                side_effect=lambda *_args, **_kwargs: copy.deepcopy(
                    frozen_context
                ),
            ),
        ):
            first = self._stream(request_id="failed-retry")
            second = self._stream(request_id="failed-retry")

        self.assertEqual(provider_calls, 1)
        self.assertNotIn("provider failed", first)
        self.assertIn("没有重复调用模型", second)
        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        self.assertEqual(job.llm_calls_used, 1)
        self._assert_inert_editable_completion(
            job,
            formal_counts_before=counts_before,
        )

    def test_budget_exhaustion_stops_provider_and_keeps_model_zero_write(self) -> None:
        provider_calls = 0

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("first_provider_call")
            provider_calls += 1
            # The second call is rejected before a provider can be invoked.
            call_budget.consume("second_provider_call")
            provider_calls += 1
            return self._compiled_payload()

        counts_before = self._formal_model_counts()
        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant,
                "get_settings",
                return_value=SimpleNamespace(
                    scenario_model_max_llm_calls=1,
                    scenario_model_llm_timeout=600.0,
                ),
            ),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            self._stream()

        self.assertEqual(provider_calls, 1)
        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        self.assertEqual(job.llm_calls_used, 1)
        self._assert_inert_editable_completion(
            job,
            formal_counts_before=counts_before,
        )

    def test_real_compiler_shares_budget_across_malformed_output_retries(self) -> None:
        budget = scenario_model_compiler.LLMCallBudget(1)
        provider_calls = 0
        counts_before = self._formal_model_counts()

        def invalid_chat(*_args, before_provider_call=None, **_kwargs):
            nonlocal provider_calls
            before_provider_call()
            provider_calls += 1
            return {"content": "not-json", "raw": {}}

        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            side_effect=invalid_chat,
        ) as chat_wrapper:
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="根据材料编译完整业务模型",
                documents=[],
                llm=self.llm,
                call_budget=budget,
            )
        self.assertEqual(chat_wrapper.call_count, 2)
        self.assertEqual(provider_calls, 1)
        self.assertEqual(budget.used, 1)
        self._assert_inert_compiler_payload(
            payload,
            issue_code="COMPILER_PROVIDER_REQUEST_FAILED",
        )
        self.assertEqual(counts_before, self._formal_model_counts())

    def test_adaptive_length_retry_consumes_budget_per_real_provider_call(self) -> None:
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=None),
                finish_reason="length",
            )],
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=1,
                total_tokens=5,
            ),
        )
        completed = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="should-not-run", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=2,
                total_tokens=6,
            ),
        )

        class AdaptiveClient:
            def __init__(self):
                self.calls = 0
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create)
                )

            def create(self, **_kwargs):
                response = (truncated, completed)[self.calls]
                self.calls += 1
                return response

        client = AdaptiveClient()
        budget = scenario_model_compiler.LLMCallBudget(1)
        with patch.object(llm_service, "_client", return_value=client):
            with self.assertRaises(
                scenario_model_compiler.CompilationCallBudgetExceeded
            ):
                llm_service.chat(
                    self.llm,
                    [{"role": "user", "content": "compile"}],
                    db=self.db,
                    before_provider_call=lambda: budget.consume(
                        "adaptive_provider_call"
                    ),
                )
        self.assertEqual(client.calls, 1)
        self.assertEqual(budget.used, 1)

    def test_stream_rolls_back_compiler_mutations_before_job_terminal_write(self) -> None:
        counts_before = self._formal_model_counts()

        def violating_compile(compile_db, *_args, call_budget=None, **_kwargs):
            call_budget.consume("test_provider_call")
            compile_db.add(
                OntologyEntity(
                    scenario_id=self.scenario.id,
                    name="不应落库的对象",
                    namespace="forbidden",
                )
            )
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=violating_compile,
            ),
        ):
            self._stream("编译并验证零写入边界")

        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        self.assertEqual(job.llm_calls_used, 1)
        self._assert_inert_editable_completion(
            job,
            formal_counts_before=counts_before,
        )

    def test_success_atomically_links_job_and_recoverable_server_proposal(self) -> None:
        def fake_compile(*_args, call_budget=None, **_kwargs):
            call_budget.consume("test_provider_call")
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            self._stream("验证任务结果恢复")

        self.db.expire_all()
        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        message = self.db.get(AssistantMessage, job.message_id)
        self.assertEqual(job.status, "succeeded")
        self.assertIsNotNone(message)
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.proposal, job.result)
        self.assertEqual(message.context.get("compilation_job_id"), job.id)

        response = Response()
        recovered = assistant.get_compilation_job_result(
            job.id,
            response=response,
            db=self.db,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(recovered.apply_ready)
        self.assertEqual(recovered.proposal_message_id, message.id)
        self.assertEqual(
            recovered.proposal.get("proposal_id"),
            job.result.get("proposal_id"),
        )
        serialized = json.dumps(
            recovered.model_dump(mode="json"),
            ensure_ascii=False,
        ).casefold()
        self.assertNotIn("fingerprint", serialized)
        self.assertNotIn("_hash", serialized)
        saved_thread, saved_message, saved_proposal = assistant._find_saved_proposal(
            self.db,
            recovered.proposal_thread_id,
            recovered.proposal["proposal_id"],
        )
        self.assertEqual(saved_thread.id, recovered.proposal_thread_id)
        self.assertEqual(saved_message.id, recovered.proposal_message_id)
        self.assertEqual(saved_proposal, job.result)

    def test_result_uses_canonical_message_and_never_regresses_from_newer_clone(self) -> None:
        job, canonical_thread, canonical_message = self._succeeded_canonical_job()
        canonical = copy.deepcopy(canonical_message.proposal)
        canonical["run_revision"] = 5
        canonical["payload"]["execution_revision"] = 5
        canonical_message.proposal = copy.deepcopy(canonical)
        canonical_message.context = {
            **canonical_message.context,
            "run_revision": 5,
        }
        job.result = copy.deepcopy(canonical)

        clone_thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scenario_id=self.scenario.id,
            scope_key=(
                f"scenario:{self.scenario.id}|path:/scenarios/"
                f"{self.scenario.id}/duplicate"
            ),
            title="重复订阅",
        )
        self.db.add(clone_thread)
        self.db.flush()
        legacy_clone = copy.deepcopy(canonical)
        for key in (
            "tasks",
            "current_task_id",
            "execution_status",
            "execution_summary",
            "execution_revision",
            "next_action",
            "run_id",
        ):
            legacy_clone["payload"].pop(key, None)
        legacy_clone["status"] = "pending"
        legacy_clone.pop("run_revision", None)
        self.db.add(
            AssistantMessage(
                thread_id=clone_thread.id,
                role="assistant",
                content="重复订阅旧副本",
                context={"compilation_job_id": job.id},
                proposal=legacy_clone,
            )
        )
        self.db.commit()

        assistant.list_thread_messages(
            clone_thread.id,
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}/duplicate",
            db=self.db,
        )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(AssistantCompilationJob, job.id).result["run_revision"],
            5,
        )

        recovered = assistant.get_compilation_job_result(
            job.id,
            response=Response(),
            db=self.db,
        )
        self.assertEqual(recovered.proposal_message_id, canonical_message.id)
        self.assertEqual(recovered.proposal_thread_id, canonical_thread.id)
        self.assertEqual(recovered.proposal_scope_key, canonical_thread.scope_key)
        self.assertEqual(recovered.proposal["run_revision"], 5)

    def test_result_fails_closed_when_canonical_rag_source_is_no_longer_valid(self) -> None:
        job, _thread, canonical_message = self._succeeded_canonical_job()
        canonical_message.attachments = [{
            "id": "rag:revoked:missing",
            "kind": "rag",
            "data_source_id": "revoked-source",
            "file_id": "revoked-file",
            "chunk_id": "revoked-chunk",
        }]
        self.db.commit()

        with self.assertRaises(HTTPException) as denied:
            assistant.get_compilation_job_result(
                job.id,
                response=Response(),
                db=self.db,
            )
        self.assertEqual(denied.exception.status_code, 409)
        self.assertIn("资料", str(denied.exception.detail))

    def test_thread_job_list_recovers_duplicate_request_subscription(self) -> None:
        provider_calls = 0
        compile_started = threading.Event()
        allow_finish = threading.Event()

        def fake_compile(*_args, call_budget=None, **_kwargs):
            nonlocal provider_calls
            call_budget.consume("test_provider_call")
            provider_calls += 1
            compile_started.set()
            if not allow_finish.wait(timeout=5):
                raise TimeoutError("test did not release compilation")
            return self._compiled_payload()

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            request = AssistantChatRequest(
                message="验证重复请求恢复订阅",
                request_id="subscription-retry",
                scenario_id=self.scenario.id,
                path=f"/scenarios/{self.scenario.id}",
                mode="draft",
                draft_kind="scenario_model",
            )
            def consume_request() -> str:
                with self.factory() as request_db:
                    request_db.info["tenant_id"] = self.tenant.id
                    request_db.info["user_id"] = self.user.id
                    return asyncio.run(self._consume(
                        assistant.stream_chat(request, request_db)
                    ))

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first_future = pool.submit(consume_request)
                    self.assertTrue(compile_started.wait(timeout=5))
                    second_future = pool.submit(consume_request)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with self.factory() as inspect_db:
                            linked_count = inspect_db.scalar(
                                select(func.count()).select_from(AssistantMessage).where(
                                    AssistantMessage.role == "assistant",
                                )
                            )
                        if linked_count >= 2:
                            break
                        time.sleep(0.01)
                    self.assertGreaterEqual(linked_count, 2)
                    allow_finish.set()
                    first_body = first_future.result(timeout=10)
                    second_body = second_future.result(timeout=10)
                    self.assertIn('"replayed": true', second_body)
            finally:
                allow_finish.set()
            job = self._wait_for_terminal_job()

        self.db.expire_all()
        job = self.db.get(AssistantCompilationJob, job.id)
        linked_threads = self.db.execute(
            select(AssistantThread)
            .where(AssistantThread.scenario_id == self.scenario.id)
            .order_by(AssistantThread.created_at)
        ).scalars().all()
        self.assertEqual(provider_calls, 1)
        self.assertIn('"replayed": false', first_body)
        # One request_id now freezes both the semantic route and its thread;
        # a transport retry subscribes in that same durable conversation.
        self.assertEqual(len(linked_threads), 1)
        linked_messages = list(self.db.scalars(
            select(AssistantMessage).where(
                AssistantMessage.role == "assistant",
            )
        ).all())
        linked_messages = [
            message for message in linked_messages
            if (message.context or {}).get("compilation_job_id") == job.id
        ]
        self.assertEqual(len(linked_messages), 2)
        canonical = next(
            message for message in linked_messages if message.id == job.message_id
        )
        subscription = next(
            message for message in linked_messages if message.id != job.message_id
        )
        self.assertEqual(canonical.proposal, job.result)
        self.assertNotEqual(canonical.context.get("status"), "processing")
        self.assertEqual(subscription.proposal, {})
        self.assertEqual(subscription.context.get("status"), "no_changes")
        self.assertEqual(
            subscription.context.get("canonical_message_id"),
            canonical.id,
        )
        self.assertEqual(
            subscription.context.get("model_run_id"),
            job.result.get("proposal_id"),
        )
        for thread in linked_threads:
            response = Response()
            jobs = assistant.list_thread_compilation_jobs(
                thread.id,
                response=response,
                scenario_id=self.scenario.id,
                path=f"/scenarios/{self.scenario.id}",
                db=self.db,
            )
            self.assertEqual([item.id for item in jobs], [job.id])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            public = json.dumps(
                [item.model_dump(mode="json") for item in jobs],
                ensure_ascii=False,
            )
            self.assertNotIn("execution_input", public)
            self.assertNotIn("lease_token", public)

    def test_job_status_is_creator_scoped_and_hides_internal_failures(self) -> None:
        identity = self._identity(message="无效 JSON 输出")
        thread_id, message_id = self._claim_fixture()
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=identity,
            tenant_id=self.tenant.id,
            user_id=self.user.id,
            scenario_id=self.scenario.id,
            thread_id=thread_id,
            message_id=message_id,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline="baseline-v1",
            llm_call_budget=4,
        )
        self.assertTrue(acquired)
        raw_failure = (
            "JSONDecodeError at byte 9137; provider payload secret-token-123"
        )
        job_service.mark_failed(self.db, job.id, error=raw_failure)

        response = Response()
        status = assistant.get_compilation_job(
            job.id,
            response=response,
            db=self.db,
        )
        public = json.dumps(status.model_dump(mode="json"), ensure_ascii=False)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(status.error_code, job_service.ERROR_OUTPUT_INVALID)
        self.assertIn("零写入", status.error_message)
        self.assertNotIn("secret-token-123", public)
        self.assertNotIn("JSONDecodeError", public)
        for hidden in (
            "request_fingerprint",
            "message_hash",
            "attachment_content_hash",
            "llm_config_fingerprint",
            "mapping_context_fingerprint",
            "execution_policy_fingerprint",
        ):
            self.assertNotIn(hidden, public)
        with self.assertRaises(HTTPException) as failed_result:
            assistant.get_compilation_job_result(
                job.id,
                response=Response(),
                db=self.db,
            )
        self.assertEqual(failed_result.exception.status_code, 409)
        self.assertNotIn(
            "secret-token-123",
            json.dumps(failed_result.exception.detail, ensure_ascii=False),
        )

        other_user = User(
            id="other-compilation-user",
            tenant_id=self.tenant.id,
            email="other-compilation@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(other_user)
        self.db.flush()
        organization = permission_service.organization_for_principal(self.db)
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=other_user.id,
            role_key="admin",
        )
        self.db.commit()
        self.db.info["user_id"] = other_user.id
        try:
            with self.assertRaises(HTTPException) as foreign_status:
                assistant.get_compilation_job(
                    job.id,
                    response=Response(),
                    db=self.db,
                )
            self.assertEqual(foreign_status.exception.status_code, 404)
        finally:
            self.db.info["user_id"] = self.user.id

    def test_stream_and_sync_hide_invalid_json_parser_details(self) -> None:
        raw_failure = (
            "复合业务模型连续三次输出无效：JSONDecodeError byte 8129 "
            "provider-secret-fragment"
        )
        counts_before = self._formal_model_counts()

        def fake_compile(*_args, call_budget=None, **_kwargs):
            call_budget.consume("test_provider_call")
            raise ValueError(raw_failure)

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            stream_body = self._stream("验证流式 JSON 错误净化")
            stream_job = self.db.execute(
                select(AssistantCompilationJob)
            ).scalar_one()
            self._assert_inert_editable_completion(
                stream_job,
                formal_counts_before=counts_before,
            )
            sync_reply = assistant.chat(
                AssistantChatRequest(
                    message="验证同步 JSON 错误净化",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="draft",
                    draft_kind="scenario_model",
                ),
                self.db,
            )

        self.assertNotIn("provider-secret-fragment", stream_body)
        self.assertNotIn("JSONDecodeError", stream_body)
        self.assertNotIn("provider-secret-fragment", sync_reply.reply)
        self.assertNotIn("JSONDecodeError", sync_reply.reply)
        self.db.expire_all()
        jobs = self.db.execute(select(AssistantCompilationJob)).scalars().all()
        self.assertEqual(len(jobs), 2)
        sync_job = next(job for job in jobs if job.id != stream_job.id)
        self._assert_inert_editable_completion(
            sync_job,
            formal_counts_before=counts_before,
        )
        for job in jobs:
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.result["status"], "completed_with_gaps")
            status = assistant.get_compilation_job(
                job.id,
                response=Response(),
                db=self.db,
            )
            result = assistant.get_compilation_job_result(
                job.id,
                response=Response(),
                db=self.db,
            )
            public = json.dumps(
                {
                    "status": status.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
            self.assertNotIn("provider-secret-fragment", public)
            self.assertNotIn("JSONDecodeError", public)
            self.assertNotIn(raw_failure, public)

    def test_truncated_output_has_safe_explicit_retry_message(self) -> None:
        public = job_service.public_compilation_error(
            RuntimeError(
                "分块编译返回了在末尾截断的超长 JSON: provider-secret-fragment"
            )
        )
        self.assertEqual(public.code, job_service.ERROR_OUTPUT_TRUNCATED)
        self.assertIn("输出过长", public.message)
        self.assertIn("零写入", public.message)
        self.assertIn("显式重试", public.message)
        self.assertNotIn("provider-secret-fragment", public.message)

    def test_transient_provider_failure_has_safe_retry_message(self) -> None:
        public = job_service.public_compilation_error(
            scenario_model_compiler.CompilerProviderUnavailable(
                "Connection error: provider-secret-fragment"
            )
        )
        self.assertEqual(public.code, job_service.ERROR_PROVIDER_UNAVAILABLE)
        self.assertIn("暂时不可用", public.message)
        self.assertIn("零写入", public.message)
        self.assertNotIn("provider-secret-fragment", public.message)

    def test_historic_failed_compilation_message_is_redacted_on_read(self) -> None:
        thread_id, _message_id = self._claim_fixture()
        raw_failure = (
            "复合业务模型连续三次输出无效：JSONDecodeError "
            "provider-secret-fragment"
        )
        self.db.add(
            AssistantMessage(
                thread_id=thread_id,
                role="assistant",
                content=f"这次助手任务没有完成：{raw_failure}",
                context={
                    "draft_kind": "scenario_model",
                    "evidence": {
                        "rules_used": [],
                        "tools_called": [],
                        "confidence": 0.0,
                        "uncertainties": [raw_failure],
                    },
                },
                proposal={},
            )
        )
        self.db.commit()

        messages = assistant.list_thread_messages(
            thread_id,
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}",
            db=self.db,
        )
        historic = messages[-1]
        serialized = json.dumps(historic.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("provider-secret-fragment", serialized)
        self.assertNotIn("JSONDecodeError", serialized)
        self.assertIn("结构化结果不完整或无效", historic.content)
        self.assertEqual(
            historic.context.get("error_code"),
            job_service.ERROR_OUTPUT_INVALID,
        )

    def test_completion_preserves_inert_drafts_when_scenario_baseline_changes(self) -> None:
        counts_before = {
            model: self.db.scalar(select(func.count()).select_from(model))
            for model in (
                OntologyEntity,
                OntologyRelation,
                OntologyAction,
                OntologyRule,
                OntologyEvent,
                OntologyWorkflow,
            )
        }

        def fake_compile(*_args, call_budget=None, **_kwargs):
            call_budget.consume("test_provider_call")
            with self.factory() as changed_db:
                changed = changed_db.get(BusinessScenario, self.scenario.id)
                changed.description = "编译期间由另一个受控请求更新"
                changed_db.commit()
            compiled = self._compiled_payload()
            entity = {
                "key": "entity.baseline_candidate",
                "name": "基线漂移候选项目",
                "description": "编译结果必须保留但不得正式写入",
                "properties": [],
                "evidence_refs": [],
            }
            rule = {
                "key": "rule.baseline_candidate",
                "name": "基线漂移候选规则",
                "entity_ref": "entity.baseline_candidate",
                "condition": {"expression": "项目状态 != null"},
                "severity": "warning",
                "evidence_refs": [],
            }
            compiled["entities"] = [entity]
            compiled["rules"] = [rule]
            compiled["changes"] = [
                {
                    "change_id": entity["key"],
                    "operation": "add",
                    "resource": "entity",
                    "name": entity["name"],
                },
                {
                    "change_id": rule["key"],
                    "operation": "add",
                    "resource": "rule",
                    "name": rule["name"],
                },
            ]
            compiled["draft_candidates"] = [
                {
                    "resource_kind": "entity",
                    "resource_key": entity["key"],
                    "task_id": "ontology",
                    "payload": copy.deepcopy(entity),
                    "evidence_refs": [],
                    "validation_issues": [],
                    "validation_status": "valid",
                },
                {
                    "resource_kind": "rule",
                    "resource_key": rule["key"],
                    "task_id": "rules",
                    "payload": copy.deepcopy(rule),
                    "evidence_refs": [],
                    "validation_issues": [],
                    "validation_status": "valid",
                },
            ]
            return compiled

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            body = self._stream("验证完成时场景基线")

        self.db.expire_all()
        job = self.db.execute(select(AssistantCompilationJob)).scalar_one()
        self.assertEqual(job.status, "succeeded")
        proposal = job.result
        model_payload = proposal["payload"]
        self.assertEqual(proposal["status"], "completed_with_gaps")
        self.assertFalse(proposal["requires_confirmation"])
        self.assertEqual(proposal["changes"], [])
        self.assertEqual(model_payload["changes"], [])
        self.assertTrue(all(
            model_payload.get(section) == []
            for section in assistant._SCENARIO_MODEL_RESOURCE_SECTIONS
        ))
        self.assertEqual(
            model_payload["baseline_guard"]["status"],
            "changed_during_compilation",
        )
        self.assertTrue(model_payload["execution_summary"]["final"])
        self.assertEqual(
            model_payload["execution_summary"]["status"],
            "completed_with_gaps",
        )
        self.assertEqual(model_payload["current_task_id"], "")
        self.assertIn("BASELINE_CHANGED_DURING_COMPILATION", body)
        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.compilation_job_id == job.id
            )
        ).all())
        self.assertEqual(
            {row.resource_key for row in rows},
            {"entity.baseline_candidate", "rule.baseline_candidate"},
        )
        self.assertTrue(all(
            row.draft_status == "needs_attention"
            and not row.enabled
            and not row.publishable
            for row in rows
        ))
        self.assertTrue(all(
            any(
                issue["code"] == "BASELINE_CHANGED_DURING_COMPILATION"
                for issue in row.validation_issues
            )
            for row in rows
        ))
        self.assertEqual(
            counts_before,
            {
                model: self.db.scalar(select(func.count()).select_from(model))
                for model in counts_before
            },
        )

    def test_progress_and_terminal_writes_use_independent_sessions(self) -> None:
        compile_session_ids: set[int] = set()
        progress_session_ids: set[int] = set()
        terminal_session_ids: set[int] = set()
        original_record = job_service.record_provider_call
        original_succeeded = job_service.mark_succeeded

        def fake_compile(compile_db, *_args, call_budget=None, **_kwargs):
            compile_session_ids.add(id(compile_db))
            call_budget.consume("test_provider_call")
            return self._compiled_payload()

        def record_spy(progress_db, *args, **kwargs):
            progress_session_ids.add(id(progress_db))
            return original_record(progress_db, *args, **kwargs)

        def succeeded_spy(terminal_db, *args, **kwargs):
            terminal_session_ids.add(id(terminal_db))
            return original_succeeded(terminal_db, *args, **kwargs)

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
            patch.object(job_service, "record_provider_call", side_effect=record_spy),
            patch.object(job_service, "mark_succeeded", side_effect=succeeded_spy),
        ):
            self._stream("验证独立任务账本事务")

        self.assertTrue(compile_session_ids)
        self.assertTrue(progress_session_ids)
        self.assertTrue(terminal_session_ids)
        self.assertTrue(compile_session_ids.isdisjoint(progress_session_ids))
        self.assertTrue(compile_session_ids.isdisjoint(terminal_session_ids))


if __name__ == "__main__":
    unittest.main()
