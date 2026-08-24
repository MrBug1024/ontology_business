from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from app.services import (
    function_definition_service,
    scenario_model_compiler,
    scenario_model_evaluator,
    workflow_service,
)


class ScenarioModelSchemaNormalizationTests(unittest.TestCase):
    def test_value_field_is_explicit_validated_and_evaluated_fail_closed(self) -> None:
        condition = scenario_model_compiler._normalize_rule_condition({
            "field": "实际金额",
            "op": ">",
            "value_field": "限制金额",
        })
        self.assertEqual(
            condition,
            {"field": "实际金额", "op": ">", "value_field": "限制金额"},
        )
        self.assertEqual(
            scenario_model_compiler._condition_fields(condition),
            {"实际金额", "限制金额"},
        )
        self.assertEqual(
            scenario_model_evaluator._canonical_condition(condition),
            condition,
        )
        self.assertTrue(
            workflow_service.evaluate_condition(
                condition, {"实际金额": 10, "限制金额": 5}
            )
        )
        self.assertFalse(
            workflow_service.evaluate_condition(condition, {"实际金额": 10})
        )
        self.assertFalse(
            workflow_service.evaluate_condition(
                {**condition, "value": 5},
                {"实际金额": 10, "限制金额": 5},
            )
        )
        self.assertTrue(
            workflow_service.evaluate_condition(
                {"field": "实际金额", "op": ">", "value": 5},
                {"实际金额": 10, "限制金额": 5},
            )
        )
        self.assertTrue(
            workflow_service.evaluate_condition(
                {
                    "field": "结束日期",
                    "op": ">=",
                    "value_field": "开始日期",
                },
                {"开始日期": "2026-01-02", "结束日期": "2026-01-03"},
            )
        )
        self.assertFalse(
            workflow_service.evaluate_condition(
                {
                    "field": "结束日期",
                    "op": ">=",
                    "value_field": "开始日期",
                },
                {"开始日期": "2026-01-03", "结束日期": "2026-01-02"},
            )
        )
        self.assertFalse(
            workflow_service.evaluate_condition(
                {"field": "状态", "op": ">=", "value": "开始日期"},
                {"状态": "进行中"},
            )
        )

        for malformed in (
            {"field": "实际金额", "op": ">"},
            {"field": "实际金额", "op": ">", "value_field": ""},
            {
                "field": "实际金额",
                "op": ">",
                "value": 5,
                "value_field": "限制金额",
            },
        ):
            with self.assertRaises(ValueError):
                scenario_model_compiler._normalize_rule_condition(malformed)

    def test_same_entity_field_name_used_as_string_literal_is_blocking(self) -> None:
        source_bundle = scenario_model_compiler.build_source_bundle(
            "规则测试",
            [],
        )
        source_ref = source_bundle["paragraphs"][0]["ref"]
        scenario = SimpleNamespace(
            id="scenario-rule-dsl",
            name="计划校验",
            namespace="schedule",
            entities=[],
            relations=[],
            function_definitions=[],
            actions=[],
            rules=[],
            events=[],
            workflows=[],
            data_mappings=[],
        )
        raw = {
            "schema_version": scenario_model_compiler.SCHEMA_VERSION,
            "entities": [{
                "key": "entity.schedule",
                "name": "计划",
                "properties": [
                    {
                        "name": "计划ID",
                        "data_type": "string",
                        "is_key": True,
                        "is_title": True,
                        "is_required": True,
                    },
                    {"name": "开始日期", "data_type": "date"},
                    {"name": "结束日期", "data_type": "date"},
                ],
                "evidence_refs": [source_ref],
                "confidence": 0.9,
            }],
            "relations": [],
            "functions": [],
            "actions": [],
            "rules": [{
                "key": "rule.schedule_order",
                "name": "计划日期顺序",
                "entity_ref": "entity.schedule",
                "condition": {
                    "field": "结束日期",
                    "op": ">=",
                    "value": "开始日期",
                },
                "evidence_refs": [source_ref],
                "confidence": 0.9,
            }],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": source_ref,
                "status": "modeled",
                "reason": "计划及日期顺序规则",
                "change_keys": ["entity.schedule", "rule.schedule_order"],
            }],
        }

        ambiguous = scenario_model_compiler.normalize_scenario_model(
            None,
            scenario,
            raw,
            source_bundle=source_bundle,
            mapping_catalog=[],
            columns_by_table={},
        )
        self.assertIn(
            "ambiguous_rule_literal_field",
            {item["code"] for item in ambiguous["unresolved"]},
        )

        explicit = copy.deepcopy(raw)
        explicit["rules"][0]["condition"] = {
            "field": "结束日期",
            "op": ">=",
            "value_field": "开始日期",
        }
        normalized = scenario_model_compiler.normalize_scenario_model(
            None,
            scenario,
            explicit,
            source_bundle=source_bundle,
            mapping_catalog=[],
            columns_by_table={},
        )
        self.assertNotIn(
            "ambiguous_rule_literal_field",
            {item["code"] for item in normalized["unresolved"]},
        )
        self.assertEqual(
            normalized["rules"][0]["condition"]["value_field"],
            "开始日期",
        )

        literal = copy.deepcopy(raw)
        literal["rules"][0]["condition"]["value"] = "固定日期"
        literal_normalized = scenario_model_compiler.normalize_scenario_model(
            None,
            scenario,
            literal,
            source_bundle=source_bundle,
            mapping_catalog=[],
            columns_by_table={},
        )
        self.assertNotIn(
            "ambiguous_rule_literal_field",
            {item["code"] for item in literal_normalized["unresolved"]},
        )
        self.assertEqual(
            literal_normalized["rules"][0]["condition"]["value"],
            "固定日期",
        )

    def test_only_exact_current_entity_qualified_fields_are_flattened(self) -> None:
        entity_key = "entity.工资保证金凭证"
        qualifiers = scenario_model_compiler._entity_field_qualifiers_for_ref(
            SimpleNamespace(entities=[]),
            [{"key": entity_key, "name": "工资保证金存储凭证"}],
            {"kind": "generated", "key": entity_key},
        )
        self.assertIn("entity_工资保证金凭证", qualifiers)

        condition = scenario_model_compiler._rewrite_self_qualified_rule_fields(
            {
                "op": "and",
                "conditions": [
                    {
                        "field": "entity_工资保证金凭证.凭证状态",
                        "op": "==",
                        "value": "缺失",
                    },
                    {
                        "field": "entity_工资表.应发总额",
                        "op": ">",
                        "value": 0,
                    },
                    {
                        "field": "entity_工资保证金凭证.派生差额",
                        "op": ">",
                        "value": 0,
                    },
                ],
            },
            qualifiers=qualifiers,
            available_fields={"凭证状态", "存储金额"},
        )

        fields = [item["field"] for item in condition["conditions"]]
        self.assertEqual(
            fields,
            ["凭证状态", "entity_工资表.应发总额", "entity_工资保证金凭证.派生差额"],
        )

    def test_chunk_merge_treats_nonempty_enum_values_as_the_enum_declaration(self) -> None:
        def chunk(ref: str, *, is_enum: bool, enum_values: list[str]) -> dict:
            return {
                "schema_version": scenario_model_compiler.SCHEMA_VERSION,
                "entities": [{
                    "key": "entity.component",
                    "name": "构件",
                    "properties": [{
                        "name": "构件类型",
                        "data_type": "string",
                        "is_enum": is_enum,
                        "enum_values": enum_values,
                    }],
                    "evidence_refs": [ref],
                    "confidence": 0.9,
                }],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "relation_mappings": [],
                "unresolved": [],
                "coverage": [{
                    "source_ref": ref,
                    "status": "modeled",
                    "reason": "构件类型定义",
                    "change_keys": ["entity.component"],
                }],
            }

        merged = scenario_model_compiler._merge_chunk_models([
            chunk("document:p0001", is_enum=False, enum_values=[]),
            chunk(
                "document:p0002",
                is_enum=True,
                enum_values=["安装构件", "场地构件"],
            ),
        ])

        self.assertNotIn(
            "chunk_resource_conflict",
            {item.get("code") for item in merged["unresolved"]},
        )
        property_definition = merged["entities"][0]["properties"][0]
        self.assertTrue(property_definition["is_enum"])
        self.assertEqual(
            property_definition["enum_values"],
            ["安装构件", "场地构件"],
        )

    def test_chunk_merge_canonicalizes_relation_syntax_and_empty_defaults(self) -> None:
        def chunk(
            ref: str,
            *,
            relation_type: str,
            constraints: dict,
            target_ref: str = "entity.component",
        ) -> dict:
            return {
                "schema_version": scenario_model_compiler.SCHEMA_VERSION,
                "entities": [],
                "relations": [{
                    "key": "relation.associated_with",
                    "name": "关联",
                    "source_ref": "entity.container",
                    "target_ref": target_ref,
                    "relation_type": relation_type,
                    "constraints": constraints,
                    "evidence_refs": [ref],
                    "confidence": 0.9,
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
                    "source_ref": ref,
                    "status": "modeled",
                    "reason": "关系定义",
                    "change_keys": ["relation.associated_with"],
                }],
            }

        merged = scenario_model_compiler._merge_chunk_models([
            chunk(
                "document:p0001",
                relation_type="ASSOCIATION",
                constraints={
                    "asymmetric": "false",
                    "acyclic": None,
                    "source_max_cardinality": "N",
                },
            ),
            chunk(
                "document:p0002",
                relation_type="N:M",
                constraints={
                    "asymmetric": True,
                    "target_min_cardinality": "0",
                },
            ),
        ])

        self.assertNotIn(
            "chunk_resource_conflict",
            {item.get("code") for item in merged["unresolved"]},
        )
        relation = merged["relations"][0]
        self.assertEqual(relation["relation_type"], "N:M")
        self.assertEqual(
            relation["constraints"],
            {"asymmetric": True, "irreflexive": True, "target_min_cardinality": 0},
        )

        conflicting = scenario_model_compiler._merge_chunk_models([
            chunk(
                "document:p0001",
                relation_type="N:M",
                constraints={"asymmetric": True, "source_min_cardinality": 1},
            ),
            chunk(
                "document:p0002",
                relation_type="1:N",
                constraints={"symmetric": True, "source_min_cardinality": 2},
                target_ref="entity.other_component",
            ),
        ])
        conflict = next(
            item for item in conflicting["unresolved"]
            if item.get("code") == "chunk_resource_conflict"
        )
        self.assertIn("relation_type", conflict["message"])
        self.assertIn("target_ref", conflict["message"])
        self.assertIn("constraints.source_min_cardinality", conflict["message"])

    def test_common_model_types_become_strict_json_schema_recursively(self) -> None:
        schema = scenario_model_compiler._object_schema({
            "type": "OBJECT",
            "properties": {
                "开工日期": {"type": "date"},
                "更新时间": {"type": "datetime"},
                "合同金额": {"type": "decimal", "minimum": 0},
                "完成比例": {"type": "number"},
                "楼层": {"type": "int"},
                "标签": {"type": "array", "items": {"type": "text"}},
                "扩展": {
                    "anyOf": [
                        {"type": "bool"},
                        {"type": "date-time"},
                    ],
                },
            },
            "additionalProperties": False,
        })

        self.assertEqual(
            schema["properties"]["开工日期"],
            {"type": "string", "format": "date"},
        )
        self.assertEqual(
            schema["properties"]["更新时间"],
            {"type": "string", "format": "date-time"},
        )
        self.assertEqual(schema["properties"]["合同金额"]["type"], "number")
        self.assertEqual(schema["properties"]["完成比例"]["type"], "number")
        self.assertEqual(schema["properties"]["楼层"]["type"], "integer")
        self.assertEqual(schema["properties"]["标签"]["items"]["type"], "string")
        self.assertEqual(
            schema["properties"]["扩展"]["anyOf"][1],
            {"type": "string", "format": "date-time"},
        )

    def test_schema_alias_adapter_does_not_weaken_persisted_contract(self) -> None:
        with self.assertRaisesRegex(
            function_definition_service.FunctionDefinitionError,
            "type 不支持",
        ):
            function_definition_service.normalize_schema(
                {
                    "type": "object",
                    "properties": {"开工日期": {"type": "date"}},
                },
                label="输入 Schema",
            )

    def test_unknown_schema_type_and_conflicting_temporal_format_stay_blocking(self) -> None:
        with self.assertRaisesRegex(Exception, "type 不支持"):
            scenario_model_compiler._object_schema({
                "type": "object",
                "properties": {"造价": {"type": "money"}},
            })
        with self.assertRaisesRegex(ValueError, "类型 date 与 format=date-time 冲突"):
            scenario_model_compiler._object_schema({
                "type": "object",
                "properties": {
                    "开工日期": {"type": "date", "format": "date-time"},
                },
            })

    def test_rule_operator_aliases_are_canonicalized_without_guessing(self) -> None:
        self.assertEqual(
            scenario_model_compiler._normalize_rule_condition({
                "field": "验收日期",
                "operator": "IS NOT NULL",
            }),
            {"field": "验收日期", "op": "is_not_null"},
        )
        self.assertEqual(
            scenario_model_compiler._normalize_rule_condition({
                "field": "状态",
                "operator": "=",
                "value": "已审批",
            }),
            {"field": "状态", "op": "==", "value": "已审批"},
        )
        self.assertEqual(
            scenario_model_compiler._normalize_rule_condition({
                "field": "状态",
                "op": "不属于",
                "value": ["已取消", "已归档"],
            }),
            {"field": "状态", "op": "not_in", "value": ["已取消", "已归档"]},
        )

    def test_missing_unknown_or_conflicting_rule_operator_stays_blocking(self) -> None:
        with self.assertRaisesRegex(ValueError, "空值"):
            scenario_model_compiler._normalize_rule_condition({
                "field": "状态",
                "value": "已审批",
            })
        with self.assertRaisesRegex(ValueError, "不支持的运算符"):
            scenario_model_compiler._normalize_rule_condition({
                "field": "状态",
                "op": "approximately",
                "value": "已审批",
            })
        with self.assertRaisesRegex(ValueError, "互相冲突"):
            scenario_model_compiler._normalize_rule_condition({
                "field": "状态",
                "op": "==",
                "operator": "!=",
                "value": "已审批",
            })

    def test_rule_severity_defaults_and_known_aliases_are_deterministic(self) -> None:
        self.assertEqual(scenario_model_compiler._normalize_rule_severity(None), "info")
        self.assertEqual(scenario_model_compiler._normalize_rule_severity("low"), "info")
        self.assertEqual(scenario_model_compiler._normalize_rule_severity("warn"), "warning")
        self.assertEqual(scenario_model_compiler._normalize_rule_severity("高"), "critical")
        with self.assertRaisesRegex(ValueError, "不支持的严重级别"):
            scenario_model_compiler._normalize_rule_severity("ordinary")


if __name__ == "__main__":
    unittest.main()
