from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import BusinessScenario, OntologyEntity, OntologyProperty, Tenant
from app.services import scenario_model_compiler


def _empty_model() -> dict:
    return {
        "schema_version": scenario_model_compiler.SCHEMA_VERSION,
        **{
            section: []
            for section in scenario_model_compiler._RESOURCE_SECTIONS
        },
        "unresolved": [],
        "coverage": [],
    }


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


class ScenarioModelCrossChunkReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-cross-chunk", name="跨块引用租户")
        self.scenario = BusinessScenario(
            id="scenario-cross-chunk",
            tenant_id=tenant.id,
            name="跨块引用场景",
            namespace="cross_chunk",
            status="draft",
        )
        self.db.add_all([tenant, self.scenario])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _bundle(self) -> dict:
        return {
            "documents": [],
            "paragraphs": [{
                "ref": "synthetic:p0001",
                "source_id": "synthetic",
                "text": "仅用于测试引用合并的合成段落。",
            }],
            "sources": [],
            "total_characters": 0,
        }

    def _cross_resource_model(self) -> dict:
        ref = "synthetic:p0001"
        raw = _empty_model()
        raw["entities"] = [{
            "key": "building",
            "name": "建筑物",
            "properties": [{
                "name": "建筑编号",
                "data_type": "string",
                "is_key": True,
                "is_required": True,
            }, {
                "name": "状态",
                "data_type": "string",
                "is_required": True,
            }],
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        # All four resources intentionally use the same unqualified raw key.
        # Typed canonicalization must make them globally unique.
        raw["actions"] = [{
            "key": "building",
            "name": "更新建筑物",
            "entity_ref": "entity_building",
            "input_schema": _schema(),
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["rules"] = [{
            "key": "building",
            "name": "建筑状态校验",
            "entity_ref": "entities:building",
            "condition": {"field": "状态", "op": "==", "value": "有效"},
            "trigger_action_refs": ["action_building"],
            "severity": "warning",
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["workflows"] = [{
            "key": "building",
            "name": "建筑更新流程",
            "trigger_type": "manual",
            "nodes": [
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {
                    "id": "update",
                    "type": "action",
                    "data": {"label": "更新", "resource_ref": "actions:building"},
                },
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "update"},
                {"id": "e2", "source": "update", "target": "end"},
            ],
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["coverage"] = [{
            "source_ref": ref,
            "status": "modeled",
            "reason": "已形成对象、操作、规则和流程",
            # This raw token is globally ambiguous after typed key repair.
            # Exact evidence links must rebuild the final list safely.
            "change_keys": ["building"],
        }]
        return raw

    def test_typed_aliases_reconcile_refs_and_duplicate_cross_type_keys(self) -> None:
        merged = scenario_model_compiler._merge_chunk_models([
            self._cross_resource_model()
        ])

        self.assertEqual(merged["entities"][0]["key"], "entity.building")
        self.assertEqual(merged["actions"][0]["key"], "action.building")
        self.assertEqual(merged["rules"][0]["key"], "rule.building")
        self.assertEqual(merged["workflows"][0]["key"], "workflow.building")
        self.assertEqual(merged["actions"][0]["entity_ref"], "entity.building")
        self.assertEqual(merged["rules"][0]["entity_ref"], "entity.building")
        self.assertEqual(
            merged["rules"][0]["trigger_action_refs"], ["action.building"]
        )
        self.assertEqual(
            merged["workflows"][0]["nodes"][1]["data"]["resource_ref"],
            "action.building",
        )

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            merged,
            source_bundle=self._bundle(),
        )
        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("missing_reference", codes)
        self.assertNotIn("duplicate_change_key", codes)
        self.assertNotIn("invalid_modeled_coverage", codes)
        self.assertEqual(
            set(payload["coverage"][0]["change_keys"]),
            {
                "entity.building",
                "action.building",
                "rule.building",
                "workflow.building",
            },
        )

    def test_exact_name_collision_remains_an_ambiguity_blocker(self) -> None:
        raw = self._cross_resource_model()
        ref = "synthetic:p0001"
        raw["actions"].append({
            "key": "other_action",
            # Collides exactly with the first Action's canonical key.
            "name": "action.building",
            "entity_ref": "建筑物",
            "input_schema": _schema(),
            "evidence_refs": [ref],
            "confidence": 0.8,
        })
        merged = scenario_model_compiler._merge_chunk_models([raw])
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            merged,
            source_bundle=self._bundle(),
        )

        self.assertIn(
            "ambiguous_reference",
            {item["code"] for item in payload["unresolved"]},
        )

    def test_every_typed_reference_slot_uses_its_own_alias_index(self) -> None:
        ref = "synthetic:p0001"
        raw = _empty_model()
        raw["entities"] = [
            {"key": "building", "name": "建筑物", "evidence_refs": [ref]},
            {
                "key": "entity.building_risk",
                "name": "建筑风险",
                "evidence_refs": [ref],
            },
            {
                "key": "entities:participant",
                "name": "参与方",
                "evidence_refs": [ref],
            },
        ]
        raw["relations"] = [{
            "key": "has_risk",
            "name": "存在风险",
            "source_ref": "entity_building",
            "target_ref": "entity_building_risk",
            "evidence_refs": [ref],
        }]
        raw["actions"] = [{
            "key": "mitigate_risk",
            "name": "处置风险",
            "entity_ref": "建筑风险",
            "evidence_refs": [ref],
        }]
        raw["rules"] = [{
            "key": "building_risk",
            "name": "风险处置规则",
            "entity_ref": "entity_building_risk",
            "trigger_action_refs": ["actions:mitigate_risk"],
            "evidence_refs": [ref],
        }]
        raw["events"] = [{
            "key": "risk_detected",
            "name": "发现风险",
            "evidence_refs": [ref],
        }]
        raw["workflows"] = [{
            "key": "risk_response",
            "name": "风险响应",
            "trigger_config": {"event_ref": "event_risk_detected"},
            "nodes": [
                {
                    "id": "a",
                    "type": "action",
                    "data": {"action_id": "action_mitigate_risk"},
                },
                {
                    "id": "r",
                    "type": "rule",
                    "resource_ref": "rules:building_risk",
                },
                {
                    "id": "e",
                    "type": "event",
                    "data": {"event_id": "events:risk_detected"},
                },
            ],
            "evidence_refs": [ref],
        }]
        raw["mappings"] = [{
            "key": "participant",
            "entity_ref": "entity_participant",
            "evidence_refs": [ref],
        }]

        merged = scenario_model_compiler._merge_chunk_models([raw])

        self.assertEqual(
            (merged["relations"][0]["source_ref"], merged["relations"][0]["target_ref"]),
            ("entity.building", "entity.building_risk"),
        )
        self.assertEqual(merged["actions"][0]["entity_ref"], "entity.building_risk")
        self.assertEqual(merged["rules"][0]["entity_ref"], "entity.building_risk")
        self.assertEqual(
            merged["rules"][0]["trigger_action_refs"], ["action.mitigate_risk"]
        )
        workflow = merged["workflows"][0]
        self.assertEqual(workflow["trigger_config"]["event_ref"], "event.risk_detected")
        self.assertEqual(workflow["nodes"][0]["data"]["action_id"], "action.mitigate_risk")
        self.assertEqual(workflow["nodes"][1]["resource_ref"], "rule.building_risk")
        self.assertEqual(workflow["nodes"][2]["data"]["event_id"], "event.risk_detected")
        self.assertEqual(merged["mappings"][0]["entity_ref"], "entity.participant")

    def test_chunk_refs_are_canonicalized_before_conflict_detection(self) -> None:
        ref = "synthetic:p0001"
        first = _empty_model()
        first["entities"] = [
            {"key": "building", "name": "建筑物", "evidence_refs": [ref]},
            {"key": "building_risk", "name": "建筑风险", "evidence_refs": [ref]},
        ]
        first["relations"] = [{
            "key": "has_risk",
            "name": "存在风险",
            "source_ref": "entity_building",
            "target_ref": "entity_building_risk",
            "relation_type": "1:N",
            "evidence_refs": [ref],
        }]
        second = _empty_model()
        second["relations"] = [{
            "key": "relation.has_risk",
            "name": "存在风险",
            "source_ref": "建筑物",
            "target_ref": "建筑风险",
            "relation_type": "1:N",
            "evidence_refs": [ref],
        }]

        merged = scenario_model_compiler._merge_chunk_models([first, second])

        self.assertNotIn(
            "chunk_resource_conflict",
            {item["code"] for item in merged["unresolved"]},
        )
        self.assertEqual(merged["relations"][0]["source_ref"], "entity.building")
        self.assertEqual(
            merged["relations"][0]["target_ref"], "entity.building_risk"
        )

        # A genuinely different, uniquely identified endpoint is not erased.
        third = _empty_model()
        third["entities"] = [{
            "key": "participant",
            "name": "参与方",
            "evidence_refs": [ref],
        }]
        third["relations"] = [{
            "key": "has_risk",
            "name": "存在风险",
            "source_ref": "建筑物",
            "target_ref": "参与方",
            "relation_type": "1:N",
            "evidence_refs": [ref],
        }]
        conflicted = scenario_model_compiler._merge_chunk_models(
            [first, second, third]
        )
        self.assertIn(
            "chunk_resource_conflict",
            {item["code"] for item in conflicted["unresolved"]},
        )

    def test_equivalent_existing_property_storage_types_are_retained(self) -> None:
        ref = "synthetic:p0001"
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="建筑活动",
            namespace=self.scenario.namespace,
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add_all([
            OntologyProperty(
                entity_id=entity.id,
                name="活动编号",
                data_type="string",
                is_key=True,
                is_required=True,
            ),
            OntologyProperty(entity_id=entity.id, name="活动描述", data_type="text"),
            OntologyProperty(entity_id=entity.id, name="成本", data_type="float"),
            OntologyProperty(entity_id=entity.id, name="开始日期", data_type="date"),
        ])
        self.db.commit()
        raw = _empty_model()
        raw["entities"] = [{
            "key": "activity",
            "name": "建筑活动",
            "properties": [
                {
                    "name": "活动编号",
                    "data_type": "string",
                    "is_key": True,
                    "is_required": True,
                },
                {"name": "活动描述", "data_type": "string"},
                {"name": "成本", "data_type": "number"},
                {
                    "name": "开始日期",
                    "data_type": "string",
                    "constraints": {"format": "date"},
                },
            ],
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["coverage"] = [{
            "source_ref": ref,
            "status": "modeled",
            "reason": "属性定义",
            "change_keys": ["activity"],
        }]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
        )

        self.assertNotIn(
            "existing_property_conflict",
            {item["code"] for item in payload["unresolved"]},
        )
        properties = {
            item["name"]: item for item in payload["entities"][0]["properties"]
        }
        self.assertEqual(properties["活动描述"]["data_type"], "text")
        self.assertEqual(properties["成本"]["data_type"], "float")
        self.assertEqual(properties["开始日期"]["data_type"], "date")
        self.assertEqual(properties["开始日期"]["constraints"], {})

    def test_compact_and_repeated_type_prefixes_are_pure_syntax_aliases(self) -> None:
        self.assertEqual(
            scenario_model_compiler._canonical_generated_key(
                "entities", "entity建筑活动"
            ),
            "entity.建筑活动",
        )
        self.assertEqual(
            scenario_model_compiler._canonical_generated_key(
                "entities", "entity.entity建筑活动"
            ),
            "entity.建筑活动",
        )
        self.assertEqual(
            scenario_model_compiler._canonical_generated_key(
                "entities", "entityBuildingRisk"
            ),
            "entity.BuildingRisk",
        )

        unresolved: list[dict] = []
        resolved = scenario_model_compiler._resolve_ref(
            "entity建筑资源",
            generated=[],
            existing=[SimpleNamespace(id="resource-id", name="建筑资源")],
            resource_label="规则对象类型",
            unresolved=unresolved,
            source_refs=["synthetic:p0001"],
        )
        self.assertEqual(resolved, {"kind": "existing", "id": "resource-id"})
        self.assertFalse(unresolved)

        # If the stripped exact name and the original exact key point to
        # different candidates, the normal ambiguity guard still wins.
        ambiguous: list[dict] = []
        resolved = scenario_model_compiler._resolve_ref(
            "entity建筑资源",
            generated=[{
                "key": "entity建筑资源",
                "name": "另一资源",
            }],
            existing=[SimpleNamespace(id="resource-id", name="建筑资源")],
            resource_label="规则对象类型",
            unresolved=ambiguous,
            source_refs=["synthetic:p0001"],
        )
        self.assertIsNone(resolved)
        self.assertEqual(ambiguous[0]["code"], "ambiguous_reference")

    def test_chunk_merge_collapses_only_equivalent_property_type_aliases(self) -> None:
        ref = "synthetic:p0001"

        def chunk(data_type: str) -> dict:
            raw = _empty_model()
            raw["entities"] = [{
                "key": "risk",
                "name": "风险",
                "properties": [{
                    "name": "发生概率",
                    "data_type": data_type,
                }],
                "evidence_refs": [ref],
            }]
            return raw

        merged = scenario_model_compiler._merge_chunk_models([
            chunk("float"), chunk("number")
        ])
        self.assertNotIn(
            "chunk_resource_conflict",
            {item["code"] for item in merged["unresolved"]},
        )
        self.assertEqual(
            merged["entities"][0]["properties"][0]["data_type"], "float"
        )

        conflicted = scenario_model_compiler._merge_chunk_models([
            chunk("integer"), chunk("number")
        ])
        self.assertIn(
            "chunk_resource_conflict",
            {item["code"] for item in conflicted["unresolved"]},
        )

    def test_repeated_chunk_conflicts_are_coalesced_per_resource(self) -> None:
        ref = "synthetic:p0001"

        def chunk(data_type: str, required: bool) -> dict:
            raw = _empty_model()
            raw["entities"] = [{
                "key": "risk",
                "name": "风险",
                "properties": [{
                    "name": "发生概率",
                    "data_type": data_type,
                    "is_required": required,
                }],
                "evidence_refs": [ref],
            }]
            return raw

        merged = scenario_model_compiler._merge_chunk_models([
            chunk("number", False),
            chunk("integer", False),
            chunk("number", True),
        ])

        issues = [
            item for item in merged["unresolved"]
            if item["code"] == "chunk_resource_conflict"
        ]
        self.assertEqual(len(issues), 1)
        self.assertIn("properties[name=发生概率].data_type", issues[0]["message"])
        self.assertIn("properties[name=发生概率].is_required", issues[0]["message"])


if __name__ == "__main__":
    unittest.main()
