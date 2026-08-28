from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import LLMEvaluationRecord, LLMConfig, LLMInvocationTrace, Tenant, User
from app.routers.llm_configs import (
    _out,
    create_evaluation,
    evaluation_summary,
    resolve_llm,
    usage_summary,
)
from app.schemas import LLMEvaluationIn, LLMConfigIn
from app.services import assistant_orchestrator, llm_service, permission_service
from app.services.assistant_orchestrator import AssistantSemanticDecision


class _CompletionClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kwargs):
        if self.error:
            raise self.error
        return self.response


class _StreamClient:
    def __init__(self, chunks):
        self.chunks = chunks
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kwargs):
        return iter(self.chunks)


class _EmbeddingClient:
    def __init__(self, vectors):
        self.vectors = vectors
        self.embeddings = SimpleNamespace(create=self.create)

    def create(self, **_kwargs):
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=vector) for vector in self.vectors],
            usage=SimpleNamespace(prompt_tokens=6, completion_tokens=0, total_tokens=6),
        )


def _response(content: str = "ok", *, prompt_tokens: int = 11, completion_tokens: int = 7):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


class LLMManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-llm", name="模型租户")
        self.owner = User(
            id="user-llm-owner",
            tenant_id=self.tenant.id,
            email="owner-llm@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add_all([self.tenant, self.owner])
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.owner.id
        )
        self.db.commit()
        self.config = LLMConfig(
            id="cfg-primary",
            tenant_id=self.tenant.id,
            name="主模型",
            provider="openai-compatible",
            api_key="sk-secret-value-should-not-leak",
            model="example-chat",
            capabilities=["chat", "tool"],
            enabled=True,
            routing_priority=20,
            input_cost_per_million=2.0,
            output_cost_per_million=4.0,
            budget_limit=10.0,
        )
        self.db.add(self.config)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _trace_session(self):
        return Session(self.engine)

    def test_config_contract_hides_key_and_routes_enabled_capabilities(self) -> None:
        disabled = LLMConfig(
            id="cfg-disabled",
            tenant_id=self.tenant.id,
            name="已停用",
            model="disabled",
            capabilities=["chat"],
            enabled=False,
            routing_priority=1,
        )
        vision = LLMConfig(
            id="cfg-vision",
            tenant_id=self.tenant.id,
            name="视觉模型",
            model="vision",
            capabilities=["vision"],
            enabled=True,
            routing_priority=1,
        )
        secondary = LLMConfig(
            id="cfg-secondary",
            tenant_id=self.tenant.id,
            name="备用模型",
            model="backup",
            capabilities=["chat"],
            enabled=True,
            routing_priority=30,
        )
        self.db.add_all([disabled, vision, secondary])
        self.db.commit()

        payload = LLMConfigIn(name="验证", capabilities=["CHAT", "tool", "chat"])
        self.assertEqual(payload.capabilities, ["chat", "tool"])
        with self.assertRaises(ValueError):
            LLMConfigIn(name="非法工具模型", capabilities=["tool"])
        self.assertEqual(_out(self.config).api_key, "")
        self.assertNotIn("sk-secret", _out(self.config).model_dump_json())

        route = resolve_llm("chat", db=self.db)
        self.assertEqual(route.selected.id, self.config.id)
        self.assertEqual([item.id for item in route.candidates], [self.config.id, secondary.id])
        self.assertEqual(resolve_llm("vision", db=self.db).selected.id, vision.id)

    def test_sync_call_persists_costed_trace_without_prompt_or_key(self) -> None:
        with (
            patch("app.services.llm_service._client", return_value=_CompletionClient(_response("业务回复"))),
            patch(
                "app.database.SessionLocal",
                side_effect=AssertionError("不得把隔离会话的 trace 写入默认数据库"),
            ) as global_session,
        ):
            result = llm_service.chat(
                self.config,
                [{"role": "user", "content": "包含敏感业务内容的提示词"}],
                db=self.db,
            )
        global_session.assert_not_called()
        self.assertEqual(result["content"], "业务回复")
        trace = Session(self.engine).execute(select(LLMInvocationTrace)).scalars().one()
        self.assertEqual(trace.status, "succeeded")
        self.assertEqual(trace.operation, "chat")
        self.assertEqual(trace.provider, "openai-compatible")
        self.assertEqual(trace.model, "example-chat")
        self.assertEqual(trace.input_tokens, 11)
        self.assertEqual(trace.output_tokens, 7)
        self.assertAlmostEqual(trace.estimated_cost, 0.00005)
        self.assertFalse(hasattr(trace, "prompt"))
        self.assertNotIn("secret", trace.error)

    def test_chat_uses_configured_default_request_timeout(self) -> None:
        client = _CompletionClient(_response())
        with (
            patch("app.services.llm_service.get_settings", return_value=SimpleNamespace(llm_timeout=47.0)),
            patch("app.services.llm_service.OpenAI", return_value=client) as openai,
            patch("app.database.SessionLocal", side_effect=self._trace_session),
        ):
            llm_service.chat(
                self.config,
                [{"role": "user", "content": "使用普通调用默认超时"}],
                db=self.db,
            )

        openai.assert_called_once_with(
            base_url=None,
            api_key="sk-secret-value-should-not-leak",
            timeout=47.0,
        )

    def test_chat_forwards_request_timeout_and_retry_override(self) -> None:
        client = _CompletionClient(_response())
        with (
            patch("app.services.llm_service.OpenAI", return_value=client) as openai,
            patch("app.database.SessionLocal", side_effect=self._trace_session),
        ):
            llm_service.chat(
                self.config,
                [{"role": "user", "content": "使用场景编译请求边界"}],
                request_timeout=480.0,
                max_retries=0,
                db=self.db,
            )

        openai.assert_called_once_with(
            base_url=None,
            api_key="sk-secret-value-should-not-leak",
            timeout=480.0,
            max_retries=0,
        )

    def test_structured_chat_falls_back_to_validated_json_for_basic_compatible_models(self) -> None:
        structured_model = Mock()
        structured_model.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "provider does not support structured output"
        )
        fallback_decision = AssistantSemanticDecision(
            goal="answer",
            scope="workflow",
            confidence="high",
            reason="用户询问已有工作流数量",
        )
        with (
            patch("app.services.llm_service.ChatOpenAI", return_value=structured_model),
            patch(
                "app.services.llm_service.chat",
                return_value={"content": fallback_decision.model_dump_json()},
            ) as fallback_chat,
        ):
            result = llm_service.structured_chat(
                self.config,
                [{"role": "user", "content": "当前有多少个工作流？"}],
                AssistantSemanticDecision,
                db=self.db,
                operation="assistant_route",
                request_timeout=15,
                max_tokens=384,
                max_retries=0,
            )

        self.assertEqual(result, fallback_decision)
        self.assertEqual(structured_model.with_structured_output.call_count, 2)
        fallback_chat.assert_called_once()
        fallback_call = fallback_chat.call_args
        self.assertIn("Return only one JSON object", fallback_call.args[1][0]["content"])
        self.assertEqual(fallback_call.kwargs["operation"], "assistant_route")
        self.assertGreater(fallback_call.kwargs["request_timeout"], 0)
        self.assertLessEqual(fallback_call.kwargs["request_timeout"], 15)
        self.assertEqual(fallback_call.kwargs["max_retries"], 0)
        self.assertFalse(fallback_call.kwargs["retry_on_length"])

    def test_structured_chat_reserves_deadline_for_each_compatible_strategy(self) -> None:
        structured_model = Mock()
        structured_model.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "structured mode unavailable"
        )
        fallback_decision = AssistantSemanticDecision(
            goal="create",
            scope="scenario_model",
            confidence="high",
            reason="用户明确要求创建完整场景模型",
        )
        with (
            patch(
                "app.services.llm_service.time.monotonic",
                side_effect=[0.0, 0.0, 10.0, 20.0],
            ),
            patch(
                "app.services.llm_service.ChatOpenAI",
                return_value=structured_model,
            ) as structured_client,
            patch(
                "app.services.llm_service.chat",
                return_value={"content": fallback_decision.model_dump_json()},
            ) as fallback_chat,
        ):
            result = llm_service.structured_chat(
                self.config,
                [{"role": "user", "content": "请完成整个业务场景的建模"}],
                AssistantSemanticDecision,
                db=self.db,
                operation="assistant_route",
                request_timeout=30,
                max_tokens=1024,
                max_retries=0,
            )

        self.assertEqual(result, fallback_decision)
        self.assertEqual(structured_client.call_count, 2)
        self.assertAlmostEqual(
            structured_client.call_args_list[0].kwargs["timeout"], 10.0
        )
        self.assertAlmostEqual(
            structured_client.call_args_list[1].kwargs["timeout"], 10.0
        )
        self.assertAlmostEqual(
            fallback_chat.call_args.kwargs["request_timeout"], 10.0
        )

    def test_assistant_router_uses_reasoning_safe_completion_budget(self) -> None:
        response = {
            "content": "",
            "tool_calls": [{
                "id": "call-route",
                "type": "function",
                "function": {
                    "name": "compile_scenario_model",
                    "arguments": {
                        "goal": "create",
                        "confidence": "high",
                        "reason": "用户明确要求根据附件创建完整场景模型",
                    },
                },
            }],
        }
        with patch.object(
            assistant_orchestrator.llm_service,
            "chat",
            return_value=response,
        ) as route_chat:
            plan = assistant_orchestrator.plan_assistant_request(
                llm=self.config,
                db=self.db,
                message="请阅读附件并完成整个业务场景的场景建模",
                history=[],
                page="场景建模",
                path="/scenarios/scenario-1",
                mode="ask",
                preferred_scope="auto",
                has_scenario=True,
                has_attachments=True,
                has_active_model_drafts=False,
                active_draft_scopes=[],
                context_summary="当前场景尚无正式资源。",
            )

        self.assertEqual(plan.intent, "scenario_model")
        self.assertEqual(plan.capability, "compile_scenario_model")
        self.assertEqual(plan.public_context()["capability_label"], "附件理解与场景建模")
        call = route_chat.call_args
        self.assertEqual(call.kwargs["operation"], "assistant_route")
        self.assertEqual(
            call.kwargs["request_timeout"],
            assistant_orchestrator.ROUTE_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertGreater(call.kwargs["request_timeout"], 10)
        self.assertEqual(
            call.kwargs["max_tokens"],
            assistant_orchestrator.ROUTE_MAX_COMPLETION_TOKENS,
        )
        self.assertGreaterEqual(call.kwargs["max_tokens"], 768)
        self.assertEqual(call.kwargs["tool_choice"], "required")
        self.assertFalse(call.kwargs["retry_on_length"])
        self.assertEqual(call.kwargs["max_retries"], 0)
        self.assertIn(
            "compile_scenario_model",
            {tool["function"]["name"] for tool in call.kwargs["tools"]},
        )

    def test_assistant_router_keeps_attachment_request_read_only_when_planner_times_out(self) -> None:
        with patch.object(
            assistant_orchestrator.llm_service,
            "chat",
            side_effect=TimeoutError("route timeout"),
        ) as route_chat:
            plan = assistant_orchestrator.plan_assistant_request(
                llm=self.config,
                db=self.db,
                message="请根据上传资料完成当前场景的完整建设",
                history=[],
                page="场景建模",
                path="/scenarios/scenario-1",
                mode="ask",
                preferred_scope="auto",
                has_scenario=True,
                has_attachments=True,
                has_active_model_drafts=False,
                active_draft_scopes=[],
                context_summary="当前场景尚无正式资源。",
            )

        self.assertEqual(plan.intent, "chat")
        self.assertEqual(plan.capability, "answer_question")
        self.assertEqual(plan.source, "model_fallback")
        self.assertIn("附件不会自动触发建模", plan.policy_note)
        self.assertEqual(route_chat.call_args.kwargs["max_retries"], 0)

    def test_assistant_router_accepts_provider_tool_selection_without_arguments(self) -> None:
        with patch.object(
            assistant_orchestrator.llm_service,
            "chat",
            return_value={
                "content": "",
                "tool_calls": [{
                    "id": "call-route",
                    "type": "function",
                    "function": {
                        "name": "compile_scenario_model",
                        "arguments": {},
                    },
                }],
            },
        ):
            plan = assistant_orchestrator.plan_assistant_request(
                llm=self.config,
                db=self.db,
                message="请根据附件创建完整场景模型草稿",
                history=[],
                page="场景建模",
                path="/scenarios/scenario-1",
                mode="ask",
                preferred_scope="auto",
                has_scenario=True,
                has_attachments=True,
                has_active_model_drafts=False,
                active_draft_scopes=[],
                context_summary="附件包含业务对象和处置流程。",
            )

        self.assertEqual(plan.intent, "scenario_model")
        self.assertEqual(plan.capability, "compile_scenario_model")
        self.assertEqual(plan.decision.goal, "create")
        self.assertEqual(plan.decision.confidence, "high")
        self.assertEqual(plan.source, "model")

    def test_assistant_router_does_not_resume_model_draft_for_publish_question_after_timeout(self) -> None:
        with patch.object(
            assistant_orchestrator.llm_service,
            "chat",
            side_effect=TimeoutError("route timeout"),
        ):
            plan = assistant_orchestrator.plan_assistant_request(
                llm=self.config,
                db=self.db,
                message="如果需要达到可发布状态，我现在需要做些什么？",
                history=[],
                page="场景建模",
                path="/scenarios/scenario-1",
                mode="ask",
                preferred_scope="auto",
                has_scenario=True,
                has_attachments=False,
                has_active_model_drafts=True,
                active_draft_scopes=["scenario_model"],
                context_summary="当前场景存在一份待确认的完整建模草稿。",
            )

        self.assertEqual(plan.intent, "chat")
        self.assertEqual(plan.decision.goal, "answer")
        self.assertEqual(plan.source, "model_fallback")
        self.assertIn("普通回答", plan.policy_note)
        self.assertIn("发布前需要什么", assistant_orchestrator._ROUTER_SYSTEM_PROMPT)

    def test_structured_chat_accepts_complete_json_after_provider_preamble(self) -> None:
        structured_model = Mock()
        structured_model.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "structured mode unavailable"
        )
        decision = AssistantSemanticDecision(
            goal="create",
            scope="scenario_model",
            confidence="high",
            reason="用户明确要求创建完整场景模型",
        )
        provider_content = (
            "路由结果如下：\n```json\n"
            + decision.model_dump_json()
            + "\n```\n请按该结构处理。"
        )
        with (
            patch("app.services.llm_service.ChatOpenAI", return_value=structured_model),
            patch(
                "app.services.llm_service.chat",
                return_value={"content": provider_content},
            ),
        ):
            result = llm_service.structured_chat(
                self.config,
                [{"role": "user", "content": "请完成整个业务场景的建模"}],
                AssistantSemanticDecision,
                db=self.db,
                operation="assistant_route",
                request_timeout=20,
                max_tokens=2048,
                max_retries=0,
            )

        self.assertEqual(result, decision)

    def test_structured_chat_does_not_repair_truncated_json(self) -> None:
        structured_model = Mock()
        structured_model.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "structured mode unavailable"
        )
        truncated = (
            '{"goal":"create","scope":"scenario_model",'
            '"confidence":"high","reason":"未结束"'
        )
        with (
            patch("app.services.llm_service.ChatOpenAI", return_value=structured_model),
            patch(
                "app.services.llm_service.chat",
                return_value={"content": truncated},
            ),
            self.assertRaisesRegex(ValueError, "完整且符合结构约束"),
        ):
            llm_service.structured_chat(
                self.config,
                [{"role": "user", "content": "请完成整个业务场景的建模"}],
                AssistantSemanticDecision,
                db=self.db,
                operation="assistant_route",
                request_timeout=20,
                max_tokens=2048,
                max_retries=0,
            )

    def test_stream_completion_and_failure_record_sanitized_traces(self) -> None:
        token_chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="流式", tool_calls=None))],
            usage=None,
        )
        final_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=3, total_tokens=11),
        )
        with (
            patch("app.services.llm_service._client", return_value=_StreamClient([token_chunk, final_chunk])),
            patch("app.database.SessionLocal", side_effect=self._trace_session),
        ):
            events = list(llm_service.chat_stream(self.config, [{"role": "user", "content": "stream"}], db=self.db))
        self.assertEqual(events, [{"type": "token", "content": "流式"}])

        with (
            patch(
                "app.services.llm_service._client",
                return_value=_CompletionClient(error=RuntimeError("api_key=sk-secret-value-should-not-leak")),
            ),
            patch("app.database.SessionLocal", side_effect=self._trace_session),
        ):
            with self.assertRaises(RuntimeError):
                llm_service.chat(self.config, [{"role": "user", "content": "will fail"}], db=self.db)

        traces = Session(self.engine).execute(
            select(LLMInvocationTrace).order_by(LLMInvocationTrace.created_at)
        ).scalars().all()
        self.assertEqual([trace.status for trace in traces], ["succeeded", "failed"])
        self.assertEqual(traces[0].operation, "chat_stream")
        self.assertEqual((traces[0].input_tokens, traces[0].output_tokens), (8, 3))
        self.assertEqual(traces[1].error, "RuntimeError: provider 调用失败")
        self.assertNotIn("sk-secret", traces[1].error)

    def test_usage_and_evaluation_summaries_are_owner_scoped(self) -> None:
        self.db.add_all(
            [
                LLMInvocationTrace(
                    tenant_id=self.tenant.id,
                    llm_config_id=self.config.id,
                    provider=self.config.provider,
                    model=self.config.model,
                    capability="chat",
                    operation="chat",
                    status="succeeded",
                    latency_ms=120,
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                    estimated_cost=0.005,
                    currency="USD",
                ),
                LLMInvocationTrace(
                    tenant_id=self.tenant.id,
                    llm_config_id=self.config.id,
                    provider=self.config.provider,
                    model=self.config.model,
                    capability="tool",
                    operation="chat_stream",
                    status="failed",
                    latency_ms=50,
                    input_tokens=30,
                    output_tokens=0,
                    total_tokens=30,
                    estimated_cost=0.001,
                    currency="USD",
                ),
            ]
        )
        self.db.commit()
        usage = usage_summary(self.config.id, days=30, db=self.db)
        self.assertEqual(usage.invocation_count, 2)
        self.assertEqual(usage.failed_count, 1)
        self.assertEqual(usage.input_tokens, 130)
        self.assertAlmostEqual(usage.budget_remaining or 0, 9.994)
        self.assertEqual(usage.by_capability["chat"]["invocation_count"], 1)

        evaluation = create_evaluation(
            self.config.id,
            LLMEvaluationIn(
                name="工具准确率",
                passed=True,
                score=0.9,
                notes="authorization=Bearer sk-secret-value-should-not-leak",
                metrics={"api_key": "sk-secret-value-should-not-leak", "accuracy": 0.9},
            ),
            db=self.db,
        )
        self.assertNotIn("sk-secret", evaluation.notes)
        record = self.db.get(LLMEvaluationRecord, evaluation.id)
        self.assertEqual(record.metrics["api_key"], "[REDACTED]")
        summary = evaluation_summary(self.config.id, db=self.db)
        self.assertEqual((summary.total, summary.passed, summary.failed), (1, 1, 0))
        self.assertAlmostEqual(summary.average_score, 0.9)

    def test_embedding_runtime_and_trace_context_are_real_provider_calls(self) -> None:
        embedding = LLMConfig(
            id="cfg-embedding",
            tenant_id=self.tenant.id,
            name="Embedding",
            model="example-embedding",
            capabilities=["embedding"],
            enabled=True,
            input_cost_per_million=1.0,
        )
        self.db.add(embedding)
        self.db.commit()
        self.db.info["llm_trace_context"] = {
            "correlation_id": "request-1",
            "agent_id": "agent-1",
            "conversation_id": "conversation-1",
            "scenario_id": "scenario-1",
        }
        with (
            patch("app.services.llm_service._client", return_value=_EmbeddingClient([[0.2, 0.8]])),
            patch("app.database.SessionLocal", side_effect=self._trace_session),
        ):
            vectors = llm_service.embed(embedding, ["文本"], db=self.db)
        self.assertEqual(vectors, [[0.2, 0.8]])
        trace = Session(self.engine).execute(
            select(LLMInvocationTrace).where(LLMInvocationTrace.llm_config_id == embedding.id)
        ).scalars().one()
        self.assertEqual(trace.capability, "embedding")
        self.assertEqual(trace.correlation_id, "request-1")
        self.assertEqual(trace.agent_id, "agent-1")
        self.assertEqual(trace.conversation_id, "conversation-1")
        self.assertEqual(trace.scenario_id, "scenario-1")

    def test_budget_limit_blocks_provider_before_call(self) -> None:
        self.config.budget_limit = 0.01
        self.db.add(
            LLMInvocationTrace(
                tenant_id=self.tenant.id,
                llm_config_id=self.config.id,
                provider=self.config.provider,
                model=self.config.model,
                capability="chat",
                operation="chat",
                status="succeeded",
                estimated_cost=0.01,
            )
        )
        self.db.commit()
        with patch("app.services.llm_service._client") as client:
            with self.assertRaises(llm_service.LLMRuntimeError):
                llm_service.chat(
                    self.config,
                    [{"role": "user", "content": "不应发送到 provider"}],
                    db=self.db,
                )
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
