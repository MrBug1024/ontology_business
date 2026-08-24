"""End-to-end proof that configured scenario resources reach the Agent runtime."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.routers import agents as agents_router
from app.models import (
    ActionExecutionLog,
    Agent,
    BusinessScenario,
    DataMapping,
    DataSource,
    EventEnvelope,
    FunctionDefinition,
    FunctionRun,
    LLMConfig,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRelease,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    RelationDataMapping,
    RelationInstance,
    Tenant,
    User,
    WorkflowRun,
)
from app.services import (
    agent_engine,
    capability_readiness_service,
    datasource_service,
    mapped_query_service,
    permission_service,
    release_service,
    runtime_definition_service,
    workflow_service,
)
from app.services.policies import (
    PolicyViolation,
    validate_agent_sql_scope,
    validate_read_only_sql,
)


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


class AgentCapabilityClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()
        self.tenant = Tenant(id="tenant-agent-closure", name="Agent 闭环租户")
        self.user = User(
            id="user-agent-closure",
            tenant_id=self.tenant.id,
            email="agent.closure@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-agent-closure",
            tenant_id=self.tenant.id,
            name="施工风险管控",
            description="识别项目风险并形成受治理处置流程",
        )
        self.db.add_all([self.tenant, self.user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id
        self._seed_scenario_model()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _seed_scenario_model(self) -> None:
        project = OntologyEntity(
            id="entity-project",
            scenario_id=self.scenario.id,
            name="项目",
            description="建筑项目",
            state_property="状态",
        )
        contractor = OntologyEntity(
            id="entity-contractor",
            scenario_id=self.scenario.id,
            name="承包商",
            description="负责施工的组织",
        )
        properties = [
            OntologyProperty(
                id="property-project-id",
                entity_id=project.id,
                name="项目编号",
                data_type="string",
                is_key=True,
                is_required=True,
            ),
            OntologyProperty(
                id="property-risk-score",
                entity_id=project.id,
                name="风险分",
                data_type="float",
            ),
            OntologyProperty(
                id="property-project-name",
                entity_id=project.id,
                name="项目名称",
                data_type="string",
            ),
            OntologyProperty(
                id="property-state",
                entity_id=project.id,
                name="状态",
                data_type="string",
                is_enum=True,
                enum_values=["施工中", "停工"],
            ),
            OntologyProperty(
                id="property-contractor-id",
                entity_id=contractor.id,
                name="承包商编号",
                data_type="string",
                is_key=True,
            ),
        ]
        relation = OntologyRelation(
            id="relation-responsible",
            scenario_id=self.scenario.id,
            name="负责施工",
            source_entity_id=contractor.id,
            target_entity_id=project.id,
            relation_type="1:N",
            description="承包商负责项目施工",
        )
        project_object = OntologyInstance(
            id="object-project-1",
            scenario_id=self.scenario.id,
            entity_id=project.id,
            name="滨江中心",
            attributes={"项目编号": "P-001", "风险分": 82.0, "状态": "施工中"},
            state="施工中",
            source="imported",
            source_ref="projects:P-001",
            source_metadata={
                "mapping_id": "mapping-projects",
                "data_source_id": "source-projects",
                "table_name": "projects",
                "record_key": "P-001",
            },
        )
        contractor_object = OntologyInstance(
            id="object-contractor-1",
            scenario_id=self.scenario.id,
            entity_id=contractor.id,
            name="华建集团",
            attributes={"承包商编号": "C-001"},
        )
        relation_object = RelationInstance(
            id="relation-instance-1",
            scenario_id=self.scenario.id,
            relation_id=relation.id,
            source_instance_id=contractor_object.id,
            target_instance_id=project_object.id,
            attributes={"合同角色": "总包"},
        )
        source = DataSource(
            id="source-projects",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目主数据",
            type="sqlite",
            config={},
            status="ok",
        )
        mapping = DataMapping(
            id="mapping-projects",
            scenario_id=self.scenario.id,
            entity_id=project.id,
            data_source_id=source.id,
            table_name="projects",
            column_map={
                "项目编号": "project_code",
                "项目名称": "project_name",
                "风险分": "risk_score",
            },
            transform_rules={"项目编号": [{"op": "trim"}]},
            status="ok",
            last_imported_count=1,
        )
        contract = FunctionDefinition(
            id="function-contract",
            scenario_id=self.scenario.id,
            name="风险说明契约",
            description="描述风险评估输入输出，不直接运行",
            input_schema=_schema({"score": {"type": "number"}}, ["score"]),
            output_schema=_schema({"level": {"type": "string"}}, ["level"]),
            runtime_kind="contract",
        )
        function = FunctionDefinition(
            id="function-score",
            scenario_id=self.scenario.id,
            name="计算项目风险分",
            description="按权重计算项目风险",
            input_schema=_schema({"amount": {"type": "number"}}, ["amount"]),
            output_schema=_schema({"score": {"type": "number"}}, ["score"]),
            runtime_kind="weighted_score",
            runtime_config={"weights": {"amount": 0.5}, "bias": 2},
        )
        action = OntologyAction(
            id="action-mark-risk",
            scenario_id=self.scenario.id,
            entity_id=project.id,
            name="标记高风险",
            description="为项目生成高风险标记预演",
            precondition=json.dumps(
                {"field": "project_id", "op": "is_not_null"},
                ensure_ascii=False,
            ),
            postcondition=json.dumps(
                {"field": "updated", "op": "==", "value": True},
                ensure_ascii=False,
            ),
            input_schema=_schema({"project_id": {"type": "string"}}, ["project_id"]),
            executor_type="sql",
            executor_config={
                "data_source_id": source.id,
                "sql": "SELECT '{project_id}' AS project_id",
            },
            enabled=True,
            requires_confirmation=True,
            idempotency_required=True,
        )
        rule = OntologyRule(
            id="rule-high-risk",
            scenario_id=self.scenario.id,
            entity_id=project.id,
            name="高风险项目",
            description="风险分达到 80 分即命中",
            condition={"field": "风险分", "op": ">=", "value": 80},
            trigger_action_ids=[action.id],
            severity="critical",
            enabled=True,
        )
        event = OntologyEvent(
            id="event-risk-found",
            scenario_id=self.scenario.id,
            name="发现高风险项目",
            description="规则命中后可发布的业务事件",
            payload_schema=_schema({"project_id": {"type": "string"}}, ["project_id"]),
            trigger_source="高风险项目规则",
            enabled=True,
        )
        workflow = OntologyWorkflow(
            id="workflow-risk-response",
            scenario_id=self.scenario.id,
            name="高风险处置流程",
            description="形成处置任务并等待人工确认",
            trigger_type="manual",
            nodes=[
                {"id": "start", "type": "start", "data": {}},
                {"id": "end", "type": "end", "data": {}},
            ],
            edges=[{"id": "edge-1", "source": "start", "target": "end", "label": ""}],
            status="active",
            enabled=True,
        )
        self.agent = Agent(
            id="agent-risk",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目风险助手",
            description="使用本体、映射和规则回答施工风险问题",
            system_prompt="你负责施工风险分析。",
            data_source_ids=[source.id],
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": [contract.id, function.id]},
                "actions": {"mode": "explicit", "selected_ids": [action.id]},
                "rules": {"mode": "explicit", "selected_ids": [rule.id]},
                "events": {"mode": "explicit", "selected_ids": [event.id]},
                "workflows": {"mode": "explicit", "selected_ids": [workflow.id]},
            },
        )
        self.db.add_all(
            [
                project,
                contractor,
                *properties,
                relation,
                project_object,
                contractor_object,
                relation_object,
                source,
                mapping,
                contract,
                function,
                action,
                rule,
                event,
                workflow,
                self.agent,
            ]
        )
        self.db.commit()

    def _context(self) -> agent_engine.AgentContext:
        return agent_engine.AgentContext(self.db, self.agent, LLMConfig(name="工具模型"))

    def test_every_scenario_resource_changes_agent_tools_or_context(self) -> None:
        context = self._context()
        tools_by_name = {
            tool["function"]["name"]: tool["function"]
            for tool in context.build_tools()
        }
        tool_names = set(tools_by_name)
        self.assertTrue(
            {
                "list_ontology_model",
                "search_ontology",
                "get_ontology_object",
                "list_data_mappings",
                "query_mapped_objects",
                "list_functions",
                "run_function",
                "list_actions",
                "execute_action",
                "list_rules",
                "evaluate_rule",
                "list_events",
                "prepare_event_publish",
                "list_workflows",
                "execute_workflow",
            }.issubset(tool_names)
        )
        self.assertNotIn("run_sql", tool_names)
        prompt = agent_engine.build_system_prompt(
            context,
            self.scenario.name,
            "项目包含项目编号、项目名称和风险分。",
        )
        self.assertIn("query_mapped_objects", prompt)
        self.assertIn("query_business_data", prompt)
        self.assertNotIn("run_sql", prompt)

        business_schema = tools_by_name["query_business_data"]["parameters"]
        base_schema = business_schema["properties"]["base_entity"]
        self.assertEqual(
            {choice.get("type") for choice in base_schema["oneOf"]},
            {"object", "string"},
        )
        self.assertNotIn("base_properties", business_schema["required"])
        group_item = business_schema["properties"]["group_by"]["items"]
        aggregate_item = business_schema["properties"]["aggregations"]["items"]
        self.assertEqual(group_item["required"], ["property"])
        self.assertEqual(
            group_item["anyOf"],
            [{"required": ["entity_id"]}, {"required": ["entity_name"]}],
        )
        self.assertEqual(aggregate_item["required"], ["function", "alias"])
        self.assertIn("COUNT(*)", business_schema["properties"]["aggregations"]["description"])
        self.assertIn("list_actions", tools_by_name["execute_action"]["description"])
        self.assertIn("list_workflows", tools_by_name["execute_workflow"]["description"])

        ontology = json.loads(context.execute_tool("list_ontology_model", {}))
        self.assertEqual({item["name"] for item in ontology["entities"]}, {"项目", "承包商"})
        self.assertEqual(ontology["relations"][0]["name"], "负责施工")

        objects = json.loads(
            context.execute_tool("search_ontology", {"entity": "项目", "query": "P-001"})
        )
        self.assertEqual(objects[0]["name"], "滨江中心")
        detail = json.loads(
            context.execute_tool("get_ontology_object", {"object_id": objects[0]["id"]})
        )
        self.assertEqual(detail["attributes"]["风险分"], 82.0)
        self.assertEqual(detail["relations"][0]["relation"], "负责施工")
        self.assertEqual(detail["relations"][0]["attributes"], {"合同角色": "总包"})

        mappings = json.loads(context.execute_tool("list_data_mappings", {}))
        self.assertEqual(mappings[0]["table"], "projects")
        self.assertEqual(mappings[0]["column_map"]["风险分"], "risk_score")
        self.assertEqual(mappings[0]["transform_operations"], {"项目编号": ["trim"]})

        functions = json.loads(context.execute_tool("list_functions", {}))
        function_status = {item["name"]: item["executable"] for item in functions}
        self.assertEqual(
            function_status,
            {"风险说明契约": False, "计算项目风险分": True},
        )
        contract_status = next(item for item in functions if item["name"] == "风险说明契约")
        self.assertTrue(any("仅定义了契约" in reason for reason in contract_status["blocked_reasons"]))
        function_result = json.loads(
            context.execute_tool(
                "run_function",
                {"function_id": "function-score", "params": {"amount": 10}},
            )
        )
        self.assertEqual(function_result["score"], 7)
        self.assertEqual(self.db.query(FunctionRun).count(), 1)

        actions = json.loads(context.execute_tool("list_actions", {}))
        self.assertEqual(actions[0]["name"], "标记高风险")
        self.assertTrue(actions[0]["executable"])
        self.assertEqual(actions[0]["blocked_reasons"], [])
        self.assertEqual(actions[0]["required"], ["project_id"])
        self.assertIn('"field": "project_id"', actions[0]["precondition"])
        self.assertIn('"field": "updated"', actions[0]["postcondition"])
        preview = json.loads(
            context.execute_tool(
                "execute_action",
                {"action_id": "action-mark-risk", "params": {"project_id": "P-001"}},
            )
        )
        self.assertEqual(preview["status"], "dry_run")
        self.assertTrue(preview["result"]["plan"]["side_effects_skipped"])
        self.assertEqual(
            preview["result"]["plan"]["precondition_condition"],
            {"field": "project_id", "op": "is_not_null"},
        )
        self.assertEqual(
            preview["result"]["plan"]["postcondition_condition"],
            {"field": "updated", "op": "==", "value": True},
        )
        self.assertEqual(self.db.query(ActionExecutionLog).count(), 1)

        rules = json.loads(context.execute_tool("list_rules", {}))
        self.assertTrue(rules[0]["executable"])
        self.assertEqual(rules[0]["trigger_action_ids"], ["action-mark-risk"])
        evaluated = json.loads(
            context.execute_tool(
                "evaluate_rule", {"rule_id": "rule-high-risk", "record": {"风险分": 82}}
            )
        )
        self.assertTrue(evaluated["matched"])
        self.assertFalse(evaluated["side_effects_executed"])
        self.assertEqual(evaluated["trigger_actions"][0]["status"], "preview_required")

        events = json.loads(context.execute_tool("list_events", {}))
        self.assertTrue(events[0]["executable"])
        self.assertEqual(events[0]["payload_schema"]["required"], ["project_id"])
        event_plan = json.loads(
            context.execute_tool(
                "prepare_event_publish",
                {"event_id": "event-risk-found", "payload": {"project_id": "P-001"}},
            )
        )
        self.assertEqual(event_plan["status"], "confirmation_required")
        self.assertEqual(self.db.query(EventEnvelope).count(), 0)

        workflows = json.loads(context.execute_tool("list_workflows", {}))
        self.assertTrue(workflows[0]["executable"])
        self.assertEqual(workflows[0]["nodes_count"], 2)
        self.assertEqual(workflows[0]["params_schema"]["type"], "object")
        self.assertEqual(workflows[0]["required"], [])
        workflow_plan = json.loads(
            context.execute_tool(
                "execute_workflow",
                {"workflow_id": "workflow-risk-response", "params": {"project_id": "P-001"}},
            )
        )
        self.assertEqual(workflow_plan["status"], "confirmation_required")
        self.assertEqual(self.db.query(WorkflowRun).count(), 0)

        prompt = agent_engine.build_system_prompt(
            context,
            self.scenario.name,
            agent_engine.ontology_summary_for(self.scenario, db=self.db),
        )
        self.assertIn("前置条件：{", prompt)
        self.assertIn('"field": "project_id"', prompt)
        self.assertIn("完成后：{", prompt)
        for expected in (
            "使用本体、映射和规则回答施工风险问题",
            "项目主数据",
            "projects",
            "风险说明契约",
            "标记高风险",
            "高风险项目",
            "发现高风险项目",
            "高风险处置流程",
        ):
            self.assertIn(expected, prompt)
        for expected in (
            "就是本次审计的有效规则",
            "在该范围内未发现违规",
            "不得向用户索要内部 ID",
            "最多重试一次",
            "必须先调用 list_actions",
            "实际调用 execute_action",
        ):
            self.assertIn(expected, prompt)

    def test_resource_tools_resolve_id_api_name_and_display_name(self) -> None:
        context = self._context()
        aliases = {
            "function-score": "calculate_project_risk",
            "action-mark-risk": "mark_high_risk",
            "rule-high-risk": "high_risk_rule",
            "event-risk-found": "risk_found_event",
            "workflow-risk-response": "risk_response_workflow",
        }
        for resources in (
            context.functions,
            context.actions,
            context.rules,
            context.events,
            context.workflows,
        ):
            for resource in resources:
                if resource.id in aliases:
                    resource.api_name = aliases[resource.id]

        self.assertEqual(
            json.loads(
                context.execute_tool(
                    "run_function",
                    {"function_id": "calculate_project_risk", "params": {"amount": 10}},
                )
            )["score"],
            7,
        )
        self.assertEqual(
            json.loads(
                context.execute_tool(
                    "execute_action",
                    {"action_id": "mark_high_risk", "params": {"project_id": "P-001"}},
                )
            )["status"],
            "dry_run",
        )
        self.assertTrue(
            json.loads(
                context.execute_tool(
                    "evaluate_rule",
                    {"rule_id": "high_risk_rule", "record": {"风险分": 82}},
                )
            )["matched"]
        )
        self.assertEqual(
            json.loads(
                context.execute_tool(
                    "prepare_event_publish",
                    {"event_id": "risk_found_event", "payload": {"project_id": "P-001"}},
                )
            )["status"],
            "confirmation_required",
        )
        self.assertEqual(
            json.loads(
                context.execute_tool(
                    "execute_workflow",
                    {"workflow_id": "risk_response_workflow", "params": {}},
                )
            )["status"],
            "confirmation_required",
        )

        catalogs = {
            name: json.loads(context.execute_tool(name, {}))
            for name in (
                "list_functions",
                "list_actions",
                "list_rules",
                "list_events",
                "list_workflows",
            )
        }
        self.assertTrue(
            any(item["api_name"] == "calculate_project_risk" for item in catalogs["list_functions"])
        )
        self.assertEqual(catalogs["list_actions"][0]["required"], ["project_id"])
        self.assertEqual(catalogs["list_events"][0]["required"], ["project_id"])

    def test_workflow_discovery_infers_required_params_before_preview(self) -> None:
        context = self._context()
        # Released scenarios can still contain the pre-JSON-Schema flat field map.
        # Discovery must normalize it before copying the downstream Action contract.
        normalized = agent_engine._normalized_object_schema(
            {"project_id": {"type": "string", "required": True}}
        )
        self.assertEqual(normalized["required"], ["project_id"])
        self.assertEqual(normalized["properties"]["project_id"]["type"], "string")
        workflow = context.workflows[0]
        workflow.nodes = [
            {"id": "start", "type": "start", "data": {}},
            {
                "id": "mark",
                "type": "action",
                "data": {
                    "action_id": "action-mark-risk",
                    "params": {"project_id": "{{params.project_id}}"},
                },
            },
            {"id": "end", "type": "end", "data": {}},
        ]
        workflow.edges = [
            {"id": "start-mark", "source": "start", "target": "mark", "label": ""},
            {"id": "mark-end", "source": "mark", "target": "end", "label": ""},
        ]

        listed = json.loads(context.execute_tool("list_workflows", {}))[0]
        self.assertEqual(listed["required"], ["project_id"])
        self.assertEqual(
            listed["params_schema"]["properties"]["project_id"]["type"],
            "string",
        )
        missing = json.loads(
            context.execute_tool(
                "execute_workflow",
                {"workflow_id": workflow.name, "params": {}},
            )
        )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "INVALID_TOOL_ARGUMENTS")
        self.assertTrue(missing["error"]["retryable"])
        self.assertIn("params.project_id", missing["error"]["message"])
        preview = json.loads(
            context.execute_tool(
                "execute_workflow",
                {"workflow_id": workflow.name, "params": {"project_id": "P-001"}},
            )
        )
        self.assertEqual(preview["status"], "confirmation_required")

    def test_sql_action_uses_bound_values_and_rejects_ambiguous_placeholders(self) -> None:
        source = DataSource(
            id="source-sql-action-binding",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="SQL Action 参数绑定",
            type="sqlite",
            config={"path": ":memory:"},
            status="ok",
        )
        engine = datasource_service.get_engine(source)
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE allowed_rows (code TEXT, visible TEXT)"))
                connection.execute(text("CREATE TABLE hidden_rows (secret TEXT)"))
                connection.execute(
                    text("INSERT INTO allowed_rows VALUES ('SAFE', '可见记录')")
                )
                connection.execute(
                    text("INSERT INTO hidden_rows VALUES ('不可越权读取')")
                )

            injected = workflow_service._exec_sql(
                self.db,
                {
                    "data_source_id": source.id,
                    "sql": "SELECT visible FROM allowed_rows WHERE code = '{code}'",
                },
                {"code": "X' UNION SELECT secret FROM hidden_rows --"},
                data_source=source,
            )
            self.assertEqual(injected["rows"], [])
            legitimate = workflow_service._exec_sql(
                self.db,
                {
                    "data_source_id": source.id,
                    "sql": "SELECT visible FROM allowed_rows WHERE code = {code} OR code = {code}",
                },
                {"code": "SAFE"},
                data_source=source,
            )
            self.assertEqual(legitimate["rows"], [["可见记录"]])

            for template, params in (
                ("SELECT {missing}", {}),
                ("SELECT 1", {"extra": "value"}),
                ("SELECT 'prefix-{code}'", {"code": "SAFE"}),
                ("SELECT 1 -- {code}", {"code": "SAFE"}),
            ):
                with self.assertRaises(ValueError, msg=template):
                    workflow_service._compile_sql_action(template, params)
        finally:
            datasource_service.invalidate_engine(source)

    def test_disabled_definitions_are_discoverable_but_not_invocable(self) -> None:
        self.db.query(FunctionDefinition).filter(
            FunctionDefinition.id == "function-score"
        ).update({FunctionDefinition.runtime_kind: "contract"})
        self.db.query(OntologyAction).update({OntologyAction.enabled: False})
        self.db.query(OntologyRule).update({OntologyRule.enabled: False})
        self.db.query(OntologyEvent).update({OntologyEvent.enabled: False})
        self.db.query(OntologyWorkflow).update(
            {OntologyWorkflow.enabled: False, OntologyWorkflow.status: "draft"}
        )
        self.db.commit()

        context = self._context()
        tool_names = {tool["function"]["name"] for tool in context.build_tools()}
        self.assertTrue(
            {"list_functions", "list_actions", "list_rules", "list_events", "list_workflows"}
            .issubset(tool_names)
        )
        self.assertFalse(
            {
                "run_function",
                "execute_action",
                "evaluate_rule",
                "prepare_event_publish",
                "execute_workflow",
            }
            & tool_names
        )
        self.assertFalse(json.loads(context.execute_tool("list_actions", {}))[0]["executable"])
        self.assertTrue(
            json.loads(context.execute_tool("list_workflows", {}))[0]["blocked_reasons"]
        )
        prompt = agent_engine.build_system_prompt(context, self.scenario.name, "")
        self.assertIn("不可执行；阻塞原因", prompt)
        self.assertIn("已停用", context.execute_tool("execute_action", {"action_id": "action-mark-risk"}))
        self.assertIn("已停用", context.execute_tool("evaluate_rule", {"rule_id": "rule-high-risk"}))

    def test_natural_language_action_condition_is_a_visible_runtime_blocker(self) -> None:
        action = self.db.get(OntologyAction, "action-mark-risk")
        assert action is not None
        action.precondition = "项目风险分已经计算"
        self.db.commit()
        definition = runtime_definition_service.resolve_active(
            self.db, self.scenario, environment="dev"
        )

        readiness = capability_readiness_service.capability_readiness(
            "action", action, definition=definition, db=self.db
        )

        self.assertFalse(readiness.executable)
        self.assertTrue(
            any("自然语言" in reason for reason in readiness.blocked_reasons)
        )
        with self.assertRaises(capability_readiness_service.CapabilityNotReady):
            workflow_service.preview_action(
                self.db,
                action,
                {"project_id": "P-001"},
                runtime_environment=definition.environment,
                runtime_definition=definition,
            )
        self.assertEqual(self.db.query(ActionExecutionLog).count(), 0)

    def test_structured_action_conditions_gate_before_and_after_dispatch(self) -> None:
        action = self.db.get(OntologyAction, "action-mark-risk")
        assert action is not None
        action.input_schema = _schema(
            {
                "project_id": {"type": "string"},
                "allowed": {"type": "boolean"},
            },
            ["project_id", "allowed"],
        )
        action.precondition = json.dumps(
            {"field": "allowed", "op": "==", "value": True}
        )
        action.postcondition = json.dumps(
            {"field": "updated", "op": "==", "value": True}
        )
        self.db.commit()
        definition = runtime_definition_service.resolve_active(
            self.db, self.scenario, environment="dev"
        )

        with self.assertRaises(PolicyViolation):
            workflow_service.preview_action(
                self.db,
                action,
                {"project_id": "P-001", "allowed": False},
                runtime_environment=definition.environment,
                runtime_definition=definition,
            )
        self.assertEqual(self.db.query(ActionExecutionLog).count(), 0)

        with patch.object(
            workflow_service,
            "_dispatch_executor",
            side_effect=[({"updated": False}, []), ({"updated": True}, [])],
        ):
            failed = workflow_service.execute_action(
                self.db,
                action,
                {"project_id": "P-001", "allowed": True},
                confirm=True,
                idempotency_key="postcondition-fails",
                runtime_environment=definition.environment,
                runtime_definition=definition,
            )
            succeeded = workflow_service.execute_action(
                self.db,
                action,
                {"project_id": "P-001", "allowed": True},
                confirm=True,
                idempotency_key="postcondition-passes",
                runtime_environment=definition.environment,
                runtime_definition=definition,
            )

        self.assertEqual(failed["status"], "failed")
        self.assertIn("后置条件校验失败", failed["error"])
        self.assertEqual(succeeded["status"], "success")

    def test_agent_data_bindings_filter_mappings_and_imported_objects(self) -> None:
        hidden_source = DataSource(
            id="source-hidden",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="未绑定隐私数据",
            type="sqlite",
            config={},
        )
        hidden_mapping = DataMapping(
            id="mapping-hidden",
            scenario_id=self.scenario.id,
            entity_id="entity-project",
            data_source_id=hidden_source.id,
            table_name="private_projects",
            column_map={"项目编号": "project_code"},
        )
        hidden_object = OntologyInstance(
            id="object-hidden",
            scenario_id=self.scenario.id,
            entity_id="entity-project",
            name="秘密项目",
            attributes={"项目编号": "SECRET-1"},
            source="imported",
            source_ref="private_projects:SECRET-1",
            source_metadata={
                "mapping_id": hidden_mapping.id,
                "data_source_id": hidden_source.id,
                "table_name": hidden_mapping.table_name,
                "record_key": "SECRET-1",
            },
        )
        self.db.add_all([hidden_source, hidden_mapping, hidden_object])
        self.db.commit()

        context = self._context()
        mappings = json.loads(context.execute_tool("list_data_mappings", {}))
        self.assertEqual([item["id"] for item in mappings], ["mapping-projects"])
        searched = json.loads(
            context.execute_tool(
                "search_ontology", {"entity": "项目", "query": "SECRET-1"}
            )
        )
        self.assertEqual(searched, [])
        detail = json.loads(
            context.execute_tool("get_ontology_object", {"object_id": hidden_object.id})
        )
        self.assertIn("不在当前 Agent 绑定的数据范围", detail["error"])

    def test_semantic_mapping_query_is_parameterized_transformed_and_lineaged(self) -> None:
        context = self._context()
        query_result = {
            "columns": ["__ontology_0", "__ontology_1"],
            "rows": [["  P-001  ", 82.0]],
            "row_count": 1,
            "truncated": False,
        }
        with patch.object(
            mapped_query_service.datasource_service,
            "run_parameterized_query",
            return_value=query_result,
        ) as run_query:
            args = {
                "entity_name": "项目",
                "properties": ["项目编号", "风险分"],
                "filters": [
                    {
                        "property": "项目名称",
                        "op": "eq",
                        "value": "P-001' OR 1=1 --",
                    },
                    {"property": "风险分", "op": "gte", "value": 80},
                ],
                "sort": [{"property": "风险分", "direction": "desc"}],
                "limit": 10,
            }
            result = json.loads(
                context.execute_tool("query_mapped_objects", args)
            )
            self.assertEqual(
                result["objects"],
                [{"项目编号": "P-001", "风险分": 82.0}],
            )
            self.assertEqual(result["lineage"]["mapping_id"], "mapping-projects")
            self.assertEqual(result["lineage"]["data_source_id"], "source-projects")
            self.assertEqual(
                result["lineage"]["data_source_connector_revision"],
                self.db.get(DataSource, "source-projects").connector_revision,
            )
            self.assertEqual(result["lineage"]["table"], "projects")
            self.assertEqual(
                result["lineage"]["definition"]["definition_hash"],
                context.runtime_definition.definition_hash,
            )
            run_query.assert_called_once()
            _source, sql, parameters = run_query.call_args.args
            self.assertIn('FROM "projects"', sql)
            self.assertIn('"project_name" = :mq_0', sql)
            self.assertIn('"risk_score" >= :mq_1', sql)
            self.assertNotIn("OR 1=1", sql)
            self.assertEqual(parameters["mq_0"], "P-001' OR 1=1 --")
            self.assertEqual(parameters["mq_1"], 80.0)
            self.assertEqual(parameters["mq_limit"], 11)

            self.assertTrue(
                context.authorize_historic_tool_result(
                    "query_mapped_objects",
                    args,
                    json.dumps(result, ensure_ascii=False),
                )
            )

            legacy_without_connector_lineage = json.loads(json.dumps(result))
            legacy_without_connector_lineage["lineage"].pop(
                "data_source_connector_revision"
            )
            self.assertFalse(
                context.authorize_historic_tool_result(
                    "query_mapped_objects",
                    args,
                    json.dumps(legacy_without_connector_lineage, ensure_ascii=False),
                )
            )

            source = self.db.get(DataSource, "source-projects")
            source.config = {"database": "same-schema-rebound.sqlite"}
            self.db.commit()
            rebound_context = self._context()
            self.assertFalse(
                rebound_context.authorize_historic_tool_result(
                    "query_mapped_objects",
                    args,
                    json.dumps(result, ensure_ascii=False),
                )
            )

            bypass = context.execute_tool(
                "query_mapped_objects",
                {**args, "sql": "SELECT * FROM users"},
            )
            self.assertIn("不接受 SQL", bypass)
            self.assertEqual(run_query.call_count, 1)

        denied = context.execute_tool(
            "run_sql",
            {"data_source_id": "source-projects", "sql": "SELECT * FROM projects"},
        )
        self.assertIn("不直接接收 SQL", denied)

    def test_business_query_joins_groups_and_aggregates_using_semantic_fields(self) -> None:
        contractor = self.db.get(OntologyEntity, "entity-contractor")
        self.db.add(
            DataMapping(
                id="mapping-contractors",
                scenario_id=self.scenario.id,
                entity_id=contractor.id,
                data_source_id="source-projects",
                table_name="contractors",
                column_map={"承包商编号": "contractor_code"},
                status="ok",
            )
        )
        # This test exercises relational planning; a separate test covers
        # transformation-aware single-object projection.
        self.db.get(DataMapping, "mapping-projects").transform_rules = {}
        self.db.commit()
        context = self._context()
        raw = {
            "columns": ["q_col_0", "q_agg_0"],
            "rows": [["P-001", 1]],
            "row_count": 1,
            "truncated": False,
        }
        args = {
            "base_entity": {"entity_name": "项目"},
            "base_properties": ["项目编号"],
            "related_entities": [{
                "entity_name": "承包商",
                "properties": [],
                "join": {"base_property": "项目编号", "related_property": "承包商编号"},
            }],
            "group_by": [{"entity_name": "项目", "property": "项目编号"}],
            "aggregations": [{
                "function": "count",
                "entity_name": "承包商",
                "property": "承包商编号",
                "alias": "承包商数量",
            }],
            "having": [{"alias": "承包商数量", "op": "gt", "value": 0}],
            "sort": [{"alias": "承包商数量", "direction": "desc"}],
            "limit": 10,
        }
        with patch.object(
            agent_engine.business_query_service.datasource_service,
            "run_parameterized_query",
            return_value=raw,
        ) as run_query:
            result = json.loads(context.execute_tool("query_business_data", args))
        self.assertEqual(result["records"], [{"项目编号": "P-001", "承包商数量": 1}])
        source, sql, parameters = run_query.call_args.args
        self.assertEqual(source.id, "source-projects")
        self.assertIn('JOIN "contractors" "r0"', sql)
        self.assertIn('GROUP BY "b"."project_code"', sql)
        self.assertIn('COUNT("r0"."contractor_code")', sql)
        self.assertIn('HAVING "q_agg_0" > :having_0', sql)
        self.assertEqual(parameters["having_0"], 0)
        self.assertEqual(parameters["bq_limit"], 11)

    def test_business_query_accepts_string_entity_and_count_star_without_key_transform(self) -> None:
        context = self._context()
        args = {
            "base_entity": "项目",
            "aggregations": [
                {
                    "function": "count",
                    "entity_name": "项目",
                    "alias": "记录数",
                }
            ],
        }
        with patch.object(
            agent_engine.business_query_service.datasource_service,
            "run_parameterized_query",
            return_value={
                "columns": ["q_agg_0"],
                "rows": [[2]],
                "row_count": 1,
                "truncated": False,
            },
        ) as run_query:
            raw_result = context.execute_tool("query_business_data", args)
            result = json.loads(raw_result)

        self.assertEqual(result["records"], [{"记录数": 2}])
        _source, sql, parameters = run_query.call_args.args
        self.assertIn("COUNT(*)", sql)
        self.assertNotIn("project_code", sql)
        self.assertNotIn("TRIM(", sql.upper())
        self.assertEqual(parameters["bq_limit"], 101)
        self.assertTrue(
            context.authorize_historic_tool_result(
                "query_business_data",
                args,
                raw_result,
            )
        )

        legacy_without_lineage = json.loads(raw_result)
        legacy_without_lineage["scope"].pop("data_source_connector_revision")
        self.assertFalse(
            context.authorize_historic_tool_result(
                "query_business_data",
                args,
                json.dumps(legacy_without_lineage, ensure_ascii=False),
            )
        )

        with patch.object(
            agent_engine.business_query_service.datasource_service,
            "run_parameterized_query",
            return_value={
                "columns": ["q_agg_0"],
                "rows": [],
                "row_count": 0,
                "truncated": False,
            },
        ):
            empty_result = context.execute_tool("query_business_data", args)
        self.assertEqual(json.loads(empty_result)["row_count"], 0)
        self.assertTrue(
            context.authorize_historic_tool_result(
                "query_business_data",
                args,
                empty_result,
            )
        )

        invalid = json.loads(
            context.execute_tool(
                "query_business_data",
                {
                    "base_entity": "项目",
                    "aggregations": [
                        {
                            "function": "sum",
                            "entity_name": "项目",
                            "alias": "合计",
                        }
                    ],
                },
            )
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "INVALID_QUERY")
        self.assertIn("必须提供 property", invalid["error"]["message"])

    def test_resource_history_requires_the_exact_runtime_definition_fingerprint(self) -> None:
        context = self._context()
        definition_hash = context.runtime_definition.definition_hash
        result = json.dumps({
            "status": "success",
            "definition_hash": definition_hash,
        })
        self.assertTrue(
            context.authorize_historic_tool_result(
                "execute_action",
                {"action_id": "action-mark-risk"},
                result,
            )
        )
        self.assertFalse(
            context.authorize_historic_tool_result(
                "execute_action",
                {"action_id": "action-mark-risk"},
                json.dumps({"status": "success"}),
            )
        )

        action = self.db.get(OntologyAction, "action-mark-risk")
        action.description = "已改绑后的 Action 定义"
        self.db.commit()
        rebound_context = self._context()
        self.assertNotEqual(
            rebound_context.runtime_definition.definition_hash,
            definition_hash,
        )
        self.assertFalse(
            rebound_context.authorize_historic_tool_result(
                "execute_action",
                {"action_id": "action-mark-risk"},
                result,
            )
        )

    def test_tool_errors_are_safe_structured_and_historically_authorizable(self) -> None:
        context = self._context()
        invalid_args = {"base_entity": 123, "aggregations": []}
        raw_error = context.execute_tool("query_business_data", invalid_args)
        error = json.loads(raw_error)
        self.assertEqual(
            set(error),
            {"ok", "error"},
        )
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "INVALID_QUERY")
        self.assertTrue(error["error"]["retryable"])
        self.assertTrue(
            context.authorize_historic_tool_result(
                "query_business_data",
                invalid_args,
                raw_error,
            )
        )

        forged = {
            "ok": False,
            "error": {
                "code": "INVALID_QUERY",
                "message": "safe-looking",
                "retryable": True,
                "raw_exception": "password=secret",
            },
        }
        self.assertFalse(
            context.authorize_historic_tool_result(
                "query_business_data",
                invalid_args,
                forged,
            )
        )

        with patch.object(
            agent_engine.business_query_service,
            "query_business_data",
            side_effect=RuntimeError("connector password=should-never-appear"),
        ):
            raw_runtime_error = context.execute_tool(
                "query_business_data",
                {"base_entity": "项目", "base_properties": ["项目编号"]},
            )
        runtime_error = json.loads(raw_runtime_error)
        self.assertEqual(runtime_error["error"]["code"], "TOOL_EXECUTION_FAILED")
        self.assertFalse(runtime_error["error"]["retryable"])
        self.assertNotIn("password", raw_runtime_error)
        self.assertNotIn("should-never-appear", raw_runtime_error)

    def test_business_query_prefers_configured_relation_mapping_over_column_guess(self) -> None:
        contractor = self.db.get(OntologyEntity, "entity-contractor")
        contractor_mapping = DataMapping(
            id="mapping-contractors-relation",
            scenario_id=self.scenario.id,
            entity_id=contractor.id,
            data_source_id="source-projects",
            table_name="contractors",
            column_map={"承包商编号": "contractor_code"},
            status="ok",
        )
        relation_mapping = RelationDataMapping(
            id="relation-mapping-responsible",
            scenario_id=self.scenario.id,
            relation_id="relation-responsible",
            source_mapping_id=contractor_mapping.id,
            target_mapping_id="mapping-projects",
            mode="source_fk",
            data_source_id="source-projects",
            foreign_key_column="project_code",
            status="ready",
        )
        self.db.add_all([contractor_mapping, relation_mapping])
        self.db.get(DataMapping, "mapping-projects").transform_rules = {}
        self.db.commit()
        context = self._context()
        raw = {
            "columns": ["q_col_0", "q_col_1"],
            "rows": [["P-001", "C-001"]],
            "row_count": 1,
            "truncated": False,
        }
        args = {
            "base_entity": {"entity_name": "项目"},
            "base_properties": ["项目编号"],
            "related_entities": [{
                "entity_name": "承包商",
                "properties": ["承包商编号"],
            }],
            "limit": 10,
        }
        with patch.object(
            agent_engine.business_query_service.datasource_service,
            "run_parameterized_query",
            return_value=raw,
        ) as run_query:
            result = json.loads(context.execute_tool("query_business_data", args))
        self.assertEqual(
            result["records"],
            [{"项目编号": "P-001", "承包商.承包商编号": "C-001"}],
        )
        _source, sql, _parameters = run_query.call_args.args
        self.assertIn('"r0"."project_code" = "b"."project_code"', sql)
        self.assertNotIn('"r0"."contractor_code" = "b"."project_code"', sql)

    def test_business_query_uses_configured_join_table_in_reverse_relation_direction(self) -> None:
        contractor = self.db.get(OntologyEntity, "entity-contractor")
        contractor_mapping = DataMapping(
            id="mapping-contractors-join",
            scenario_id=self.scenario.id,
            entity_id=contractor.id,
            data_source_id="source-projects",
            table_name="contractors",
            column_map={"承包商编号": "contractor_code"},
            status="ok",
        )
        relation_mapping = RelationDataMapping(
            id="relation-mapping-join",
            scenario_id=self.scenario.id,
            relation_id="relation-responsible",
            source_mapping_id=contractor_mapping.id,
            target_mapping_id="mapping-projects",
            mode="join_table",
            data_source_id="source-projects",
            table_name="contractor_projects",
            source_key_column="contractor_code",
            target_key_column="project_code",
            status="ready",
        )
        self.db.add_all([contractor_mapping, relation_mapping])
        self.db.get(DataMapping, "mapping-projects").transform_rules = {}
        self.db.commit()
        context = self._context()
        raw = {
            "columns": ["q_col_0", "q_col_1"],
            "rows": [["P-001", "C-001"]],
            "row_count": 1,
            "truncated": False,
        }
        args = {
            "base_entity": {"entity_name": "项目"},
            "base_properties": ["项目编号"],
            "related_entities": [{
                "entity_name": "承包商",
                "properties": ["承包商编号"],
            }],
            "limit": 10,
        }
        with patch.object(
            agent_engine.business_query_service.datasource_service,
            "run_parameterized_query",
            return_value=raw,
        ) as run_query:
            result = json.loads(context.execute_tool("query_business_data", args))
        self.assertEqual(
            result["records"],
            [{"项目编号": "P-001", "承包商.承包商编号": "C-001"}],
        )
        _source, sql, _parameters = run_query.call_args.args
        self.assertIn('JOIN "contractor_projects" "j0" ON "b"."project_code" = "j0"."project_code"', sql)
        self.assertIn('JOIN "contractors" "r0" ON "j0"."contractor_code" = "r0"."contractor_code"', sql)

    def test_semantic_mapping_query_executes_bound_sqlite_values_without_injection(self) -> None:
        context = self._context()
        source_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        injected_name = "P-001' OR 1=1 --"
        try:
            with source_engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE projects ("
                        "project_code TEXT, project_name TEXT, risk_score REAL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO projects(project_code, project_name, risk_score) "
                        "VALUES (:code, :name, :score)"
                    ),
                    [
                        {"code": "  P-001  ", "name": injected_name, "score": 82.0},
                        {"code": "P-002", "name": "普通项目", "score": 99.0},
                    ],
                )
            with patch.object(
                mapped_query_service.datasource_service,
                "get_engine",
                return_value=source_engine,
            ):
                result = json.loads(
                    context.execute_tool(
                        "query_mapped_objects",
                        {
                            "entity_name": "项目",
                            "properties": ["项目编号", "风险分"],
                            "filters": [
                                {"property": "项目名称", "op": "eq", "value": injected_name}
                            ],
                            "limit": 10,
                        },
                    )
                )
            self.assertEqual(
                result["objects"],
                [{"项目编号": "P-001", "风险分": 82.0}],
            )
            self.assertFalse(result["truncated"])
        finally:
            source_engine.dispose()

    def test_semantic_mapping_query_fails_closed_on_ambiguity_and_invalid_fields(self) -> None:
        context = self._context()
        with patch.object(
            mapped_query_service.datasource_service,
            "run_parameterized_query",
        ) as run_query:
            for args in (
                {"entity_name": "项目", "properties": ["状态"]},
                {
                    "entity_name": "项目",
                    "properties": ["项目编号"],
                    "filters": [{"property": "风险分", "op": "gte", "value": "80"}],
                },
                {
                    "entity_name": "项目",
                    "properties": ["项目编号"],
                    "filters": [{"property": "项目编号", "op": "raw", "value": "x"}],
                },
                {
                    "entity_name": "项目",
                    "properties": ["项目编号"],
                    "filters": [{"property": "项目编号", "op": "eq", "value": "P-001"}],
                },
                {
                    "entity_name": "项目",
                    "properties": ["项目编号"],
                    "sort": [{"property": "项目编号", "direction": "asc"}],
                },
                {
                    "entity_name": "项目",
                    "properties": ["project_code; DROP TABLE users"],
                },
                {
                    "entity_name": "项目",
                    "properties": ["项目编号"],
                    "table": "users",
                },
            ):
                error = json.loads(context.execute_tool("query_mapped_objects", args))
                self.assertFalse(error["ok"])
                self.assertEqual(error["error"]["code"], "INVALID_QUERY")
                self.assertTrue(error["error"]["retryable"])
            self.assertIn(
                "不能保证源端过滤与本体语义等价",
                context.execute_tool(
                    "query_mapped_objects",
                    {
                        "entity_name": "项目",
                        "properties": ["项目编号"],
                        "filters": [
                            {"property": "项目编号", "op": "eq", "value": "P-001"}
                        ],
                    },
                ),
            )
            self.assertIn(
                "不能保证源端排序与本体语义等价",
                context.execute_tool(
                    "query_mapped_objects",
                    {
                        "entity_name": "项目",
                        "properties": ["项目编号"],
                        "sort": [{"property": "项目编号", "direction": "asc"}],
                    },
                ),
            )
            run_query.assert_not_called()

        duplicate = DataMapping(
            id="mapping-projects-duplicate",
            scenario_id=self.scenario.id,
            entity_id="entity-project",
            data_source_id="source-projects",
            table_name="projects_archive",
            column_map={"项目编号": "project_code"},
        )
        self.db.add(duplicate)
        self.db.commit()
        ambiguous = self._context()
        self.assertIn(
            "多个已绑定数据映射",
            ambiguous.execute_tool(
                "query_mapped_objects",
                {"entity_name": "项目", "properties": ["项目编号"]},
            ),
        )

    def test_semantic_mapping_query_quotes_all_supported_dialects(self) -> None:
        self.assertEqual(
            mapped_query_service.quote_identifier("sqlite", 'odd"column'),
            '"odd""column"',
        )
        self.assertEqual(
            mapped_query_service.quote_identifier("postgres", 'odd"column'),
            '"odd""column"',
        )
        self.assertEqual(
            mapped_query_service.quote_identifier("mysql", "odd`column"),
            "`odd``column`",
        )
        self.assertEqual(
            mapped_query_service.quote_table("postgres", "report.projects"),
            '"report"."projects"',
        )
        context = self._context()
        args = {
            "entity_id": "entity-project",
            "properties": ["项目编号"],
            "filters": [{"property": "项目名称", "op": "contains", "value": "A%_!"}],
        }
        source = context.data_sources[0]
        expected = {
            "sqlite": ('"project_code"', 'FROM "projects"'),
            "postgres": ('"project_code"', 'FROM "projects"'),
            "mysql": ("`project_code`", "FROM `projects`"),
        }
        for dialect, fragments in expected.items():
            source.type = dialect
            plan = mapped_query_service.prepare_query(
                self.db,
                definition=context.runtime_definition,
                mappings=context.mappings,
                data_sources=context.data_sources,
                args=args,
            )
            self.assertIn(fragments[0], plan.sql)
            self.assertIn(fragments[1], plan.sql)
            self.assertNotIn("A%_!", plan.sql)
            self.assertEqual(plan.parameters["mq_0"], "%A!%!_!!%")

    def test_semantic_mapping_query_rechecks_property_acl_and_historic_results(self) -> None:
        owner_context = self._context()
        args = {
            "entity_id": "entity-project",
            "properties": ["项目编号", "风险分"],
            "filters": [{"property": "风险分", "op": "gte", "value": 80}],
        }
        raw_query_result = {
            "columns": ["__ontology_0", "__ontology_1"],
            "rows": [["P-001", 82.0]],
            "row_count": 1,
            "truncated": False,
        }
        with patch.object(
            mapped_query_service.datasource_service,
            "run_parameterized_query",
            return_value=raw_query_result,
        ):
            owner_result_text = owner_context.execute_tool("query_mapped_objects", args)
        owner_result = json.loads(owner_result_text)
        self.assertEqual(owner_result["objects"][0]["风险分"], 82.0)

        viewer = User(
            id="user-agent-viewer",
            tenant_id=self.tenant.id,
            email="agent.viewer@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(viewer)
        self.db.flush()
        organization = permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=viewer.id,
            role_key="viewer",
        )
        self.db.get(OntologyProperty, "property-risk-score").is_sensitive = True
        self.db.commit()
        self.db.info["user_id"] = viewer.id
        viewer_context = self._context()

        with patch.object(
            mapped_query_service.datasource_service,
            "run_parameterized_query",
        ) as run_query:
            denied = viewer_context.execute_tool("query_mapped_objects", args)
            self.assertIn("不存在、未映射或无读取权限", denied)
            run_query.assert_not_called()
        self.assertFalse(
            viewer_context.authorize_historic_tool_result(
                "query_mapped_objects",
                args,
                owner_result_text,
            )
        )
        self.assertFalse(
            viewer_context.authorize_historic_tool_result(
                "run_sql",
                {"data_source_id": "source-projects", "sql": "SELECT risk_score FROM projects"},
                json.dumps({"columns": ["risk_score"], "rows": [[82.0]]}),
            )
        )

    def test_semantic_mapping_query_uses_frozen_mapping_and_runtime_connector(self) -> None:
        snapshot, release = self._staging_release()
        staging_source = DataSource(
            id="source-projects-query-staging",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目查询库（预发布）",
            type="postgres",
            status="connected",
        )
        self.db.add(staging_source)
        self.agent.data_source_ids = [staging_source.id]
        live_mapping = self.db.get(DataMapping, "mapping-projects")
        live_mapping.table_name = "draft_projects"
        live_mapping.column_map = {"项目编号": "draft_project_code"}
        live_mapping.transform_rules = {}
        self.db.commit()

        with (
            patch.object(
                agent_engine.runtime_connector_service,
                "runtime_environment",
                return_value="staging",
            ),
            patch.object(
                agent_engine.runtime_connector_service,
                "resolve_connector",
                return_value=(staging_source, {"managed": True}),
            ),
        ):
            context = self._context()

        raw_query_result = {
            "columns": ["__ontology_0"],
            "rows": [["  P-001  "]],
            "row_count": 1,
            "truncated": False,
        }
        with patch.object(
            mapped_query_service.datasource_service,
            "run_parameterized_query",
            return_value=raw_query_result,
        ) as run_query:
            result = json.loads(
                context.execute_tool(
                    "query_mapped_objects",
                    {
                        "entity_name": "项目",
                        "properties": ["项目编号"],
                    },
                )
            )

        queried_source, sql, _parameters = run_query.call_args.args
        self.assertIs(queried_source, staging_source)
        self.assertIn('"project_code"', sql)
        self.assertIn('FROM "projects"', sql)
        self.assertNotIn("draft_", sql)
        self.assertEqual(result["objects"], [{"项目编号": "P-001"}])
        self.assertEqual(result["lineage"]["data_source_id"], staging_source.id)
        self.assertEqual(result["lineage"]["definition"]["source"], "release")
        self.assertEqual(result["lineage"]["definition"]["snapshot_id"], snapshot.id)
        self.assertEqual(result["lineage"]["definition"]["release_id"], release.id)

    def test_sql_scope_proof_is_reusable_without_executing_the_query(self) -> None:
        context = self._context()
        with patch.object(agent_engine.datasource_service, "run_query") as run_query:
            normalized = context.validate_sql_query(
                "source-projects",
                "SELECT project_code FROM projects;",
            )
            self.assertEqual(normalized, "SELECT project_code FROM projects")
            run_query.assert_not_called()
            with self.assertRaises((PolicyViolation, PermissionError)):
                context.validate_sql_query(
                    "source-projects",
                    "SELECT password_hash FROM users",
                )

    def test_read_only_policy_rejects_postgres_mutating_select_forms(self) -> None:
        for unsafe_sql in (
            "SELECT project_code INTO archived_projects FROM projects",
            "SELECT nextval('project_sequence')",
            'SELECT "pg_sleep"(1)',
            "SELECT set_config('search_path', 'public', false)",
            "SELECT project_code FROM projects FOR UPDATE",
            "EXPLAIN ANALYZE SELECT project_code FROM projects",
        ):
            with self.assertRaises(PolicyViolation, msg=unsafe_sql):
                validate_read_only_sql(unsafe_sql)

    def test_agent_sql_rejects_noncanonical_mixed_case_mapping_identifiers(self) -> None:
        with self.assertRaises(PolicyViolation):
            validate_agent_sql_scope(
                "SELECT project_code FROM projects",
                {"PROJECTS": {"project_code"}},
            )
        with self.assertRaises(PolicyViolation):
            validate_agent_sql_scope(
                "SELECT project_code FROM projects",
                {"projects": {"PROJECT_CODE"}},
            )

    def _staging_release(self) -> tuple[OntologySnapshot, OntologyRelease]:
        branch = OntologyBranch(
            id="branch-agent-closure",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="main",
            created_by_user_id=self.user.id,
        )
        self.db.add(branch)
        self.db.flush()
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        snapshot = OntologySnapshot(
            id="snapshot-agent-closure",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            kind="merge",
            content=content,
            content_hash=release_service.snapshot_hash(content),
            created_by_user_id=self.user.id,
        )
        self.db.add(snapshot)
        self.db.flush()
        branch.base_snapshot_id = snapshot.id
        branch.head_snapshot_id = snapshot.id
        release = OntologyRelease(
            id="release-agent-closure",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="staging",
            status="released",
            created_by_user_id=self.user.id,
        )
        self.db.add(release)
        self.db.commit()
        return snapshot, release

    def test_non_dev_agent_uses_one_frozen_definition_for_every_resource(self) -> None:
        snapshot, release = self._staging_release()
        staging_source = DataSource(
            id="source-projects-staging",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目库（预发布）",
            type="postgres",
            status="connected",
        )
        staging_llm = LLMConfig(
            id="llm-agent-closure-staging",
            tenant_id=self.tenant.id,
            name="预发布模型",
            provider="openai",
            model="test-model",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        self.db.add_all([staging_source, staging_llm])
        self.agent.data_source_ids = [staging_source.id]
        self.agent.llm_config_id = staging_llm.id
        original_scenario_name = self.scenario.name
        self.scenario.name = "草稿施工风险场景"
        self.db.get(OntologyEntity, "entity-project").name = "草稿项目"
        self.db.get(OntologyProperty, "property-risk-score").name = "草稿风险分"
        self.db.get(DataMapping, "mapping-projects").table_name = "draft_projects"
        self.db.get(FunctionDefinition, "function-score").name = "草稿函数"
        self.db.get(OntologyAction, "action-mark-risk").name = "草稿操作"
        self.db.get(OntologyRule, "rule-high-risk").name = "草稿规则"
        self.db.get(OntologyEvent, "event-risk-found").name = "草稿事件"
        self.db.get(OntologyWorkflow, "workflow-risk-response").name = "草稿流程"
        self.db.commit()

        with (
            patch.object(
                agent_engine.runtime_connector_service,
                "runtime_environment",
                return_value="staging",
            ),
            patch.object(
                agent_engine.runtime_connector_service,
                "resolve_connector",
                return_value=(staging_source, {}),
            ),
        ):
            context = self._context()

        self.assertEqual(context.runtime_definition.snapshot_id, snapshot.id)
        self.assertEqual(context.runtime_definition.release_id, release.id)
        self.assertEqual(context.runtime_definition.scenario_name, original_scenario_name)
        self.assertEqual(context.data_sources[0].id, staging_source.id)
        frozen_project = next(item for item in context.entities if item.id == "entity-project")
        self.assertEqual(frozen_project.name, "项目")
        self.assertIn("风险分", {prop.name for prop in frozen_project.properties})
        self.assertEqual(context.mappings[0].table_name, "projects")
        self.assertIn("计算项目风险分", {item.name for item in context.functions})
        self.assertIn("标记高风险", {item.name for item in context.actions})
        self.assertIn("高风险项目", {item.name for item in context.rules})
        self.assertIn("发现高风险项目", {item.name for item in context.events})
        self.assertIn("高风险处置流程", {item.name for item in context.workflows})
        frozen_text = json.dumps(context._ontology_model(), ensure_ascii=False)
        self.assertNotIn("草稿", frozen_text)
        readiness = agents_router._agent_readiness_missing(
            self.db,
            self.agent,
            runtime_context=context,
        )
        self.assertNotIn("数据源", readiness)
        self.assertNotIn("数据映射", readiness)
        self.assertNotIn("映射数据绑定", readiness)

        prompts: list[str] = []

        def fake_chat_stream(_llm, messages, **_kwargs):
            prompts.append(messages[0]["content"])
            return iter([{"type": "token", "content": "完成"}])

        with patch.object(agent_engine.llm_service, "chat_stream", fake_chat_stream):
            list(
                agent_engine.run_agent(
                    self.db,
                    self.agent,
                    staging_llm,
                    [],
                    "继续",
                    self.scenario.name,
                    "",
                    runtime_context=context,
                )
            )
        self.assertIn(original_scenario_name, prompts[0])
        self.assertNotIn("草稿施工风险场景", prompts[0])

    def test_non_dev_agent_keeps_explicit_file_bucket_as_runtime_data_boundary(self) -> None:
        self._staging_release()
        bound_bucket = DataSource(
            id="source-live-bucket-staging",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="Agent 显式绑定资料库",
            type="file_bucket",
            status="connected",
        )
        self.db.add(bound_bucket)
        self.agent.data_source_ids = ["source-projects", bound_bucket.id]
        self.db.commit()

        with (
            patch.object(
                agent_engine.runtime_connector_service,
                "runtime_environment",
                return_value="staging",
            ),
            patch.object(
                agent_engine.runtime_connector_service,
                "resolve_connector",
                return_value=(self.db.get(DataSource, "source-projects"), {}),
            ),
        ):
            context = self._context()

        self.assertIn(bound_bucket.id, {source.id for source in context.data_sources})
        self.assertIn(
            "search_documents",
            {tool["function"]["name"] for tool in context.build_tools()},
        )

    def test_non_dev_agent_rejects_legacy_snapshot_missing_resource_collection(self) -> None:
        snapshot, _release = self._staging_release()
        broken = dict(snapshot.content)
        broken.pop("functions")
        snapshot.content = broken
        snapshot.content_hash = release_service.snapshot_hash(broken)
        self.db.commit()
        with patch.object(
            agent_engine.runtime_connector_service,
            "runtime_environment",
            return_value="staging",
        ), self.assertRaises(runtime_definition_service.RuntimeDefinitionError):
            self._context()

    def test_agent_turn_reuses_the_context_that_authorized_history(self) -> None:
        context = self._context()
        llm = LLMConfig(name="本轮真实模型", model="test-model")
        with (
            patch.object(
                agent_engine,
                "AgentContext",
                side_effect=AssertionError("同一回合不得再次解析运行定义"),
            ),
            patch.object(
                agent_engine.llm_service,
                "chat_stream",
                return_value=iter([{"type": "token", "content": "完成"}]),
            ),
        ):
            events = list(
                agent_engine.run_agent(
                    self.db,
                    self.agent,
                    llm,
                    [],
                    "继续处理",
                    self.scenario.name,
                    "",
                    runtime_context=context,
                )
            )

        self.assertEqual(events[-1], {"type": "done", "data": "完成"})
        self.assertIs(context.llm, llm)


if __name__ == "__main__":
    unittest.main()
