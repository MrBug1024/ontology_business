"""Read explicit structured handoff requirements without guessing label semantics."""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ..distillation_construction_schemas import PropertyRequirement


def _indexed_values(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and all(str(k).isdigit() for k in value):
        indexes = sorted(int(k) for k in value)
        if indexes == list(range(len(indexes))):
            return [value[str(index)] for index in indexes]
    raise ValueError("交接的结构化属性要求无效，请修正原交接后重试")


def _property_contracts(value: Any) -> list[dict]:
    contracts = []
    for item in _indexed_values(value):
        if not isinstance(item, dict):
            raise ValueError("交接的结构化属性要求无效，请修正原交接后重试")
        item = dict(item)
        if "enum_values" in item:
            item["enum_values"] = _indexed_values(item["enum_values"])
        try:
            contracts.append(PropertyRequirement.model_validate(item).model_dump())
        except ValidationError:
            # Never echo arbitrary uploaded values or Pydantic's raw input.
            raise ValueError("交接的结构化属性要求无效，请修正原交接后重试") from None
    return contracts

def _place(root: dict, path: list, value: Any) -> None:
    current = root
    for part in path[:-1]:
        current = current.setdefault(str(part), {})
    current[str(path[-1])] = value


def structured_items(paragraphs: list[dict], section: str) -> dict[str, dict]:
    documents: dict[str, dict] = {}
    refs: dict[str, list[str]] = {}
    for paragraph in paragraphs:
        if paragraph.get("structured_handoff") is not True:
            continue
        unit = json.loads(paragraph["text"])
        path, value = unit.get("source_path"), unit.get("value")
        if not isinstance(path, list) or not path or path[0] != section:
            continue
        source_id = str(paragraph.get("source_id") or paragraph["ref"])
        document = documents.setdefault(source_id, {})
        refs.setdefault(source_id, []).append(paragraph["ref"])
        if path == [section] and isinstance(value, list):
            for index, entity in enumerate(value):
                document[str(index)] = entity
        elif len(path) == 2 and isinstance(value, dict):
            document[str(path[1])] = value
        elif len(path) > 2:
            _place(document, path[1:], value)
    return {source_id: {"items": list(document.values()), "source_refs": refs[source_id]}
            for source_id, document in documents.items()}


def entity_requirements(paragraphs: list[dict]) -> dict[tuple[str, str], dict]:
    result = {}
    for source_id, document in structured_items(paragraphs, "entities").items():
        for entity in document["items"]:
            if not isinstance(entity, dict) or not isinstance(entity.get("key"), str):
                continue
            contracts = _property_contracts(entity.get("property_contracts", []))
            attributes = _indexed_values(entity.get("attributes", []))
            result[(source_id, entity["key"])] = {
                **entity, "attributes": attributes,
                "source_refs": document["source_refs"], "property_contracts": contracts,
            }
    return result


def _property_mismatches(requirement: dict, property_value: dict) -> list[str]:
    mismatches = []
    if property_value.get("data_type") != requirement.get("data_type"):
        mismatches.append("类型")
    if bool(property_value.get("is_required")) != requirement.get("is_required"):
        mismatches.append("必填")
    if bool(property_value.get("is_key")) != requirement.get("is_key", False):
        mismatches.append("身份")
    if any(property_value.get("constraints", {}).get(k) != v
           for k, v in requirement.get("constraints", {}).items()):
        mismatches.append("约束")
    if requirement.get("enum_values") and set(property_value.get("enum_values", [])) != set(requirement["enum_values"]):
        mismatches.append("枚举")
    return mismatches


def validate_requirements(raw: dict, entities: list[dict], source: dict, *, task_scope: str = "", existing_properties: dict | None = None) -> list[dict]:
    if task_scope not in {"", "ontology"}:
        return []
    expected = entity_requirements(source.get("paragraphs", []))
    issues = []
    by_key = {entity["key"]: entity for entity in entities}
    covered = set()
    for candidate in raw.get("entities", []):
        entity = by_key.get(candidate.get("key"))
        if entity is None:
            continue
        properties = dict((existing_properties or {}).get(entity.get("existing_id"), {}))
        properties.update({p["name"]: p for p in entity["properties"]})
        entity["_construction_requirements"] = []
        for binding in candidate.get("source_entity_bindings") or []:
            if not isinstance(binding, dict):
                continue
            for identity, requirement in expected.items():
                if binding.get("source_key") != identity[1] or binding.get("source_ref") not in requirement["source_refs"]:
                    continue
                covered.add(identity)
                mapping = binding.get("attribute_map") or {}
                for prop in requirement["property_contracts"]:
                    if not isinstance(prop, dict) or prop.get("attribute") not in mapping:
                        continue  # Existing coverage validator handles missing maps.
                    actual = properties.get(mapping[prop["attribute"]], {})
                    entity["_construction_requirements"].append({**prop, "property_name": mapping[prop["attribute"]]})
                    mismatch = _property_mismatches(prop, actual)
                    if mismatch:
                        issues.append({"code": "source_property_contract_mismatch", "blocking": True,
                            "message": f"对象“{entity['name']}”属性“{prop['attribute']}”未落实交接要求：{'、'.join(mismatch)}",
                            "source_refs": [binding["source_ref"]], "affected_change_keys": [entity["key"]],
                            "resolution_hint": "按来源的结构化属性要求修正，不要让专家重填已经提供的事实。"})
    for identity, requirement in expected.items():
        if identity in covered:
            continue
        # Legacy entities with no attributes were exploratory concepts. Explicit
        # property contracts establish a mandatory construction requirement.
        if not requirement["property_contracts"] and not requirement.get("attributes"):
            continue
        issues.append({"code": "missing_source_entity", "blocking": True,
            "message": f"建设清单中的对象“{requirement.get('name') or identity[1]}”尚未完整建模",
            "source_refs": requirement["source_refs"],
            "affected_change_keys": [f"source-entity:{identity[0]}:{identity[1]}"],
            "resolution_hint": "生成完整定义并绑定来源；若原概念不成立，明确提出重新规划及业务要求差异。"})
    return issues


def preserved_requirement_issues(requirements: list, properties: list) -> list[dict]:
    actual = {p.get("name"): p for p in properties if isinstance(p, dict)}
    return [{"code": "source_property_contract_mismatch", "blocking": True,
        "message": f"属性“{requirement['attribute']}”仍未满足原交接要求：{'、'.join(mismatch)}",
        "resolution_hint": "按已保存的交接要求修正；改变业务要求请修订蒸馏交接并重新生成。"}
        for requirement in requirements
        if (mismatch := _property_mismatches(requirement, actual.get(requirement["property_name"], {})))]


GUIDANCE = """
结构化交接实体的 property_contracts 是必须落实的建设要求，不是背景说明。
source_entity_bindings 的 attribute_map 逐项关联 attribute 原文与候选属性名；类型、必填、身份、约束和枚举必须一致。
覆盖整个对象清单；缺失对象不能通过标记段落 context 消除。原对象不成立时提出具体依据与重新规划，不伪造字段。
结构化 relations 的 source、target、cardinality 必须落实；1:N 表示源对象可关联多个目标，每个目标最多关联一个源。
平台 source_max_cardinality 限制每个源的目标数量；target_max_cardinality 限制每个目标的源数量。不要颠倒。
"""
