from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import database
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


class OntologyApiNameMigrationTests(unittest.TestCase):
    def test_legacy_schema_is_backfilled_idempotently(self) -> None:
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
                conn.exec_driver_sql(
                    "CREATE TABLE ontology_properties "
                    "(id VARCHAR(32) PRIMARY KEY, entity_id VARCHAR(32), name VARCHAR(200))"
                )
                conn.exec_driver_sql(
                    "CREATE TABLE ontology_relations "
                    "(id VARCHAR(32) PRIMARY KEY, scenario_id VARCHAR(32), name VARCHAR(200))"
                )
                conn.exec_driver_sql(
                    "CREATE TABLE relation_data_mappings "
                    "(relation_id VARCHAR(32), mode VARCHAR(20))"
                )
                conn.execute(
                    text("INSERT INTO ontology_entities VALUES (:id, :scenario, :name)"),
                    {"id": "entity-hospital", "scenario": "scenario-1", "name": "医院"},
                )
                conn.execute(
                    text("INSERT INTO ontology_properties VALUES (:id, :entity, :name)"),
                    {"id": "property-name", "entity": "entity-hospital", "name": "医院名称"},
                )
                conn.execute(
                    text("INSERT INTO ontology_relations VALUES (:id, :scenario, :name)"),
                    {"id": "relation-visits", "scenario": "scenario-1", "name": "接诊"},
                )
                conn.execute(
                    text("INSERT INTO relation_data_mappings VALUES (:relation, :mode)"),
                    {"relation": "relation-visits", "mode": "join_table"},
                )

            settings = SimpleNamespace(database_url="sqlite://")
            with patch.object(database, "engine", engine), patch.object(
                database, "_settings", settings
            ):
                database._migrate_ontology_api_names()
                with engine.connect() as conn:
                    first = conn.execute(text(
                        "SELECT api_name, source_display_name, source_api_name, "
                        "target_display_name, target_api_name, storage_kind "
                        "FROM ontology_relations"
                    )).mappings().one()
                database._migrate_ontology_api_names()
                with engine.connect() as conn:
                    second = conn.execute(text(
                        "SELECT api_name, source_display_name, source_api_name, "
                        "target_display_name, target_api_name, storage_kind "
                        "FROM ontology_relations"
                    )).mappings().one()

            self.assertEqual(dict(first), dict(second))
            self.assertEqual(first["source_display_name"], "接诊")
            self.assertEqual(first["target_display_name"], "接诊（反向）")
            self.assertEqual(first["source_api_name"], first["api_name"])
            self.assertEqual(first["target_api_name"], f"inverse_{first['api_name']}")
            self.assertEqual(first["storage_kind"], "join_table")
            columns = {
                column["name"] for column in inspect(engine).get_columns("ontology_relations")
            }
            self.assertTrue({
                "api_name", "source_display_name", "source_api_name",
                "target_display_name", "target_api_name", "storage_kind",
            }.issubset(columns))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
