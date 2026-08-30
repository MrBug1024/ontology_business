"""Security and compatibility contract for Agent business-capability scopes."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import database as database_module
from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    FunctionDefinition,
    LLMConfig,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyProperty,
    OntologyRelease,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
)
from app.routers import agents as agents_router
from app.schemas import AgentCapabilityScope, AgentIn
from app.services import (
    agent_capability_service,
    agent_engine,
    permission_service,
    release_service,
    runtime_definition_service,
)


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"amount": {"type": "number"}},
        "required": ["amount"],
        "additionalProperties": False,
    }


def _scope(
    category: str | None = None,
    *resource_ids: str,
) -> AgentCapabilityScope:
    values = agent_capability_service.explicit_empty_scope()
    if category:
        values[category] = {"mode": "explicit", "selected_ids": list(resource_ids)}
    return AgentCapabilityScope.model_validate(values)


class AgentCapabilityScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.db: Session = self.Session()
        self.tenant = Tenant(id="tenant-agent-scope", name="能力范围租户")
        self.owner = User(
            id="owner-agent-scope",
            tenant_id=self.tenant.id,
            email="owner-agent-scope@example.test",
            password_hash="test-only",
            status="active",
        )
        self.operator = User(
            id="operator-agent-scope",
            tenant_id=self.tenant.id,
            email="operator-agent-scope@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-agent-scope-a",
            tenant_id=self.tenant.id,
            name="场景 A",
        )
        self.other_scenario = BusinessScenario(
            id="scenario-agent-scope-b",
            tenant_id=self.tenant.id,
            name="场景 B",
        )
        self.entity = OntologyEntity(
            id="entity-agent-scope-a",
            scenario_id=self.scenario.id,
            name="项目",
        )
        self.entity.properties = [
            OntologyProperty(
                id="property-agent-scope-a-key",
                name="项目编号",
                data_type="string",
                is_key=True,
                is_title=True,
                is_required=True,
            )
        ]
        self.other_entity = OntologyEntity(
            id="entity-agent-scope-b",
            scenario_id=self.other_scenario.id,
            name="合同",
        )
        self.function_a = FunctionDefinition(
            id="function-agent-scope-a",
            scenario_id=self.scenario.id,
            name="范围内函数",
            input_schema=_schema(),
            output_schema={"type": "object", "properties": {}},
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 1}},
        )
        self.function_b = FunctionDefinition(
            id="function-agent-scope-b",
            scenario_id=self.scenario.id,
            name="未授权函数",
            input_schema=_schema(),
            output_schema={"type": "object", "properties": {}},
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 2}},
        )
        self.foreign_function = FunctionDefinition(
            id="function-agent-scope-x",
            scenario_id=self.other_scenario.id,
            name="其他场景函数",
            input_schema=_schema(),
            output_schema={"type": "object", "properties": {}},
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 3}},
        )
        self.restricted_action = OntologyAction(
            id="action-agent-scope-secret",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            name="受限操作",
            input_schema={"type": "object", "properties": {}},
            executor_type="script",
            executor_config={},
            access_scope="restricted",
            enabled=True,
        )
        self.restricted_workflow = OntologyWorkflow(
            id="workflow-agent-scope-secret",
            scenario_id=self.scenario.id,
            name="包含受限操作的工作流",
            trigger_type="manual",
            nodes=[
                {"id": "start", "type": "start", "data": {}},
                {
                    "id": "action",
                    "type": "action",
                    "data": {"action_id": self.restricted_action.id},
                },
                {"id": "end", "type": "end", "data": {}},
            ],
            edges=[
                {"id": "one", "source": "start", "target": "action"},
                {"id": "two", "source": "action", "target": "end"},
            ],
            status="active",
            enabled=True,
        )
        self.legacy_agent = Agent(
            id="agent-scope-legacy",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="旧版 Agent",
            capability_scope=None,
        )
        self.db.add_all([
            self.tenant,
            self.owner,
            self.operator,
            self.scenario,
            self.other_scenario,
            self.entity,
            self.other_entity,
            self.function_a,
            self.function_b,
            self.foreign_function,
            self.restricted_action,
            self.restricted_workflow,
            self.legacy_agent,
        ])
        self.db.commit()
        organization = permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.owner.id,
        )
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=self.operator.id,
            role_key="operator",
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _context(self, agent: Agent) -> agent_engine.AgentContext:
        return agent_engine.AgentContext(self.db, agent, LLMConfig(name="工具模型"))

    def test_new_agents_are_opt_in_but_legacy_agents_keep_their_previous_runtime(self) -> None:
        created_out = agents_router.create_agent(
            AgentIn(name="新 Agent", scenario_id=self.scenario.id),
            self.db,
        )
        created = self.db.get(Agent, created_out.id)
        self.assertEqual(created.capability_scope, agent_capability_service.explicit_empty_scope())
        self.assertEqual(created.runtime_binding_mode, "capability_only")
        self.assertFalse(created_out.capability_scope_legacy)
        created_context = self._context(created)
        self.assertEqual(created_context.functions, [])
        created_tool_names = {
            tool["function"]["name"] for tool in created_context.build_tools()
        }
        self.assertIn("list_ontology_model", created_tool_names)
        self.assertNotIn("list_functions", created_tool_names)

        legacy_out = agents_router._out(self.legacy_agent, self.db)
        self.assertTrue(legacy_out.capability_scope_legacy)
        self.assertTrue(all(
            entry["mode"] == "explicit"
            for entry in legacy_out.capability_scope.model_dump().values()
        ))
        self.assertEqual(
            set(legacy_out.capability_scope.functions.selected_ids),
            {self.function_a.id, self.function_b.id},
        )
        # NULL is the legacy representation from before capability scopes were
        # introduced.  It must keep the previous scenario-visible behaviour so
        # upgrading the platform does not silently disable existing Agents.
        self.assertEqual(
            {item.id for item in self._context(self.legacy_agent).functions},
            {self.function_a.id, self.function_b.id},
        )

    def test_new_agent_default_can_be_rolled_back_without_rewriting_existing_rows(self) -> None:
        with patch.object(
            agents_router,
            "get_settings",
            return_value=SimpleNamespace(new_agent_runtime_binding_mode="legacy"),
        ):
            created_out = agents_router.create_agent(
                AgentIn(name="回退模式 Agent", scenario_id=self.scenario.id),
                self.db,
            )

        created = self.db.get(Agent, created_out.id)
        self.assertEqual(created.runtime_binding_mode, "legacy")
        self.assertEqual(self.legacy_agent.runtime_binding_mode, "legacy")

    def test_unrelated_edit_freezes_legacy_scope_without_losing_capabilities(self) -> None:
        updated = agents_router.update_agent(
            self.legacy_agent.id,
            AgentIn(name="旧版 Agent（已重命名）", scenario_id=self.scenario.id),
            self.db,
        )
        stored = self.db.get(Agent, self.legacy_agent.id)
        self.assertIsNotNone(stored.capability_scope)
        self.assertFalse(updated.capability_scope_legacy)
        self.assertEqual(
            set(stored.capability_scope["functions"]["selected_ids"]),
            {self.function_a.id, self.function_b.id},
        )
        self.assertEqual(
            {item.id for item in self._context(stored).functions},
            {self.function_a.id, self.function_b.id},
        )

    def test_select_current_all_is_frozen_to_explicit_ids(self) -> None:
        created_out = agents_router.create_agent(
            AgentIn(
                name="冻结当前全部能力",
                scenario_id=self.scenario.id,
                capability_scope=agent_capability_service.legacy_all_scope(),
            ),
            self.db,
        )
        stored = self.db.get(Agent, created_out.id)
        self.assertTrue(all(
            entry["mode"] == "explicit"
            for entry in stored.capability_scope.values()
        ))
        self.assertEqual(
            set(stored.capability_scope["functions"]["selected_ids"]),
            {self.function_a.id, self.function_b.id},
        )

        future = FunctionDefinition(
            id="function-agent-scope-future",
            scenario_id=self.scenario.id,
            name="授权后新增函数",
            input_schema=_schema(),
            output_schema={"type": "object", "properties": {}},
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 4}},
        )
        self.db.add(future)
        self.db.commit()
        self.assertEqual(
            {item.id for item in self._context(stored).functions},
            {self.function_a.id, self.function_b.id},
        )

    def test_scope_filters_prompt_tools_and_forced_direct_calls(self) -> None:
        agent = Agent(
            id="agent-scope-explicit",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="显式范围 Agent",
            capability_scope=_scope("functions", self.function_a.id).model_dump(),
        )
        self.db.add(agent)
        self.db.commit()
        context = self._context(agent)
        self.assertEqual([item.id for item in context.functions], [self.function_a.id])
        prompt = agent_engine.build_system_prompt(context, self.scenario.name, "")
        self.assertIn(self.function_a.name, prompt)
        self.assertNotIn(self.function_b.name, prompt)
        self.assertIn("未找到函数", context.execute_tool(
            "run_function",
            {"function_id": self.function_b.id, "params": {"amount": 1}},
        ))

    def test_all_mode_requires_a_resolvable_runtime_definition(self) -> None:
        all_scope = agent_capability_service.explicit_empty_scope()
        all_scope["functions"] = {"mode": "all", "selected_ids": []}
        with patch.object(
            agents_router.runtime_definition_service,
            "resolve_active",
            side_effect=runtime_definition_service.RuntimeDefinitionError("staging 尚未发布"),
        ):
            with self.assertRaises(HTTPException) as error:
                agents_router.create_agent(
                    AgentIn(
                        name="全部能力 Agent",
                        scenario_id=self.scenario.id,
                        capability_scope=AgentCapabilityScope.model_validate(all_scope),
                    ),
                    self.db,
                )
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("尚未发布", str(error.exception.detail))

    def test_cross_scenario_and_hidden_ids_are_rejected_without_leaking_identity(self) -> None:
        with self.assertRaises(HTTPException) as cross_error:
            agents_router.create_agent(
                AgentIn(
                    name="越界 Agent",
                    scenario_id=self.scenario.id,
                    capability_scope=_scope("functions", self.foreign_function.id),
                ),
                self.db,
            )
        self.assertEqual(cross_error.exception.status_code, 400)
        self.assertIn("不属于当前场景或无权读取", str(cross_error.exception.detail))
        self.assertNotIn(self.foreign_function.name, str(cross_error.exception.detail))

        self.db.info["user_id"] = self.operator.id
        with self.assertRaises(HTTPException) as hidden_error:
            agents_router.create_agent(
                AgentIn(
                    name="隐藏能力 Agent",
                    scenario_id=self.scenario.id,
                    capability_scope=_scope("actions", self.restricted_action.id),
                ),
                self.db,
            )
        self.assertEqual(hidden_error.exception.status_code, 400)
        self.assertIn("不属于当前场景或无权读取", str(hidden_error.exception.detail))
        self.assertNotIn(self.restricted_action.name, str(hidden_error.exception.detail))
        with self.assertRaises(HTTPException) as nested_hidden_error:
            agents_router.create_agent(
                AgentIn(
                    name="工作流旁路 Agent",
                    scenario_id=self.scenario.id,
                    capability_scope=_scope("workflows", self.restricted_workflow.id),
                ),
                self.db,
            )
        self.assertEqual(nested_hidden_error.exception.status_code, 400)
        self.assertNotIn(self.restricted_workflow.name, str(nested_hidden_error.exception.detail))

    def test_scenario_switch_without_scope_clears_even_a_legacy_agent(self) -> None:
        updated = agents_router.update_agent(
            self.legacy_agent.id,
            AgentIn(name=self.legacy_agent.name, scenario_id=self.other_scenario.id),
            self.db,
        )
        stored = self.db.get(Agent, self.legacy_agent.id)
        self.assertEqual(stored.capability_scope, agent_capability_service.explicit_empty_scope())
        self.assertFalse(updated.capability_scope_legacy)
        self.assertTrue(all(
            entry["mode"] == "explicit" and not entry["selected_ids"]
            for entry in updated.capability_scope.model_dump().values()
        ))

    def test_staging_catalog_and_runtime_use_the_frozen_release(self) -> None:
        branch = OntologyBranch(
            id="branch-agent-scope",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="main",
            created_by_user_id=self.owner.id,
        )
        self.db.add(branch)
        self.db.flush()
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        snapshot = OntologySnapshot(
            id="snapshot-agent-scope",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.owner.id,
        )
        self.db.add(snapshot)
        self.db.flush()
        branch.base_snapshot_id = snapshot.id
        branch.head_snapshot_id = snapshot.id
        release = OntologyRelease(
            id="release-agent-scope",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="staging",
            status="released",
            created_by_user_id=self.owner.id,
        )
        self.db.add(release)
        self.db.commit()

        frozen_name = self.function_a.name
        self.function_a.name = "开发环境已改名"
        live_only = FunctionDefinition(
            id="function-agent-scope-live",
            scenario_id=self.scenario.id,
            name="仅开发环境函数",
            input_schema=_schema(),
            output_schema={"type": "object", "properties": {}},
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 4}},
        )
        agent = Agent(
            id="agent-scope-staging",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="冻结能力 Agent",
            capability_scope=_scope("functions", self.function_a.id).model_dump(),
        )
        self.db.add_all([live_only, agent])
        self.db.commit()

        settings = SimpleNamespace(runtime_environment="staging")
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=settings,
        ):
            context = self._context(agent)
            self.assertEqual([item.name for item in context.functions], [frozen_name])
            catalog = agents_router.get_agent_capability_catalog(self.scenario.id, self.db)
            catalog_ids = {item["id"] for item in catalog["categories"]["functions"]}
            self.assertIn(self.function_a.id, catalog_ids)
            self.assertNotIn(live_only.id, catalog_ids)
            with self.assertRaises(HTTPException) as live_only_error:
                agents_router.create_agent(
                    AgentIn(
                        name="不可越过发布边界",
                        scenario_id=self.scenario.id,
                        capability_scope=_scope("functions", live_only.id),
                    ),
                    self.db,
                )
            self.assertEqual(live_only_error.exception.status_code, 400)


class AgentCapabilityMigrationTests(unittest.TestCase):
    def test_migration_keeps_legacy_rows_null_and_is_idempotent(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE agents (id VARCHAR(32) PRIMARY KEY, name VARCHAR(200))"
                )
                connection.exec_driver_sql(
                    "INSERT INTO agents (id, name) VALUES ('legacy-agent', 'legacy')"
                )
            with patch.object(database_module, "engine", engine):
                database_module._migrate_agent_capability_scope()
                database_module._migrate_agent_capability_scope()
            self.assertIn(
                "capability_scope",
                {column["name"] for column in inspect(engine).get_columns("agents")},
            )
            with engine.connect() as connection:
                value = connection.exec_driver_sql(
                    "SELECT capability_scope FROM agents WHERE id = 'legacy-agent'"
                ).scalar_one()
            self.assertIsNone(value)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
