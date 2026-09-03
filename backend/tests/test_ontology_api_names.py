from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    Tenant,
    User,
)
from app.routers import scenarios as scenario_routes
from app.schemas import EntityIn, PropertyIn, RelationIn
from app.services import permission_service, scenario_model_compiler
from tests.postgresql_migration_contracts import baseline_table_ddl


class OntologyApiNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-api-name", name="稳定标识租户")
        user = User(
            id="user-api-name",
            tenant_id=tenant.id,
            email="api-name@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-api-name",
            tenant_id=tenant.id,
            name="医保审计",
            namespace="medical.audit",
            status="draft",
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

    @staticmethod
    def _entity_payload(name: str, *, api_name: str = "") -> EntityIn:
        return EntityIn(
            name=name,
            api_name=api_name,
            properties=[
                PropertyIn(
                    name=f"{name}编码",
                    api_name="business_code",
                    is_key=True,
                    is_title=True,
                    is_required=True,
                )
            ],
        )

    def test_display_rename_preserves_entity_property_and_relation_api_names(self) -> None:
        hospital = scenario_routes.create_entity(
            self.scenario.id,
            self._entity_payload("医院", api_name="hospital"),
            self.db,
        )
        patient = scenario_routes.create_entity(
            self.scenario.id,
            self._entity_payload("患者", api_name="patient"),
            self.db,
        )
        original_property_api_name = hospital.properties[0].api_name

        renamed = scenario_routes.update_entity(
            hospital.id,
            EntityIn(
                name="医疗机构",
                # An old client omits api_name; the server must retain it.
                properties=[
                    PropertyIn(
                        name="医疗机构编码",
                        api_name=original_property_api_name,
                        is_key=True,
                        is_title=True,
                        is_required=True,
                    )
                ],
            ),
            self.db,
        )
        self.assertEqual(renamed.api_name, "hospital")
        self.assertEqual(renamed.properties[0].api_name, original_property_api_name)

        relation = scenario_routes.create_relation(
            self.scenario.id,
            RelationIn(
                name="接诊",
                api_name="treats",
                source_entity_id=renamed.id,
                target_entity_id=patient.id,
            ),
            self.db,
        )
        self.assertEqual(relation.source_display_name, "接诊")
        self.assertEqual(relation.source_api_name, "treats")
        self.assertEqual(relation.target_display_name, "接诊（反向）")
        self.assertEqual(relation.target_api_name, "inverse_treats")
        self.assertEqual(relation.storage_kind, "none")

        updated_relation = scenario_routes.update_relation(
            relation.id,
            RelationIn(
                name="提供诊疗",
                source_entity_id=renamed.id,
                target_entity_id=patient.id,
            ),
            self.db,
        )
        self.assertEqual(updated_relation.api_name, "treats")
        self.assertEqual(updated_relation.source_api_name, "treats")
        self.assertEqual(updated_relation.target_api_name, "inverse_treats")

    def test_compiler_persists_semantic_keys_and_bidirectional_defaults(self) -> None:
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件",
            [{
                "id": "ontology-api",
                "filename": "医保模型.md",
                "text": "医院以医院编码唯一标识，医院之间可建立协作关系。",
            }],
        )
        source_ref = "ontology-api:p0001"
        raw = {
            "schema_version": "scenario_model.v1",
            "entities": [{
                "key": "entity.hospital",
                "name": "医院",
                "properties": [{
                    "name": "医院编码",
                    "data_type": "string",
                    "is_key": True,
                    "is_title": True,
                    "is_required": True,
                }],
                "evidence_refs": [source_ref],
                "confidence": 0.99,
            }],
            "relations": [{
                "key": "relation.hospital_partners",
                "name": "协作",
                "source_ref": "entity.hospital",
                "target_ref": "entity.hospital",
                "relation_type": "N:M",
                "evidence_refs": [source_ref],
                "confidence": 0.95,
            }],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": source_ref,
                "status": "modeled",
                "reason": "已建模医院与协作关系",
                "change_keys": ["entity.hospital", "relation.hospital_partners"],
            }],
        }
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=source_bundle,
        )
        self.assertFalse(payload["unresolved"])
        self.assertEqual(payload["entities"][0]["api_name"], "entity_hospital")
        self.assertTrue(payload["entities"][0]["properties"][0]["api_name"])
        self.assertEqual(
            payload["relations"][0]["target_api_name"],
            "inverse_relation_hospital_partners",
        )

        scenario_model_compiler.apply_scenario_model(
            self.db, self.scenario, payload
        )
        entity = self.db.scalars(select(OntologyEntity)).one()
        relation = self.db.scalars(select(OntologyRelation)).one()
        self.assertEqual(entity.api_name, "entity_hospital")
        self.assertEqual(relation.api_name, "relation_hospital_partners")
        self.assertEqual(relation.source_display_name, "协作")
        self.assertEqual(relation.target_display_name, "协作（反向）")
        self.assertEqual(relation.storage_kind, "none")


class OntologyApiNameMigrationContractsTests(unittest.TestCase):
    def test_postgresql_baseline_declares_stable_ontology_identifiers(self) -> None:
        entity_ddl = baseline_table_ddl("ontology_entities")
        property_ddl = baseline_table_ddl("ontology_properties")
        relation_ddl = baseline_table_ddl("ontology_relations")

        self.assertIn("api_name VARCHAR(100) NOT NULL", entity_ddl)
        self.assertIn("api_name VARCHAR(100) NOT NULL", property_ddl)
        self.assertIn("api_name VARCHAR(100) NOT NULL", relation_ddl)
        self.assertIn("source_display_name VARCHAR(200) NOT NULL", relation_ddl)
        self.assertIn("source_api_name VARCHAR(100) NOT NULL", relation_ddl)
        self.assertIn("target_display_name VARCHAR(200) NOT NULL", relation_ddl)
        self.assertIn("target_api_name VARCHAR(100) NOT NULL", relation_ddl)
        self.assertIn("storage_kind VARCHAR(32) NOT NULL", relation_ddl)


if __name__ == "__main__":
    unittest.main()
