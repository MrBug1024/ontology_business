from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    RelationInstance,
    Tenant,
    User,
)
from app.services import (
    ontology_service,
    permission_service,
    release_service,
    scenario_model_compiler,
)


class RelationConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.tenant = Tenant(id="tenant-relation", name="关系约束租户")
        self.user = User(
            id="user-relation",
            tenant_id=self.tenant.id,
            email="relation@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-relation",
            tenant_id=self.tenant.id,
            name="关系约束场景",
            namespace="relation.test",
            status="draft",
        )
        node = OntologyEntity(
            id="entity-node",
            scenario_id=self.scenario.id,
            name="节点",
        )
        node.properties = [
            OntologyProperty(
                id="property-node-key",
                name="节点编号",
                data_type="string",
                is_key=True,
                is_title=True,
                is_required=True,
            )
        ]
        objects = [
            OntologyInstance(
                id=f"object-{suffix}",
                scenario_id=self.scenario.id,
                entity_id=node.id,
                name=suffix.upper(),
            )
            for suffix in ("a", "b", "c")
        ]
        self.relation = OntologyRelation(
            id="relation-precedes",
            scenario_id=self.scenario.id,
            name="先于",
            source_entity_id=node.id,
            target_entity_id=node.id,
            relation_type="N:M",
            constraints={},
        )
        self.db.add_all([
            self.tenant,
            self.user,
            self.scenario,
            node,
            *objects,
            self.relation,
        ])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _edge(self, edge_id: str, source: str, target: str) -> RelationInstance:
        edge = RelationInstance(
            id=edge_id,
            scenario_id=self.scenario.id,
            relation_id=self.relation.id,
            source_instance_id=source,
            target_instance_id=target,
        )
        self.db.add(edge)
        self.db.flush()
        return edge

    def test_create_enforces_graph_axioms_and_max_cardinality(self) -> None:
        self.relation.constraints = {
            "transitive": True,
            "irreflexive": True,
            "antisymmetric": True,
            "source_max_cardinality": 1,
        }
        self._edge("edge-ab", "object-a", "object-b")
        self._edge("edge-bc", "object-b", "object-c")

        with self.assertRaisesRegex(ValueError, "反自反"):
            ontology_service.validate_relation_instance_create(
                self.db,
                self.relation,
                source_instance_id="object-c",
                target_instance_id="object-c",
            )
        with self.assertRaisesRegex(ValueError, "反对称"):
            ontology_service.validate_relation_instance_create(
                self.db,
                self.relation,
                source_instance_id="object-b",
                target_instance_id="object-a",
            )
        with self.assertRaisesRegex(ValueError, "最大目标基数"):
            ontology_service.validate_relation_instance_create(
                self.db,
                self.relation,
                source_instance_id="object-a",
                target_instance_id="object-c",
            )
        with self.assertRaisesRegex(ValueError, "形成.*环"):
            ontology_service.validate_relation_instance_create(
                self.db,
                self.relation,
                source_instance_id="object-c",
                target_instance_id="object-a",
            )

    def test_delete_enforces_min_cardinality_for_only_removed_row(self) -> None:
        self.relation.constraints = {
            "source_min_cardinality": 1,
            "target_min_cardinality": 1,
        }
        edge = self._edge("edge-ab", "object-a", "object-b")
        with self.assertRaisesRegex(ValueError, "最小目标基数"):
            ontology_service.validate_relation_instance_delete(
                self.db, self.relation, edge
            )

    def test_snapshot_preserves_closed_relation_constraints(self) -> None:
        self.relation.constraints = {
            "transitive": True,
            "irreflexive": True,
            "source_max_cardinality": 3,
        }
        self.db.commit()
        content = release_service.capture_snapshot_content(self.db, self.scenario)
        normalized = release_service.normalize_snapshot_content(content)
        relation = next(item for item in normalized["relations"] if item["id"] == self.relation.id)
        self.assertEqual(
            relation["constraints"],
            {
                "transitive": True,
                "irreflexive": True,
                "source_max_cardinality": 3,
            },
        )

    def test_cross_type_asymmetric_antisymmetric_and_acyclic_axioms_are_valid(self) -> None:
        constraints = ontology_service.normalize_relation_constraints({
            "asymmetric": True,
            "antisymmetric": True,
            "acyclic": True,
        })

        ontology_service.validate_relation_constraint_endpoints(
            constraints,
            source_entity_id="entity-source",
            target_entity_id="entity-target",
        )

        for same_type_only in ({"symmetric": True}, {"transitive": True}):
            with self.assertRaisesRegex(ValueError, "对称和传递"):
                ontology_service.validate_relation_constraint_endpoints(
                    same_type_only,
                    source_entity_id="entity-source",
                    target_entity_id="entity-target",
                )


class RelationConstraintCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-relation-compiler", name="关系编译租户")
        user = User(
            id="user-relation-compiler",
            tenant_id=tenant.id,
            email="relation.compiler@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-relation-compiler",
            tenant_id=tenant.id,
            name="关系编译场景",
            namespace="relation.compiler",
        )
        self.db.add_all([tenant, user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, tenant.id, owner_user_id=user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _bundle(self) -> dict:
        return scenario_model_compiler.build_source_bundle(
            "编译关系模型",
            [{
                "id": "relation-doc",
                "filename": "relation.md",
                "text": "容器包含构件，构件属于容器；节点先后关系无环且可传递。",
            }],
        )

    def _raw(self) -> dict:
        ref = "relation-doc:p0001"
        entities = []
        for key, name in (("entity.container", "容器"), ("entity.component", "构件")):
            entities.append({
                "key": key,
                "name": name,
                "properties": [{
                    "name": "编号",
                    "data_type": "string",
                    "is_key": True,
                    "is_required": True,
                }],
                "evidence_refs": [ref],
                "confidence": 0.99,
            })
        return {
            "schema_version": "scenario_model.v1",
            "entities": entities,
            "relations": [
                {
                    "key": "relation.contains",
                    "name": "包含",
                    "source_ref": "entity.container",
                    "target_ref": "entity.component",
                    "relation_type": "1:N",
                    "constraints": {"source_max_cardinality": 5},
                    "inverse_relation_ref": "relation.belongs_to",
                    "evidence_refs": [ref],
                    "confidence": 0.99,
                },
                {
                    "key": "relation.belongs_to",
                    "name": "属于",
                    "source_ref": "entity.component",
                    "target_ref": "entity.container",
                    "relation_type": "N:1",
                    "inverse_relation_ref": "relation.contains",
                    "evidence_refs": [ref],
                    "confidence": 0.99,
                },
                {
                    "key": "relation.precedes",
                    "name": "先于",
                    "source_ref": "entity.component",
                    "target_ref": "entity.component",
                    "relation_type": "N:M",
                    "constraints": {
                        "transitive": True,
                        "irreflexive": True,
                        "acyclic": True,
                    },
                    "evidence_refs": [ref],
                    "confidence": 0.98,
                },
            ],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": ref,
                "status": "modeled",
                "reason": "对象、显式逆关系和图公理均已建模",
                "change_keys": [
                    "entity.container",
                    "entity.component",
                    "relation.contains",
                    "relation.belongs_to",
                    "relation.precedes",
                ],
            }],
        }

    def test_inverse_refs_and_axioms_are_normalized_and_applied_atomically(self) -> None:
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            self._raw(),
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )
        self.assertEqual(
            [item for item in payload["unresolved"] if item.get("blocking")],
            [],
        )
        self.assertEqual(
            {item["code"] for item in payload["unresolved"]},
            {"title_fallback_to_primary_key"},
        )
        by_key = {item["key"]: item for item in payload["relations"]}
        self.assertEqual(
            by_key["relation.contains"]["inverse_relation"],
            {"kind": "generated", "key": "relation.belongs_to"},
        )
        self.assertTrue(by_key["relation.precedes"]["constraints"]["transitive"])

        counts = scenario_model_compiler.apply_scenario_model(
            self.db, self.scenario, payload
        )
        self.db.commit()
        self.assertEqual(counts["counts"]["relations_added"], 3)
        persisted = {
            relation.name: relation
            for relation in self.db.query(OntologyRelation).all()
        }
        self.assertEqual(
            persisted["包含"].constraints["inverse_relation_id"],
            persisted["属于"].id,
        )
        self.assertEqual(
            persisted["属于"].constraints["inverse_relation_id"],
            persisted["包含"].id,
        )
        self.assertEqual(
            persisted["先于"].constraints,
            {"transitive": True, "irreflexive": True, "acyclic": True},
        )

    def test_generic_association_is_unconstrained_many_to_many(self) -> None:
        raw = self._raw()
        raw["relations"] = [{
            "key": "relation.associated_with",
            "name": "关联",
            "source_ref": "entity.container",
            "target_ref": "entity.component",
            "relation_type": "ASSOCIATION",
            "constraints": {
                "asymmetric": True,
                "antisymmetric": True,
                "acyclic": True,
            },
            "evidence_refs": ["relation-doc:p0001"],
            "confidence": 0.98,
        }]
        raw["coverage"][0]["change_keys"] = [
            "entity.container",
            "entity.component",
            "relation.associated_with",
        ]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_relation_cardinality", codes)
        self.assertNotIn("invalid_relation_constraint_endpoints", codes)
        relation = next(
            item for item in payload["relations"]
            if item["key"] == "relation.associated_with"
        )
        self.assertEqual(relation["relation_type"], "N:M")
        self.assertTrue(relation["constraints"]["acyclic"])

    def test_invalid_graph_rule_points_to_relation_constraints(self) -> None:
        raw = self._raw()
        ref = "relation-doc:p0001"
        raw["rules"].append({
            "key": "rule.transitive",
            "name": "传递关系规则",
            "entity_ref": "entity.component",
            "condition": {"transitive": True},
            "evidence_refs": [ref],
            "confidence": 0.9,
        })
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )
        codes = [item["code"] for item in payload["unresolved"]]
        self.assertIn("relation_axiom_requires_relation_constraint", codes)
        self.assertNotIn("invalid_rule_condition", codes)

    def test_class_axiom_is_not_misrepresented_as_record_rule(self) -> None:
        raw = self._raw()
        raw["rules"].append({
            "key": "rule.disjoint",
            "name": "互斥类公理",
            "entity_ref": "entity.component",
            "condition": {"type": "disjointWith", "class": "entity.container"},
            "evidence_refs": ["relation-doc:p0001"],
            "confidence": 0.9,
        })
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )
        codes = [item["code"] for item in payload["unresolved"]]
        self.assertIn("unsupported_class_axiom", codes)
        self.assertNotIn("invalid_rule_condition", codes)


if __name__ == "__main__":
    unittest.main()
