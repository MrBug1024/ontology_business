from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    Agent,
    AuthorizationGrant,
    BusinessScenario,
    LLMConfig,
    OntologyEntity,
    OntologyInstance,
    OntologyRelation,
    RelationInstance,
    Tenant,
    User,
)
from app.services import agent_engine, permission_service


class AgentRelationInferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-agent-inference", name="推理租户")
        user = User(
            id="user-agent-inference",
            tenant_id=tenant.id,
            email="inference@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-agent-inference",
            tenant_id=tenant.id,
            name="关系推理场景",
        )
        node = OntologyEntity(
            id="entity-node",
            scenario_id=scenario.id,
            name="节点",
        )
        project = OntologyEntity(
            id="entity-project",
            scenario_id=scenario.id,
            name="项目",
        )
        worker = OntologyEntity(
            id="entity-worker",
            scenario_id=scenario.id,
            name="人员",
        )
        friend = OntologyRelation(
            id="relation-friend",
            scenario_id=scenario.id,
            name="相识",
            source_entity_id=node.id,
            target_entity_id=node.id,
            relation_type="N:M",
            constraints={"symmetric": True},
        )
        precedes = OntologyRelation(
            id="relation-precedes",
            scenario_id=scenario.id,
            name="先于",
            source_entity_id=node.id,
            target_entity_id=node.id,
            relation_type="N:M",
            constraints={"transitive": True, "irreflexive": True},
        )
        contains = OntologyRelation(
            id="relation-contains",
            scenario_id=scenario.id,
            name="包含人员",
            source_entity_id=project.id,
            target_entity_id=worker.id,
            relation_type="1:N",
            constraints={"inverse_relation_id": "relation-works-for"},
        )
        works_for = OntologyRelation(
            id="relation-works-for",
            scenario_id=scenario.id,
            name="任职于",
            source_entity_id=worker.id,
            target_entity_id=project.id,
            relation_type="N:1",
            constraints={"inverse_relation_id": "relation-contains"},
        )
        objects = [
            OntologyInstance(id="object-a", scenario_id=scenario.id, entity_id=node.id, name="A"),
            OntologyInstance(id="object-b", scenario_id=scenario.id, entity_id=node.id, name="B"),
            OntologyInstance(id="object-c", scenario_id=scenario.id, entity_id=node.id, name="C"),
            OntologyInstance(id="object-hidden", scenario_id=scenario.id, entity_id=node.id, name="隐藏节点"),
            OntologyInstance(id="object-project", scenario_id=scenario.id, entity_id=project.id, name="项目甲"),
            OntologyInstance(id="object-worker", scenario_id=scenario.id, entity_id=worker.id, name="张工"),
        ]
        edges = [
            RelationInstance(id="edge-friend-ab", scenario_id=scenario.id, relation_id=friend.id, source_instance_id="object-a", target_instance_id="object-b"),
            RelationInstance(id="edge-friend-hidden", scenario_id=scenario.id, relation_id=friend.id, source_instance_id="object-a", target_instance_id="object-hidden"),
            RelationInstance(id="edge-precedes-ab", scenario_id=scenario.id, relation_id=precedes.id, source_instance_id="object-a", target_instance_id="object-b"),
            RelationInstance(id="edge-precedes-bc", scenario_id=scenario.id, relation_id=precedes.id, source_instance_id="object-b", target_instance_id="object-c"),
            RelationInstance(id="edge-contains", scenario_id=scenario.id, relation_id=contains.id, source_instance_id="object-project", target_instance_id="object-worker"),
        ]
        self.agent = Agent(
            id="agent-inference",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="关系推理 Agent",
        )
        self.db.add_all([
            tenant,
            user,
            scenario,
            node,
            project,
            worker,
            friend,
            precedes,
            contains,
            works_for,
            *objects,
            *edges,
            self.agent,
        ])
        self.db.commit()
        organization = permission_service.ensure_organization(
            self.db, tenant.id, owner_user_id=user.id
        )
        self.db.add(AuthorizationGrant(
            organization_id=organization.id,
            user_id=user.id,
            resource_type="object",
            resource_id="object-hidden",
            verb="read",
            effect="deny",
            created_by_user_id=user.id,
        ))
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _detail(self, object_id: str) -> dict:
        context = agent_engine.AgentContext(
            self.db, self.agent, LLMConfig(name="推理测试模型")
        )
        return json.loads(context.execute_tool(
            "get_ontology_object", {"object_id": object_id}
        ))

    def test_query_time_inference_is_bounded_visible_and_never_materialized(self) -> None:
        before = self.db.query(RelationInstance).count()

        a_detail = self._detail("object-a")
        transitive = next(
            item for item in a_detail["inferred_relations"]
            if item["relation_id"] == "relation-precedes"
            and item["related_object_id"] == "object-c"
        )
        self.assertEqual(transitive["inference"], ["transitive"])
        self.assertEqual(
            transitive["path"], ["edge-precedes-ab", "edge-precedes-bc"]
        )
        self.assertFalse(transitive["materialized"])
        self.assertNotIn(
            "object-hidden",
            {
                item["related_object_id"]
                for item in [
                    *a_detail["relations"],
                    *a_detail["inferred_relations"],
                ]
            },
        )

        b_detail = self._detail("object-b")
        symmetric = next(
            item for item in b_detail["inferred_relations"]
            if item["relation_id"] == "relation-friend"
            and item["related_object_id"] == "object-a"
        )
        self.assertEqual(symmetric["direction"], "outgoing")
        self.assertIn("symmetric", symmetric["inference"])

        worker_detail = self._detail("object-worker")
        inverse = next(
            item for item in worker_detail["inferred_relations"]
            if item["relation_id"] == "relation-works-for"
        )
        self.assertEqual(inverse["related_object_id"], "object-project")
        self.assertEqual(inverse["direction"], "outgoing")
        self.assertIn("inverse", inverse["inference"])

        self.assertEqual(self.db.query(RelationInstance).count(), before)
        self.assertIn("未写入数据库", a_detail["relation_semantics"])


if __name__ == "__main__":
    unittest.main()
