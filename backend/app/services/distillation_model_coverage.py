"""Check explicit source-property coverage without guessing business aliases."""
from __future__ import annotations

import json
import copy
from collections import Counter
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EntitySourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: str = Field(min_length=1, max_length=300)
    source_key: str = Field(min_length=1, max_length=200)
    attribute_map: dict[Annotated[str, Field(min_length=1, max_length=200)],
                        Annotated[str, Field(min_length=1, max_length=200)]] = Field(max_length=512)


def align_binding_fragments(target: dict, incoming: dict) -> None:
    """Merge compatible provenance before the compiler checks fragment conflicts."""
    if not isinstance(target.get("source_entity_bindings"), list) or not isinstance(incoming.get("source_entity_bindings"), list):
        return
    if not target["source_entity_bindings"] or not incoming["source_entity_bindings"]:
        return
    merged = {}
    for value in [*target["source_entity_bindings"], *incoming["source_entity_bindings"]]:
        try:
            binding = EntitySourceBinding.model_validate(value)
        except ValidationError:
            return
        identity = (binding.source_ref, binding.source_key)
        current = merged.setdefault(identity, binding.model_dump())
        if any(key in current["attribute_map"] and current["attribute_map"][key] != value
               for key, value in binding.attribute_map.items()):
            return  # The ordinary compiler conflict remains authoritative.
        current["attribute_map"].update(binding.attribute_map)
    target["source_entity_bindings"] = list(merged.values())
    incoming["source_entity_bindings"] = copy.deepcopy(target["source_entity_bindings"])


def _names(values: set[str]) -> str:
    names = sorted(values)
    return "、".join(names[:20]) + (f"（共 {len(names)} 项）" if len(names) > 20 else "")


def source_entities(paragraphs: list[dict]) -> dict[tuple[str, str], list[str]]:
    result = {}
    fragments: dict[tuple[str, int], dict] = {}
    for paragraph in paragraphs:
        if paragraph.get("structured_handoff") is not True:
            continue
        unit = json.loads(paragraph["text"])
        path, value = unit.get("source_path"), unit.get("value")
        if isinstance(path, list) and len(path) >= 3 and path[0] == "entities" and isinstance(path[1], int):
            identity = (paragraph["source_id"], path[1])
            fragment = fragments.setdefault(identity, {"refs": [], "attributes": {}})
            fragment["refs"].append(paragraph["ref"])
            if path[2:] == ["key"]:
                fragment["key"] = value
            elif path[2:] == ["attributes"] and isinstance(value, list):
                fragment["attributes"].update(enumerate(value))
            elif len(path) == 4 and path[2] == "attributes" and isinstance(path[3], int):
                fragment["attributes"][path[3]] = value
            continue
        records = value if path == ["entities"] and isinstance(value, list) else (
            [value] if isinstance(path, list) and len(path) == 2 and path[0] == "entities" else [])
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("attributes"), list):
                continue
            key = record.get("key")
            attributes = record["attributes"]
            if isinstance(key, str) and all(isinstance(item, str) for item in attributes):
                result[(paragraph["ref"], key)] = attributes
    for fragment in fragments.values():
        key = fragment.get("key")
        attributes = [value for _, value in sorted(fragment["attributes"].items())]
        if isinstance(key, str) and all(isinstance(item, str) for item in attributes):
            for ref in fragment["refs"]:
                result[(ref, key)] = attributes
    return result


def validate_entity_coverage(raw: dict, entities: list[dict], source_bundle: dict,
                             existing_properties: dict[str, set[str]]) -> list[dict]:
    expected = source_entities(source_bundle.get("paragraphs", []))
    source_refs = {ref for ref, _ in expected}
    by_key = {item["key"]: item for item in entities}
    issues = []
    for candidate in raw.get("entities", []):
        if not isinstance(candidate, dict) or candidate.get("key") not in by_key:
            continue
        entity = by_key[candidate["key"]]
        refs = set(entity.get("evidence_refs", [])) & source_refs
        if not refs:
            continue
        messages = []
        declared = candidate.get("source_entity_bindings")
        if not isinstance(declared, list) or not 1 <= len(declared) <= 32:
            messages.append("缺少来源对象及逐项属性对应 source_entity_bindings")
            declared = []
        properties = {p["name"] for p in entity["properties"]}
        properties.update(existing_properties.get(entity["name"], set()))
        seen = set()
        for value in declared:
            try:
                binding = EntitySourceBinding.model_validate(value)
            except ValidationError:
                messages.append("来源属性对应格式无效")
                continue
            identity = (binding.source_ref, binding.source_key)
            if identity not in expected or binding.source_ref not in refs or identity in seen:
                messages.append("来源对象不存在、未被该对象引用或被重复声明")
                continue
            seen.add(identity)
            mapping = binding.attribute_map
            missing = set(expected[identity]) - set(mapping)
            extra = set(mapping) - set(expected[identity])
            unknown = set(mapping.values()) - properties
            duplicate = [name for name, count in Counter(mapping.values()).items() if count > 1]
            if missing:
                messages.append("遗漏来源属性：" + _names(missing))
            if extra or unknown or duplicate:
                messages.append("属性对应包含未知来源、缺少目标属性或将不同属性合并："
                                + _names(extra | unknown | set(duplicate)))
        if messages:
            issues.append({"code": "incomplete_source_attributes", "blocking": True,
                "message": (f"对象类型“{entity['name']}”未完整覆盖交接属性：" + "；".join(messages))[:2000],
                "source_refs": sorted(refs), "affected_change_keys": [entity["key"]],
                "resolution_hint": "逐项对应来源属性，补齐有依据的定义；不要删除字段或映射到不存在的属性。"})
    return issues


COVERAGE_GUIDANCE = """
结构化交接中 source_path=[\"entities\"] 的 value 是完整来源对象清单；大清单可按对象索引及字段拆段，source_path 保留所属对象。
候选实体引用这种段落时，必须额外声明 source_entity_bindings:
[{source_ref:外层段落ref,source_key:该段落内来源对象的key,attribute_map:{来源属性原文:候选属性name}}]。
attribute_map 必须逐项覆盖该来源对象 attributes 的全部内容（键保留括号说明等原文），
每个值必须是本候选或已有对象的真实属性名，不同来源属性不得全部映射到同一属性。
复用已有对象只保留既有属性并不完整；来源新增属性也必须建模。来源对象不属于本体阶段时不生成假实体，保留为后续阶段 context。
"""
