from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import database
from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRelease,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    RelationInstance,
    Tenant,
    User,
)
from app.routers import scenarios as scenario_routes
from app.schemas import EntityIn, PropertyIn
from app.services import (
    ontology_service,
    permission_service,
    release_service,
    runtime_definition_service,
)


def test_legacy_entity_lifecycle_migration_backfills_idempotently() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE ontology_entities "
                "(id VARCHAR(32) PRIMARY KEY, scenario_id VARCHAR(32), name VARCHAR(200))"
            )
            conn.execute(
                text("INSERT INTO ontology_entities VALUES ('legacy', 'scenario', '旧对象')")
            )
        with patch.object(database, "engine", engine):
            database._migrate_ontology_entity_lifecycle()
            database._migrate_ontology_entity_lifecycle()
        assert "lifecycle_status" in {
            column["name"] for column in inspect(engine).get_columns("ontology_entities")
        }
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT lifecycle_status FROM ontology_entities WHERE id='legacy'")
            ).scalar_one() == "active"
    finally:
        engine.dispose()


def test_entity_lifecycle_schema_is_closed() -> None:
    assert EntityIn(name="业务对象").lifecycle_status == "active"
    with pytest.raises(ValidationError):
        EntityIn(name="业务对象", lifecycle_status="removed")  # type: ignore[arg-type]


class TestOntologyEntityLifecycleRuntime:
    def setup_method(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.tenant = Tenant(id="tenant-lifecycle", name="生命周期租户")
        self.user = User(
            id="user-lifecycle",
            tenant_id=self.tenant.id,
            email="lifecycle@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-lifecycle",
            tenant_id=self.tenant.id,
            name="本体生命周期",
            namespace="lifecycle.test",
        )
        self.active_entity = self._entity("entity-active", "业务对象", "active")
        self.deprecated_entity = self._entity(
            "entity-deprecated", "旧技术元对象", "deprecated"
        )
        self.active_relation = OntologyRelation(
            id="relation-active",
            scenario_id=self.scenario.id,
            name="业务关联",
            source_entity_id=self.active_entity.id,
            target_entity_id=self.active_entity.id,
            relation_type="1:N",
        )
        self.deprecated_relation = OntologyRelation(
            id="relation-deprecated",
            scenario_id=self.scenario.id,
            name="技术关联",
            source_entity_id=self.active_entity.id,
            target_entity_id=self.deprecated_entity.id,
            relation_type="1:N",
        )
        self.source = DataSource(
            id="source-lifecycle",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="生命周期测试源",
            type="sqlite",
            config={"path": "unused.db"},
        )
        self.active_mapping = DataMapping(
            id="mapping-active",
            scenario_id=self.scenario.id,
            entity_id=self.active_entity.id,
            data_source_id=self.source.id,
            table_name="business_objects",
            column_map={"对象编号": "id"},
        )
        self.deprecated_mapping = DataMapping(
            id="mapping-deprecated",
            scenario_id=self.scenario.id,
            entity_id=self.deprecated_entity.id,
            data_source_id=self.source.id,
            table_name="technical_objects",
            column_map={"对象编号": "id"},
        )
        self.active_action = OntologyAction(
            id="action-active",
            scenario_id=self.scenario.id,
            entity_id=self.active_entity.id,
            name="业务操作",
            executor_type="unbound",
        )
        self.deprecated_action = OntologyAction(
            id="action-deprecated",
            scenario_id=self.scenario.id,
            entity_id=self.deprecated_entity.id,
            name="技术操作",
            executor_type="unbound",
        )
        self.active_rule = OntologyRule(
            id="rule-active",
            scenario_id=self.scenario.id,
            entity_id=self.active_entity.id,
            name="业务规则",
            condition={},
        )
        self.deprecated_rule = OntologyRule(
            id="rule-deprecated",
            scenario_id=self.scenario.id,
            entity_id=self.deprecated_entity.id,
            name="技术规则",
            condition={},
            trigger_action_ids=[self.deprecated_action.id],
        )
        self.active_workflow = OntologyWorkflow(
            id="workflow-active",
            scenario_id=self.scenario.id,
            name="业务流程",
            steps=[{"step": 1, "type": "action", "action_id": self.active_action.id}],
            status="active",
            enabled=True,
        )
        self.deprecated_workflow = OntologyWorkflow(
            id="workflow-deprecated",
            scenario_id=self.scenario.id,
            name="技术流程",
            steps=[{"step": 1, "type": "action", "action_id": self.deprecated_action.id}],
            status="active",
            enabled=True,
        )
        self.active_object_a = OntologyInstance(
            id="object-active-a",
            scenario_id=self.scenario.id,
            entity_id=self.active_entity.id,
            name="业务对象 A",
            attributes={"对象编号": "A"},
        )
        self.active_object_b = OntologyInstance(
            id="object-active-b",
            scenario_id=self.scenario.id,
            entity_id=self.active_entity.id,
            name="业务对象 B",
            attributes={"对象编号": "B"},
        )
        self.deprecated_object = OntologyInstance(
            id="object-deprecated",
            scenario_id=self.scenario.id,
            entity_id=self.deprecated_entity.id,
            name="旧技术对象实例",
            attributes={"对象编号": "T"},
        )
        self.active_link = RelationInstance(
            id="link-active",
            scenario_id=self.scenario.id,
            relation_id=self.active_relation.id,
            source_instance_id=self.active_object_a.id,
            target_instance_id=self.active_object_b.id,
        )
        self.deprecated_link = RelationInstance(
            id="link-deprecated",
            scenario_id=self.scenario.id,
            relation_id=self.deprecated_relation.id,
            source_instance_id=self.active_object_a.id,
            target_instance_id=self.deprecated_object.id,
        )
        self.db.add_all([
            self.tenant,
            self.user,
            self.scenario,
            self.active_entity,
            self.deprecated_entity,
            self.active_relation,
            self.deprecated_relation,
            self.source,
            self.active_mapping,
            self.deprecated_mapping,
            self.active_action,
            self.deprecated_action,
            self.active_rule,
            self.deprecated_rule,
            self.active_workflow,
            self.deprecated_workflow,
            self.active_object_a,
            self.active_object_b,
            self.deprecated_object,
            self.active_link,
            self.deprecated_link,
        ])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _entity(self, entity_id: str, name: str, lifecycle_status: str) -> OntologyEntity:
        entity = OntologyEntity(
            id=entity_id,
            scenario_id="scenario-lifecycle",
            name=name,
            api_name=entity_id.replace("-", "_"),
            lifecycle_status=lifecycle_status,
        )
        entity.properties = [
            OntologyProperty(
                id=f"property-{entity_id[-6:]}",
                name="对象编号",
                api_name="object_id",
                data_type="string",
                is_key=True,
                is_title=True,
                is_required=True,
            )
        ]
        return entity

    def test_live_runtime_detail_and_graph_hide_deprecated_closure(self) -> None:
        definition = runtime_definition_service.resolve_active(
            self.db, self.scenario, environment="dev"
        )
        assert set(definition.entities) == {self.active_entity.id}
        assert set(definition.relations) == {self.active_relation.id}
        assert set(definition.mappings) == {self.active_mapping.id}
        assert set(definition.actions) == {self.active_action.id}
        assert set(definition.rules) == {self.active_rule.id}
        assert set(definition.workflows) == {self.active_workflow.id}
        assert ontology_service.instance_in_runtime_definition(
            self.active_object_a, definition
        )
        assert not ontology_service.instance_in_runtime_definition(
            self.deprecated_object, definition
        )
        assert not ontology_service.relation_instance_in_runtime_definition(
            self.deprecated_link, definition
        )

        detail = scenario_routes.get_scenario(self.scenario.id, self.db)
        assert {item.id for item in detail.entities} == {self.active_entity.id}
        assert {item.id for item in detail.relations} == {self.active_relation.id}
        assert {item.id for item in detail.mappings} == {self.active_mapping.id}
        assert {item.id for item in detail.instances} == {
            self.active_object_a.id,
            self.active_object_b.id,
        }
        assert {item.id for item in detail.relation_instances} == {self.active_link.id}

        schema_graph = scenario_routes.scenario_graph(
            self.scenario.id, mode="schema", db=self.db
        )
        assert {item["id"] for item in schema_graph["nodes"]} == {self.active_entity.id}
        assert {item["id"] for item in schema_graph["edges"]} == {self.active_relation.id}
        instance_graph = scenario_routes.scenario_graph(
            self.scenario.id, mode="instance", db=self.db
        )
        assert {item["id"] for item in instance_graph["nodes"]} == {
            self.active_object_a.id,
            self.active_object_b.id,
        }
        assert {item["id"] for item in instance_graph["edges"]} == {self.active_link.id}

    def test_new_and_released_snapshots_keep_lifecycle_filter_without_deleting_facts(self) -> None:
        captured = release_service.capture_snapshot_content(self.db, self.scenario)
        assert [(item["id"], item["lifecycle_status"]) for item in captured["entities"]] == [
            (self.active_entity.id, "active")
        ]
        assert {item["id"] for item in captured["relations"]} == {self.active_relation.id}
        assert {item["id"] for item in captured["mappings"]} == {self.active_mapping.id}
        assert {item["id"] for item in captured["actions"]} == {self.active_action.id}
        assert {item["id"] for item in captured["rules"]} == {self.active_rule.id}
        assert {item["id"] for item in captured["workflows"]} == {self.active_workflow.id}
        assert self.db.get(OntologyEntity, self.deprecated_entity.id) is not None
        assert self.db.get(OntologyInstance, self.deprecated_object.id) is not None
        assert self.db.get(RelationInstance, self.deprecated_link.id) is not None

        # Simulate an immutable historic snapshot which explicitly carries both
        # lifecycle states. Runtime projection must still match the live graph.
        self.deprecated_entity.lifecycle_status = "active"
        self.db.flush()
        historic = release_service.capture_snapshot_content(self.db, self.scenario)
        next(
            item for item in historic["entities"] if item["id"] == self.deprecated_entity.id
        )["lifecycle_status"] = "deprecated"
        self.deprecated_entity.lifecycle_status = "deprecated"
        branch = OntologyBranch(
            id="branch-lifecycle",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="lifecycle/main",
            created_by_user_id=self.user.id,
        )
        snapshot = OntologySnapshot(
            id="snapshot-lifecycle",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            kind="merge",
            content=historic,
            content_hash=release_service.snapshot_hash(historic),
            created_by_user_id=self.user.id,
        )
        release = OntologyRelease(
            id="release-lifecycle",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="staging",
            status="released",
            created_by_user_id=self.user.id,
        )
        self.db.add_all([branch, snapshot, release])
        self.db.commit()
        frozen = runtime_definition_service.resolve_active(
            self.db, self.scenario, environment="staging"
        )
        assert set(frozen.entities) == {self.active_entity.id}
        assert set(frozen.relations) == {self.active_relation.id}
        assert set(frozen.mappings) == {self.active_mapping.id}
        assert set(frozen.actions) == {self.active_action.id}
        assert set(frozen.rules) == {self.active_rule.id}
        assert set(frozen.workflows) == {self.active_workflow.id}
        frozen_graph = ontology_service.build_graph(
            self.scenario,
            mode="schema",
            db=self.db,
            runtime_definition=frozen,
        )
        assert {item["id"] for item in frozen_graph["nodes"]} == {self.active_entity.id}
        assert {item["id"] for item in frozen_graph["edges"]} == {self.active_relation.id}

    def test_deprecated_legacy_shape_does_not_block_a_new_release_snapshot(self) -> None:
        # The technical meta-model predates today's one-key/one-title contract.
        # Retirement must let users publish the valid business ontology without
        # deleting that historical definition or its facts.
        self.deprecated_entity.properties.clear()
        self.db.commit()
        captured = release_service.capture_snapshot_content(self.db, self.scenario)
        assert {item["id"] for item in captured["entities"]} == {self.active_entity.id}
        assert self.db.get(OntologyEntity, self.deprecated_entity.id) is not None
        assert self.db.get(OntologyInstance, self.deprecated_object.id) is not None
