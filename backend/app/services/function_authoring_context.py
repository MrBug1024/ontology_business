"""Expose trusted runtime contracts to candidate authors without executing them."""
from __future__ import annotations

import json

from . import catalog_service, permission_service, provider_definition_service


MAX_AUTHORING_MAPPINGS = 50


def available_mapping_context(db, scenario) -> list[dict]:
    """Expose semantic identities through the existing scenario/field ACL boundary."""
    if db is None or scenario is None:
        return []
    mappings = [item for item in catalog_service.list_semantic_mappings(db, scenario.id)
                if item.status == "active"]
    if len(mappings) > MAX_AUTHORING_MAPPINGS:
        raise ValueError("当前场景的激活语义映射超过单次函数建设上限，请缩小业务域")
    entities = {entity.id: entity for entity in scenario.entities}
    result = []
    for mapping in mappings:
        entity = entities.get(mapping.entity_id)
        if entity is None:
            continue
        mapped_ids = {field.ontology_property_id for field in mapping.field_mappings}
        properties = [{"id": prop.id, "name": prop.name, "api_name": prop.api_name,
                       "data_type": prop.data_type}
                      for prop in entity.properties if prop.id in mapped_ids
                      and permission_service.can_read_property(db, prop)]
        if properties:
            result.append({"id": mapping.id, "mapping_key": mapping.mapping_key,
                "entity_id": entity.id, "entity_name": entity.name,
                "entity_api_name": entity.api_name, "properties": properties})
    return result


def compiler_runtime_context(db=None, scenario=None) -> str:
    manifests = provider_definition_service.list_function_provider_manifests()
    return (
        "\n【受信函数运行契约】\n"
        "functions 可声明 runtime_kind/runtime_config，禁止生成代码、SQL、URL 或导入路径。"
        "仅当资料支持计算含义及所有配置值时绑定运行实现；不支持时保留 contract 并明确缺少的实现。"
        "输入输出 Schema 必须与执行器契约一致，不得把只有契约的函数描述为已可执行。\n"
        "threshold: config={field,threshold,operator}；field 是数值输入字段，输出 {matched:boolean,value:number,threshold:number}。\n"
        "weighted_score: config={weights:{输入字段:数字权重},bias:数字}；输出 {score:number}。\n"
        "timeseries_aggregate: config={aggregation:sum|avg|min|max|count}；输入 {values:数字数组}，"
        "输出 {aggregation:string,value:number,count:integer}。\n"
        "geo_distance: config={unit:km|m}；输入 origin/target 为 [经度,纬度]，输出 {distance:number,unit:string}。\n"
        "provider: runtime_config 必须精确包含 provider_key、provider_version、provider_config。"
        "只能选择下列静态清单，使用其封闭 config_schema。固定输入输出 Schema 请声明"
        " schema_source=provider_manifest 并省略 input_schema/output_schema，服务端按精确版本物化，"
        "不要把省略写为 null。示例：{schema_source:provider_manifest,runtime_kind:provider,runtime_config:{"
        "provider_key:精确标识,provider_version:精确版本,provider_config:配置对象}}。"
        "若显式提供 Schema，必须与清单完全一致，否则拒绝。"
        "配置引用的语义映射必须已在授权场景中存在，缺少时报告依赖，不得臆造 ID 或绑定物理数据。\n"
        + json.dumps(manifests, ensure_ascii=False, separators=(",", ":"))
        + "\n当前场景已授权的激活语义映射（只包含定义，不包含本次运行数据）：\n"
        + json.dumps(available_mapping_context(db, scenario), ensure_ascii=False, separators=(",", ":"))
    )
