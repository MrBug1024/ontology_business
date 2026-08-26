from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, event, func, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    RelationDataMapping,
    ScenarioModelDraftResource,
    Tenant,
)
from app.services import function_definition_service, workflow_service
from examples import upgrade_wage_warning


class WageWarningUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp_dir.name) / "wage-warning-upgrade.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-wage-upgrade", name="欠薪预警隔离测试租户")
        self.scenario = BusinessScenario(
            id="scenario-wage-upgrade",
            tenant_id=tenant.id,
            name=upgrade_wage_warning.SCENARIO_NAME,
            description="原有场景说明必须保留",
            namespace="existing_namespace",
            status="draft",
        )
        self.file_bucket = DataSource(
            id="source-wage-documents",
            tenant_id=tenant.id,
            scenario_id=self.scenario.id,
            name="欠薪制度附件",
            type="file_bucket",
            config={"purpose": "source-documents-only"},
            status="ok",
        )
        self.unrelated_entity = OntologyEntity(
            id="entity-existing-source",
            scenario_id=self.scenario.id,
            name="既有来源记录",
            api_name="existing_source_record",
            namespace="existing_namespace",
            description="不能被升级脚本覆盖",
        )
        self.unrelated_property = OntologyProperty(
            id="property-existing-source-id",
            entity_id=self.unrelated_entity.id,
            name="来源编号",
            api_name="source_id",
            is_key=True,
        )
        self.unrelated_instance = OntologyInstance(
            id="instance-existing-source",
            scenario_id=self.scenario.id,
            entity_id=self.unrelated_entity.id,
            name="既有来源对象",
            attributes={"来源编号": "SOURCE-001"},
            source="imported",
            source_ref="existing-source.csv:1",
            source_metadata={"original": True},
        )
        project_spec = upgrade_wage_warning.ENTITY_SPECS[0]
        self.project = OntologyEntity(
            id="entity-project-by-api",
            scenario_id=self.scenario.id,
            name=project_spec["name"],
            api_name=project_spec["api_name"],
            namespace=upgrade_wage_warning.NAMESPACE,
            lifecycle_status="active",
            description=project_spec["description"],
            is_abstract=False,
            state_property=project_spec["state_property"],
        )
        self.project_properties = [
            OntologyProperty(
                id=f"property-project-v1-{index}",
                entity_id=self.project.id,
                **{
                    field: copy.deepcopy(property_spec[field])
                    for field in (
                        "name",
                        "api_name",
                        "data_type",
                        "description",
                        "is_key",
                        "is_title",
                        "is_required",
                        "is_enum",
                        "enum_values",
                        "default_value",
                        "constraints",
                        "is_sensitive",
                    )
                },
            )
            for index, property_spec in enumerate(project_spec["properties"])
        ]
        draft_payload = {"key": "entity.draft_only", "name": "用户正在编辑的草稿"}
        self.draft = ScenarioModelDraftResource(
            id="draft-wage-existing",
            tenant_id=tenant.id,
            scenario_id=self.scenario.id,
            proposal_id="proposal-wage-existing",
            resource_kind="entity",
            resource_key="entity.draft_only",
            resource_identity=hashlib.sha256(b"entity\0entity.draft_only").hexdigest(),
            title="用户正在编辑的草稿",
            source_payload=draft_payload,
            payload={**draft_payload, "user_edit": "保留"},
            validation_issues=[{"code": "source_pending"}],
            source_refs=["attachment:p0001"],
            draft_status="needs_validation",
            enabled=False,
            publishable=False,
            revision=3,
        )
        self.db.add_all([
            tenant,
            self.scenario,
            self.file_bucket,
            self.unrelated_entity,
            self.unrelated_property,
            self.unrelated_instance,
            self.project,
            *self.project_properties,
            self.draft,
        ])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _counts(self) -> dict[str, int]:
        models = {
            "entities": OntologyEntity,
            "properties": OntologyProperty,
            "relations": OntologyRelation,
            "instances": OntologyInstance,
            "functions": FunctionDefinition,
            "actions": OntologyAction,
            "rules": OntologyRule,
            "events": OntologyEvent,
            "workflows": OntologyWorkflow,
            "mappings": DataMapping,
            "relation_mappings": RelationDataMapping,
            "agents": Agent,
            "sources": DataSource,
            "drafts": ScenarioModelDraftResource,
        }
        return {
            name: int(self.db.scalar(select(func.count()).select_from(model)) or 0)
            for name, model in models.items()
        }

    @staticmethod
    def _snapshot(resource) -> dict:
        return {
            attribute.key: copy.deepcopy(getattr(resource, attribute.key))
            for attribute in sa_inspect(resource).mapper.column_attrs
        }

    def _ontology_snapshots(self, scenario_id: str) -> dict[tuple[str, str], dict]:
        result: dict[tuple[str, str], dict] = {}
        for label, model in (
            ("entity", OntologyEntity),
            ("property", OntologyProperty),
            ("relation", OntologyRelation),
        ):
            statement = select(model)
            if model is OntologyProperty:
                statement = statement.join(
                    OntologyEntity,
                    OntologyEntity.id == OntologyProperty.entity_id,
                ).where(OntologyEntity.scenario_id == scenario_id)
            else:
                statement = statement.where(model.scenario_id == scenario_id)
            for resource in self.db.scalars(statement).all():
                result[(label, resource.id)] = self._snapshot(resource)
        return result

    def _seed_unmarked_v1_ontology_pack(
        self,
        scenario: BusinessScenario,
    ) -> dict[str, object]:
        existing_entities = {
            entity.api_name: entity
            for entity in self.db.scalars(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == scenario.id
                )
            ).all()
        }
        entities: dict[str, OntologyEntity] = {}
        properties: list[OntologyProperty] = []
        for entity_index, spec in enumerate(upgrade_wage_warning.ENTITY_SPECS):
            entity = existing_entities.get(spec["api_name"])
            if entity is None:
                entity = OntologyEntity(
                    id=f"legacy-v1-entity-{scenario.id}-{entity_index}",
                    scenario_id=scenario.id,
                    name=spec["name"],
                    api_name=spec["api_name"],
                    namespace=upgrade_wage_warning.NAMESPACE,
                    lifecycle_status="active",
                    description=spec["description"],
                    is_abstract=False,
                    state_property=spec["state_property"],
                )
                self.db.add(entity)
                self.db.flush()
            existing_properties = {
                prop.api_name: prop
                for prop in self.db.scalars(
                    select(OntologyProperty).where(
                        OntologyProperty.entity_id == entity.id
                    )
                ).all()
            }
            for property_index, property_spec in enumerate(spec["properties"]):
                prop = existing_properties.get(property_spec["api_name"])
                if prop is None:
                    prop = OntologyProperty(
                        id=(
                            f"legacy-v1-property-{scenario.id}-{entity_index}-"
                            f"{property_index}"
                        ),
                        entity_id=entity.id,
                        **{
                            field: copy.deepcopy(property_spec[field])
                            for field in (
                                "name",
                                "api_name",
                                "data_type",
                                "description",
                                "is_key",
                                "is_title",
                                "is_required",
                                "is_enum",
                                "enum_values",
                                "default_value",
                                "constraints",
                                "is_sensitive",
                            )
                        },
                    )
                    self.db.add(prop)
                properties.append(prop)
            entities[spec["name"]] = entity
        self.db.flush()

        relations: list[OntologyRelation] = []
        for index, spec in enumerate(upgrade_wage_warning.RELATION_SPECS):
            relation = OntologyRelation(
                id=f"legacy-v1-relation-{scenario.id}-{index}",
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
                namespace=upgrade_wage_warning.NAMESPACE,
                source_entity_id=entities[spec["source"]].id,
                target_entity_id=entities[spec["target"]].id,
                source_display_name=spec["source_display_name"],
                source_api_name=spec["source_api_name"],
                target_display_name=spec["target_display_name"],
                target_api_name=spec["target_api_name"],
                storage_kind="none",
                relation_type=spec["relation_type"],
                constraints={},
                description=(
                    f"{spec['source']}与{spec['target']}的概念关系；尚未绑定物理数据。"
                ),
            )
            self.db.add(relation)
            relations.append(relation)
        self.db.commit()
        return {
            "entities": entities,
            "properties": properties,
            "relations": relations,
        }

    @staticmethod
    def _conflicting_resource(
        kind: str,
        scenario: BusinessScenario,
        entity: OntologyEntity,
    ):
        if kind == "function":
            return FunctionDefinition(
                id="conflict-function",
                scenario_id=scenario.id,
                name=upgrade_wage_warning.FUNCTION_SPEC["name"],
                description="用户自建的可执行同名函数",
                input_schema={
                    "type": "object",
                    "properties": {"score": {"type": "number"}},
                    "required": ["score"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"result": {"type": "number"}},
                    "additionalProperties": False,
                },
                runtime_kind="weighted_score",
                runtime_config={"weights": {"score": 1}},
            )
        if kind == "action":
            return OntologyAction(
                id="conflict-action",
                scenario_id=scenario.id,
                entity_id=entity.id,
                name=upgrade_wage_warning.ACTION_NAME,
                description="用户自建的同名 SQL 操作，即便停用也不能认领",
                input_schema={"type": "object", "properties": {}},
                executor_type="sql",
                executor_config={"sql": "SELECT 1"},
                enabled=False,
                requires_confirmation=False,
                idempotency_required=False,
            )
        if kind == "rule":
            return OntologyRule(
                id="conflict-rule",
                scenario_id=scenario.id,
                entity_id=entity.id,
                name=upgrade_wage_warning.RULE_NAME,
                description="用户自建的同名规则，即便停用也不能认领",
                condition={"field": "自定义字段", "op": "==", "value": "自定义值"},
                action_on_match="用户自定义处置",
                severity="warning",
                enabled=False,
            )
        if kind == "event":
            return OntologyEvent(
                id="conflict-event",
                scenario_id=scenario.id,
                name=upgrade_wage_warning.EVENT_NAME,
                description="用户自建的同名事件，即便停用也不能认领",
                payload_schema={"type": "object", "properties": {}},
                trigger_source="用户系统",
                enabled=False,
            )
        if kind == "workflow":
            return OntologyWorkflow(
                id="conflict-workflow",
                scenario_id=scenario.id,
                name=upgrade_wage_warning.WORKFLOW_NAME,
                description="用户自建的同名工作流，即便草稿停用也不能认领",
                trigger_type="manual",
                trigger_config={},
                steps=[],
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "开始"}},
                    {"id": "end", "type": "end", "data": {"label": "结束"}},
                ],
                edges=[{"id": "e1", "source": "start", "target": "end"}],
                status="draft",
                enabled=False,
            )
        raise AssertionError(f"unsupported conflict kind: {kind}")

    def _seed_unmarked_v1_runtime_pack(
        self,
        entities: dict[str, OntologyEntity],
    ) -> dict[str, object]:
        function_values = function_definition_service.normalize_definition(
            upgrade_wage_warning.FUNCTION_SPEC
        )
        function = FunctionDefinition(
            id="legacy-pack-function",
            scenario_id=self.scenario.id,
            **function_values,
        )
        action_schema = function_definition_service.normalize_schema(
            upgrade_wage_warning.ACTION_INPUT_SCHEMA,
            label="操作输入契约",
        )
        action = OntologyAction(
            id="legacy-pack-action",
            scenario_id=self.scenario.id,
            entity_id=entities["欠薪预警"].id,
            name=upgrade_wage_warning.ACTION_NAME,
            description=upgrade_wage_warning.ACTION_DESCRIPTION,
            input_schema=action_schema,
            executor_type="unbound",
            executor_config={},
            precondition=upgrade_wage_warning.ACTION_PRECONDITION,
            postcondition=upgrade_wage_warning.ACTION_POSTCONDITION,
            enabled=False,
            requires_confirmation=True,
            idempotency_required=True,
            permission_scope="scenario",
            access_scope="tenant",
        )
        self.db.add_all([function, action])
        self.db.flush()
        rule = OntologyRule(
            id="legacy-pack-rule",
            scenario_id=self.scenario.id,
            entity_id=entities["工资台账"].id,
            name=upgrade_wage_warning.RULE_NAME,
            description=upgrade_wage_warning.RULE_DESCRIPTION,
            condition=copy.deepcopy(upgrade_wage_warning.RULE_CONDITION),
            action_on_match=upgrade_wage_warning.ACTION_NAME,
            trigger_action_ids=[action.id],
            severity="critical",
            enabled=False,
        )
        event_definition = OntologyEvent(
            id="legacy-pack-event",
            scenario_id=self.scenario.id,
            name=upgrade_wage_warning.EVENT_NAME,
            description=upgrade_wage_warning.EVENT_DESCRIPTION,
            payload_schema=function_definition_service.normalize_schema(
                upgrade_wage_warning.EVENT_PAYLOAD_SCHEMA,
                label="事件载荷契约",
            ),
            trigger_source=upgrade_wage_warning.EVENT_TRIGGER_SOURCE,
            enabled=False,
        )
        self.db.add_all([rule, event_definition])
        self.db.flush()
        nodes, edges = upgrade_wage_warning._workflow_graph(action.id)
        workflow = OntologyWorkflow(
            id="legacy-pack-workflow",
            scenario_id=self.scenario.id,
            name=upgrade_wage_warning.WORKFLOW_NAME,
            description=upgrade_wage_warning.WORKFLOW_DESCRIPTION,
            trigger_type="event",
            trigger_config={"event_id": event_definition.id},
            steps=[],
            nodes=nodes,
            edges=edges,
            status="draft",
            enabled=False,
            access_scope="tenant",
        )
        self.db.add(workflow)
        self.db.commit()
        return {
            "function": function,
            "action": action,
            "rule": rule,
            "event": event_definition,
            "workflow": workflow,
        }

    def test_unmarked_entity_property_and_relation_conflicts_roll_back(self) -> None:
        for kind in ("entity", "property", "relation"):
            with self.subTest(kind=kind):
                scenario = BusinessScenario(
                    id=f"ontology-conflict-{kind}",
                    tenant_id=self.scenario.tenant_id,
                    name=upgrade_wage_warning.SCENARIO_NAME,
                    description=f"{kind} ontology conflict scenario",
                    namespace=f"ontology_conflict_{kind}",
                    status="draft",
                )
                self.db.add(scenario)
                self.db.commit()
                if kind == "entity":
                    spec = upgrade_wage_warning.ENTITY_SPECS[0]
                    resource = OntologyEntity(
                        id="ontology-conflict-entity-row",
                        scenario_id=scenario.id,
                        name=spec["name"],
                        api_name=spec["api_name"],
                        namespace=upgrade_wage_warning.NAMESPACE,
                        lifecycle_status="active",
                        description="用户修改过的对象说明",
                        is_abstract=False,
                        state_property=spec["state_property"],
                    )
                    self.db.add(resource)
                    error_pattern = "对象类型.*未标记资源占用"
                else:
                    ontology = self._seed_unmarked_v1_ontology_pack(scenario)
                    if kind == "property":
                        resource = ontology["properties"][0]
                        resource.description = "用户修改过的属性说明"
                        error_pattern = "属性.*未标记资源占用"
                    else:
                        resource = ontology["relations"][0]
                        resource.relation_type = "1:1"
                        error_pattern = "关系类型.*未标记资源占用"
                self.db.commit()
                self.db.expire_all()

                before_counts = self._counts()
                before_ontology = self._ontology_snapshots(scenario.id)
                before_scenario = self._snapshot(scenario)
                self.assertFalse(upgrade_wage_warning._has_recovery_marker(resource))

                with self.assertRaisesRegex(RuntimeError, error_pattern):
                    upgrade_wage_warning.upgrade(
                        self.db,
                        scenario_id=scenario.id,
                    )

                self.db.expire_all()
                self.assertEqual(self._counts(), before_counts)
                self.assertEqual(self._ontology_snapshots(scenario.id), before_ontology)
                self.assertEqual(
                    self._snapshot(self.db.get(BusinessScenario, scenario.id)),
                    before_scenario,
                )

    def test_unmarked_same_name_runtime_resources_abort_without_changes(self) -> None:
        models = {
            "function": FunctionDefinition,
            "action": OntologyAction,
            "rule": OntologyRule,
            "event": OntologyEvent,
            "workflow": OntologyWorkflow,
        }
        for kind, model in models.items():
            with self.subTest(kind=kind):
                scenario = BusinessScenario(
                    id=f"scenario-conflict-{kind}",
                    tenant_id=self.scenario.tenant_id,
                    name=upgrade_wage_warning.SCENARIO_NAME,
                    description=f"{kind} conflict scenario",
                    namespace=f"conflict_{kind}",
                    status="draft",
                )
                self.db.add(scenario)
                self.db.commit()
                ontology = self._seed_unmarked_v1_ontology_pack(scenario)
                entity = ontology["entities"]["欠薪风险"]
                resource = self._conflicting_resource(kind, scenario, entity)
                resource.id = f"conflict-{kind}"
                self.db.add(resource)
                self.db.commit()
                # Compare two SQLite-loaded snapshots.  Newly inserted ORM
                # datetimes retain UTC tzinfo until first reload, while SQLite
                # returns the same value as a naive datetime.
                self.db.expire_all()

                before_counts = self._counts()
                before_resource = self._snapshot(resource)
                before_ontology = self._ontology_snapshots(scenario.id)
                before_scenario = self._snapshot(scenario)
                self.assertFalse(upgrade_wage_warning._has_recovery_marker(resource))

                with self.assertRaisesRegex(RuntimeError, "未标记资源占用"):
                    upgrade_wage_warning.upgrade(
                        self.db,
                        scenario_id=scenario.id,
                    )

                self.db.expire_all()
                self.assertEqual(self._counts(), before_counts)
                self.assertEqual(
                    self._snapshot(self.db.get(model, resource.id)),
                    before_resource,
                )
                self.assertEqual(self._ontology_snapshots(scenario.id), before_ontology)
                self.assertEqual(
                    self._snapshot(self.db.get(BusinessScenario, scenario.id)),
                    before_scenario,
                )

    def test_exact_unmarked_v1_inert_pack_is_adopted_once_then_stable(self) -> None:
        ontology = self._seed_unmarked_v1_ontology_pack(self.scenario)
        legacy = self._seed_unmarked_v1_runtime_pack(ontology["entities"])
        legacy_ids = {kind: resource.id for kind, resource in legacy.items()}
        ontology_resources = [
            *ontology["entities"].values(),
            *ontology["properties"],
            *ontology["relations"],
        ]
        self.assertTrue(
            all(not upgrade_wage_warning._has_recovery_marker(item) for item in legacy.values())
        )
        self.assertTrue(
            all(
                not upgrade_wage_warning._has_recovery_marker(item)
                for item in ontology_resources
            )
        )
        ontology_ids = {
            type(resource): {
                item.id
                for item in ontology_resources
                if isinstance(item, type(resource))
            }
            for resource in ontology_resources
        }

        first = upgrade_wage_warning.upgrade(self.db, scenario_id=self.scenario.id)
        counts_after_first = self._counts()
        second = upgrade_wage_warning.upgrade(self.db, scenario_id=self.scenario.id)

        self.assertEqual(first, second)
        self.assertEqual(self._counts(), counts_after_first)
        self.assertEqual(first["function_id"], legacy_ids["function"])
        self.assertEqual(first["action_id"], legacy_ids["action"])
        self.assertEqual(first["rule_id"], legacy_ids["rule"])
        self.assertEqual(first["event_id"], legacy_ids["event"])
        self.assertEqual(first["workflow_id"], legacy_ids["workflow"])
        for kind, model in {
            "function": FunctionDefinition,
            "action": OntologyAction,
            "rule": OntologyRule,
            "event": OntologyEvent,
            "workflow": OntologyWorkflow,
        }.items():
            resource = self.db.get(model, legacy_ids[kind])
            self.assertTrue(upgrade_wage_warning._has_recovery_marker(resource))
            self.assertEqual(
                resource.description.splitlines().count(
                    upgrade_wage_warning.RECOVERY_MARKER
                ),
                1,
            )
        for model, expected_ids in ontology_ids.items():
            refreshed = [self.db.get(model, resource_id) for resource_id in expected_ids]
            self.assertEqual({item.id for item in refreshed}, expected_ids)
            for resource in refreshed:
                self.assertTrue(upgrade_wage_warning._has_recovery_marker(resource))
                self.assertEqual(
                    resource.description.splitlines().count(
                        upgrade_wage_warning.RECOVERY_MARKER
                    ),
                    1,
                )

    def test_upgrade_is_idempotent_inert_and_non_destructive(self) -> None:
        first = upgrade_wage_warning.upgrade(
            self.db,
            scenario_id=self.scenario.id,
        )
        ids_after_first = {
            "entities": set(self.db.scalars(
                select(OntologyEntity.id).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_wage_warning.NAMESPACE,
                )
            )),
            "relations": set(self.db.scalars(
                select(OntologyRelation.id).where(
                    OntologyRelation.scenario_id == self.scenario.id,
                    OntologyRelation.namespace == upgrade_wage_warning.NAMESPACE,
                )
            )),
            "examples": set(self.db.scalars(
                select(OntologyInstance.id).where(
                    OntologyInstance.scenario_id == self.scenario.id,
                    OntologyInstance.source_ref.like(
                        f"{upgrade_wage_warning.EXAMPLE_SOURCE_PREFIX}%"
                    ),
                )
            )),
        }
        counts_after_first = self._counts()

        second = upgrade_wage_warning.upgrade(
            self.db,
            scenario_id=self.scenario.id,
        )
        self.assertEqual(first, second)
        self.assertEqual(counts_after_first, self._counts())
        self.assertEqual(
            ids_after_first["entities"],
            set(self.db.scalars(
                select(OntologyEntity.id).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_wage_warning.NAMESPACE,
                )
            )),
        )
        self.assertEqual(
            ids_after_first["relations"],
            set(self.db.scalars(
                select(OntologyRelation.id).where(
                    OntologyRelation.scenario_id == self.scenario.id,
                    OntologyRelation.namespace == upgrade_wage_warning.NAMESPACE,
                )
            )),
        )
        self.assertEqual(
            ids_after_first["examples"],
            set(self.db.scalars(
                select(OntologyInstance.id).where(
                    OntologyInstance.scenario_id == self.scenario.id,
                    OntologyInstance.source_ref.like(
                        f"{upgrade_wage_warning.EXAMPLE_SOURCE_PREFIX}%"
                    ),
                )
            )),
        )

        self.assertEqual(
            {key: first[key] for key in ("entities", "properties", "relations", "example_instances")},
            {"entities": 6, "properties": 30, "relations": 5, "example_instances": 2},
        )
        expected_api_names = {spec["api_name"] for spec in upgrade_wage_warning.ENTITY_SPECS}
        entities = self.db.scalars(
            select(OntologyEntity).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.api_name.in_(expected_api_names),
            )
        ).all()
        self.assertEqual(len(entities), 6)
        self.assertEqual(sum(len(entity.properties) for entity in entities), 30)
        self.assertEqual(self.db.get(OntologyEntity, "entity-project-by-api").name, "建设项目")
        for entity in entities:
            self.assertEqual(entity.lifecycle_status, "active")
            self.assertTrue(entity.state_property)
            self.assertIn(entity.state_property, {prop.name for prop in entity.properties})
            self.assertTrue(upgrade_wage_warning._has_recovery_marker(entity))
            self.assertTrue(
                all(
                    upgrade_wage_warning._has_recovery_marker(prop)
                    for prop in entity.properties
                )
            )

        relations = self.db.scalars(
            select(OntologyRelation).where(
                OntologyRelation.scenario_id == self.scenario.id,
                OntologyRelation.namespace == upgrade_wage_warning.NAMESPACE,
            )
        ).all()
        self.assertEqual(len(relations), 5)
        self.assertTrue(all(relation.storage_kind == "none" for relation in relations))
        self.assertTrue(
            all(
                upgrade_wage_warning._has_recovery_marker(relation)
                for relation in relations
            )
        )

        examples = self.db.scalars(
            select(OntologyInstance).where(
                OntologyInstance.scenario_id == self.scenario.id,
                OntologyInstance.source_ref.like(
                    f"{upgrade_wage_warning.EXAMPLE_SOURCE_PREFIX}%"
                ),
            )
        ).all()
        self.assertEqual(len(examples), 2)
        self.assertTrue(all(item.name.startswith("示例") for item in examples))
        self.assertTrue(all(item.source == "manual" for item in examples))
        self.assertTrue(
            all(item.source_metadata.get("record_kind") == "example" for item in examples)
        )
        self.assertTrue(
            all(item.quality.get("verified_business_fact") is False for item in examples)
        )

        function = self.db.scalar(select(FunctionDefinition).where(
            FunctionDefinition.scenario_id == self.scenario.id,
            FunctionDefinition.name == upgrade_wage_warning.FUNCTION_SPEC["name"],
        ))
        self.assertEqual(function.runtime_kind, "contract")
        self.assertEqual(function.runtime_config, {})

        action = self.db.scalar(select(OntologyAction).where(
            OntologyAction.scenario_id == self.scenario.id,
            OntologyAction.name == upgrade_wage_warning.ACTION_NAME,
        ))
        rule = self.db.scalar(select(OntologyRule).where(
            OntologyRule.scenario_id == self.scenario.id,
            OntologyRule.name == upgrade_wage_warning.RULE_NAME,
        ))
        event_definition = self.db.scalar(select(OntologyEvent).where(
            OntologyEvent.scenario_id == self.scenario.id,
            OntologyEvent.name == upgrade_wage_warning.EVENT_NAME,
        ))
        workflow = self.db.scalar(select(OntologyWorkflow).where(
            OntologyWorkflow.scenario_id == self.scenario.id,
            OntologyWorkflow.name == upgrade_wage_warning.WORKFLOW_NAME,
        ))
        self.assertEqual((action.executor_type, action.enabled), ("unbound", False))
        self.assertEqual(action.executor_config, {})
        self.assertFalse(rule.enabled)
        self.assertEqual(rule.trigger_action_ids, [action.id])
        self.assertFalse(event_definition.enabled)
        self.assertEqual(workflow.trigger_config, {"event_id": event_definition.id})
        self.assertEqual((workflow.status, workflow.enabled), ("draft", False))
        self.assertEqual(
            [node["type"] for node in workflow.nodes],
            ["start", "action", "approval", "end"],
        )
        self.assertEqual(
            [(edge["source"], edge["target"]) for edge in workflow.edges],
            [("start", "create_warning"), ("create_warning", "review"), ("review", "end")],
        )
        self.assertEqual(workflow.nodes[1]["data"]["action_id"], action.id)
        workflow_service.validate_workflow_definition(workflow.nodes, workflow.edges)
        workflow_service.validate_workflow_references(
            self.db,
            self.scenario.id,
            steps=workflow.steps,
            nodes=workflow.nodes,
        )

        self.assertEqual(self._counts()["mappings"], 0)
        self.assertEqual(self._counts()["relation_mappings"], 0)
        self.assertEqual(self._counts()["agents"], 0)
        self.assertEqual(self._counts()["sources"], 1)
        self.assertEqual(self.db.get(DataSource, self.file_bucket.id).config, {
            "purpose": "source-documents-only"
        })
        self.assertEqual(self.db.get(OntologyEntity, self.unrelated_entity.id).description, "不能被升级脚本覆盖")
        self.assertEqual(
            self.db.get(OntologyInstance, self.unrelated_instance.id).source_ref,
            "existing-source.csv:1",
        )
        preserved_draft = self.db.get(ScenarioModelDraftResource, self.draft.id)
        self.assertEqual(preserved_draft.payload["user_edit"], "保留")
        self.assertEqual(preserved_draft.source_refs, ["attachment:p0001"])
        self.assertEqual(preserved_draft.revision, 3)
        self.assertEqual(self.db.get(BusinessScenario, self.scenario.id).namespace, "existing_namespace")
        self.assertEqual(self.db.get(BusinessScenario, self.scenario.id).description, "原有场景说明必须保留")


if __name__ == "__main__":
    unittest.main()
