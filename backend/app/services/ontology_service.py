"""本体服务：图谱构建（schema / instance 两种模式）+ AI 生成本体。"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyInstance,
    OntologyRelation,
    RelationInstance,
)
from . import datasource_service, llm_service, tenant_service


# ──────────────────────────────────────────────
# 图谱构建
# ──────────────────────────────────────────────
def build_graph(scenario: BusinessScenario, mode: str = "schema") -> dict[str, Any]:
    """构建图谱数据。

    mode=schema:   节点=实体类型，边=关系类型（本体层）
    mode=instance: 节点=实例，边=关系实例（数据层，按实体着色）
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    if mode == "instance":
        inst_map = {i.id: i for i in scenario.instances}
        ent_map = {e.id: e for e in scenario.entities}
        for i in scenario.instances:
            ent = ent_map.get(i.entity_id)
            nodes.append(
                {
                    "id": i.id,
                    "kind": "instance",
                    "label": i.name,
                    "entity_id": i.entity_id,
                    "entity_name": ent.name if ent else "",
                    "color": (ent.color if ent else "#64748b"),
                    "size": 14 + min(10, len(i.attributes or {}) * 2),
                    "attrs": i.attributes or {},
                }
            )
        rel_map = {r.id: r for r in scenario.relations}
        for ri in scenario.relation_instances:
            if ri.source_instance_id not in inst_map or ri.target_instance_id not in inst_map:
                continue
            rel = rel_map.get(ri.relation_id)
            edges.append(
                {
                    "id": ri.id,
                    "source": ri.source_instance_id,
                    "target": ri.target_instance_id,
                    "label": rel.name if rel else "",
                    "relation_type": rel.relation_type if rel else "",
                }
            )
    else:
        for e in scenario.entities:
            prop_count = len(e.properties)
            nodes.append(
                {
                    "id": e.id,
                    "kind": "entity",
                    "label": e.name,
                    "color": e.color or "#6366f1",
                    "abstract": bool(e.is_abstract),
                    "size": 26 + min(26, prop_count * 3),
                    "props": [
                        {"name": p.name, "type": p.data_type, "key": bool(p.is_key)}
                        for p in e.properties
                    ][:12],
                    "description": e.description or "",
                }
            )
        for r in scenario.relations:
            edges.append(
                {
                    "id": r.id,
                    "source": r.source_entity_id,
                    "target": r.target_entity_id,
                    "label": r.name,
                    "relation_type": r.relation_type,
                }
            )

    return {"nodes": nodes, "edges": edges}


def search_instances(
    db: Session,
    scenario: BusinessScenario | None,
    entity_name: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按通用本体语义检索实例，不依赖任何行业字段或表名。"""
    if not scenario:
        return []
    entity_name = (entity_name or "").strip().lower()
    query = (query or "").strip().lower()
    entities = {e.id: e for e in scenario.entities}
    allowed_ids = {
        eid for eid, entity in entities.items()
        if not entity_name or entity_name in entity.name.lower()
    }
    if entity_name and not allowed_ids:
        return []
    rows = db.execute(
        select(OntologyInstance)
        .where(
            OntologyInstance.scenario_id == scenario.id,
            OntologyInstance.entity_id.in_(allowed_ids) if allowed_ids else False,
        )
        .order_by(OntologyInstance.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    results: list[dict[str, Any]] = []
    for instance in rows:
        attrs = instance.attributes or {}
        haystack = f"{instance.name} {json.dumps(attrs, ensure_ascii=False, default=str)}".lower()
        if query and query not in haystack:
            continue
        entity = entities.get(instance.entity_id)
        results.append(
            {
                "id": instance.id,
                "name": instance.name,
                "entity": entity.name if entity else "",
                "entity_id": instance.entity_id,
                "attributes": attrs,
                "source": instance.source,
                "source_ref": instance.source_ref,
            }
        )
        if len(results) >= max(1, min(int(limit), 200)):
            break
    return results


# ──────────────────────────────────────────────
# AI 生成本体
# ──────────────────────────────────────────────
_GEN_PROMPT = """你是资深业务架构师，擅长为任意行业构建本体（Ontology）模型。
请根据下面的业务描述，设计一套简洁、通用、可扩展的本体模型。

要求：
1. 实体（entities）：3~8 个核心业务对象，命名使用业务领域中的稳定名词。
2. 每个实体给出 3~8 个属性（properties），属性名用中文，data_type 只能是：string / integer / float / boolean / date / datetime / json / text。
3. 每个实体必须恰好有 1 个 is_key=true 的主键属性。
4. 关系（relations）：2~8 条，relation_type 只能是 1:1 / 1:N / N:M。
5. 只输出 JSON，不要输出任何解释文字。

输出格式（严格 JSON）：
{
  "entities": [
    {"name": "业务对象", "description": "业务领域中的核心对象", "is_abstract": false,
     "properties": [{"name": "对象ID", "data_type": "string", "is_key": true, "is_required": true}, ...]}
  ],
  "relations": [
    {"name": "关联", "source": "业务对象", "target": "相关对象", "relation_type": "1:N", "description": ""}
  ]
}

业务描述：
{description}
"""


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容忍 ```json 包裹、前后杂文与尾随逗号）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 修复常见 LLM JSON 瑕疵：尾随逗号（,} 或 ,]）
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(repaired)


def generate_ontology(db: Session, scenario: BusinessScenario, description: str) -> dict[str, Any]:
    """调用 LLM 生成本体草稿（不落库），返回 {entities, relations}。"""
    from ..models import LLMConfig

    llm = tenant_service.get_visible(db, LLMConfig, scenario.llm_config_id) if getattr(scenario, "llm_config_id", None) and db.info.get("tenant_id") else None
    if not llm:
        llm_stmt = select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        if db.info.get("tenant_id"):
            llm_stmt = llm_stmt.where(tenant_service.visible_clause(LLMConfig, db))
        llm = db.execute(llm_stmt.limit(1)).scalars().first()
    if not llm:
        raise ValueError("请先在「LLM 配置」中配置并启用一个默认模型")

    # 注意：_GEN_PROMPT 内含 JSON 示例花括号，不能用 str.format（会触发 KeyError），
    # 用 replace 注入业务描述。LLM 输出可能不稳定，最多重试 3 次。
    last_err: Exception | None = None
    data: dict[str, Any] = {}
    for _ in range(3):
        resp = llm_service.chat(
            llm,
            [
                {"role": "system", "content": "你只输出 JSON。"},
                {"role": "user", "content": _GEN_PROMPT.replace("{description}", description[:3000])},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        try:
            data = _extract_json(resp.get("content", ""))
            if data.get("entities"):
                break
            last_err = ValueError("AI 未返回有效实体")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    else:
        raise ValueError(f"AI 多次生成均失败: {last_err}")
    entities = data.get("entities") or []
    relations = data.get("relations") or []
    if not entities:
        raise ValueError("AI 未返回有效实体，请补充业务描述后重试")

    # 规范化
    name_to_idx: dict[str, int] = {}
    for i, e in enumerate(entities):
        e["name"] = str(e.get("name", "")).strip()
        e["description"] = str(e.get("description", ""))
        e["is_abstract"] = bool(e.get("is_abstract", False))
        props = []
        has_key = False
        for p in e.get("properties") or []:
            p = {
                "name": str(p.get("name", "")).strip(),
                "data_type": str(p.get("data_type", "string")),
                "description": str(p.get("description", "")),
                "is_key": bool(p.get("is_key", False)),
                "is_required": bool(p.get("is_required", False)),
            }
            if not p["name"]:
                continue
            if p["is_key"] and not has_key:
                has_key = True
            else:
                p["is_key"] = False
            props.append(p)
        if not has_key and props:
            props[0]["is_key"] = True
        e["properties"] = props
        name_to_idx[e["name"]] = i

    clean_rels = []
    for r in relations:
        src, tgt = str(r.get("source", "")).strip(), str(r.get("target", "")).strip()
        if src in name_to_idx and tgt in name_to_idx:
            clean_rels.append(
                {
                    "name": str(r.get("name", "")).strip() or f"{src}-{tgt}",
                    "source": src,
                    "target": tgt,
                    "relation_type": str(r.get("relation_type", "1:N")),
                    "description": str(r.get("description", "")),
                }
            )
    return {"entities": entities, "relations": clean_rels}


def apply_generated_ontology(
    db: Session,
    scenario: BusinessScenario,
    data: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, int]:
    """把 AI 生成的本体草稿写入场景（追加，不覆盖已有）。"""
    name_map = {e.name: e for e in scenario.entities}
    relation_keys = {(r.name, r.source_entity_id, r.target_entity_id) for r in scenario.relations}
    relation_names = {r.name for r in scenario.relations}
    entities_added = 0
    entities_skipped = 0
    for e in data.get("entities", []):
        if e["name"] in name_map:
            entities_skipped += 1
            continue
        ent = OntologyEntity(
            scenario_id=scenario.id,
            name=e["name"],
            description=e.get("description", ""),
            is_abstract=bool(e.get("is_abstract", False)),
        )
        db.add(ent)
        db.flush()
        for p in e.get("properties", []):
            from ..models import OntologyProperty

            db.add(
                OntologyProperty(
                    entity_id=ent.id,
                    name=p["name"],
                    data_type=p.get("data_type", "string"),
                    description=p.get("description", ""),
                    is_key=bool(p.get("is_key", False)),
                    is_required=bool(p.get("is_required", False)),
                )
            )
        name_map[e["name"]] = ent
        entities_added += 1
    relations_added = 0
    relations_skipped = 0
    for r in data.get("relations", []):
        src, tgt = name_map.get(r["source"]), name_map.get(r["target"])
        if not src or not tgt:
            relations_skipped += 1
            continue
        relation_key = (r["name"], src.id, tgt.id)
        if r["name"] in relation_names or relation_key in relation_keys:
            relations_skipped += 1
            continue
        db.add(
            OntologyRelation(
                scenario_id=scenario.id,
                name=r["name"],
                source_entity_id=src.id,
                target_entity_id=tgt.id,
                relation_type=r.get("relation_type", "1:N"),
                description=r.get("description", ""),
            )
        )
        relation_keys.add(relation_key)
        relation_names.add(r["name"])
        relations_added += 1
    if commit:
        db.commit()
    return {
        "entities_added": entities_added,
        "entities_skipped": entities_skipped,
        "relations_added": relations_added,
        "relations_skipped": relations_skipped,
    }


# ──────────────────────────────────────────────
# 数据映射 → 实例导入
# ──────────────────────────────────────────────
def _mapping_context(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
) -> tuple[DataSource, OntologyEntity]:
    if mapping.scenario_id != scenario.id:
        raise ValueError("映射不属于当前业务场景")
    ds = db.get(DataSource, mapping.data_source_id)
    if not ds or ds.scenario_id not in (None, scenario.id):
        raise ValueError("映射对应的数据源不存在或不属于当前业务场景")
    if ds.type == "file_bucket":
        raise ValueError("该映射的数据源不是数据库类型")
    ent = db.get(OntologyEntity, mapping.entity_id)
    if not ent or ent.scenario_id != scenario.id:
        raise ValueError("映射对应的实体不存在或不属于当前业务场景")
    if not mapping.table_name.strip():
        raise ValueError("请先选择映射的表")
    return ds, ent


def _quoted_mapping_table(table_name: str) -> str:
    """只接受简单表名或 schema.table，避免映射表名拼接出多语句 SQL。"""
    parts = [part.strip() for part in table_name.split(".")]
    if not parts or len(parts) > 2 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", part) for part in parts):
        raise ValueError("表名格式不合法，请重新选择数据源中的表")
    return ".".join(f'"{part}"' for part in parts)


def preview_mapping(
    db: Session,
    scenario: BusinessScenario,
    mapping: DataMapping,
    limit: int = 20,
) -> dict[str, Any]:
    """读取映射源表样本并检查属性覆盖，不创建或修改对象实例。"""
    ds, ent = _mapping_context(db, scenario, mapping)
    sample_limit = max(1, min(int(limit or 20), 100))
    result = datasource_service.run_query(
        ds,
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name)}",
        limit=sample_limit,
    )
    columns = [str(column) for column in result.get("columns", [])]
    available_columns = set(columns)
    col_map = {str(key): str(value) for key, value in (mapping.column_map or {}).items() if value}
    known_properties = {prop.name for prop in ent.properties}
    fields: list[dict[str, Any]] = []
    missing_properties: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    for prop in ent.properties:
        source_column = col_map.get(prop.name, "")
        source_exists = bool(source_column and source_column in available_columns)
        status = "mapped" if source_exists else "missing" if not source_column else "invalid"
        fields.append(
            {
                "property_name": prop.name,
                "data_type": prop.data_type,
                "is_key": prop.is_key,
                "is_required": prop.is_required,
                "source_column": source_column,
                "source_exists": source_exists,
                "status": status,
            }
        )
        if not source_exists:
            missing_properties.append(prop.name)
            if not source_column:
                message = f"属性“{prop.name}”尚未配置源列"
            else:
                message = f"属性“{prop.name}”引用的源列“{source_column}”不存在"
            if prop.is_key or prop.is_required:
                errors.append(message)
            else:
                warnings.append(message)

    for property_name, source_column in col_map.items():
        if property_name not in known_properties:
            errors.append(f"映射引用了不存在的实体属性“{property_name}”")

    mapped_source_columns = set(col_map.values())
    unmapped_columns = [column for column in columns if column not in mapped_source_columns]
    if unmapped_columns:
        warnings.append(f"源表还有 {len(unmapped_columns)} 个列未映射")
    if not result.get("rows"):
        warnings.append("源表当前没有数据，刷新时不会创建对象")
    if result.get("truncated"):
        warnings.append(f"仅展示前 {sample_limit} 行样本")

    ok = not errors
    return {
        "mapping_id": mapping.id,
        "entity_name": ent.name,
        "data_source_name": ds.name,
        "table_name": mapping.table_name,
        "ok": ok,
        "message": "映射检查通过" if ok else "映射存在需要修正的问题",
        "columns": columns,
        "sample_rows": result.get("rows", []),
        "row_count": int(result.get("row_count", 0)),
        "truncated": bool(result.get("truncated", False)),
        "fields": fields,
        "missing_properties": missing_properties,
        "unmapped_columns": unmapped_columns,
        "warnings": warnings,
        "errors": errors,
    }


def import_instances_from_mapping(db: Session, scenario: BusinessScenario, mapping: DataMapping, limit: int = 50) -> dict[str, Any]:
    """按数据映射从数据库表导入实例，并按外键列自动推断关系实例。"""
    ds, ent = _mapping_context(db, scenario, mapping)
    col_map = mapping.column_map or {}

    result = datasource_service.run_query(
        ds,
        f"SELECT * FROM {_quoted_mapping_table(mapping.table_name)}",
        limit=limit,
    )
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    if not rows:
        raise ValueError(f"表 {mapping.table_name} 中没有数据")

    # 主键属性
    key_prop = next((p.name for p in ent.properties if p.is_key), None)
    key_col = col_map.get(key_prop) if key_prop else None

    created_instances: list[OntologyInstance] = []
    existing = {
        i.source_ref: i
        for i in db.execute(
            select(OntologyInstance).where(
                OntologyInstance.entity_id == ent.id,
                OntologyInstance.source == "imported",
            )
        ).scalars().all()
    }

    row_instances: list[OntologyInstance] = []
    for row in rows:
        rec = dict(zip(columns, row))
        attrs = {}
        for prop_name, col in col_map.items():
            if col in rec:
                attrs[prop_name] = rec[col]
        ref = f"{mapping.table_name}:{rec.get(key_col)}" if key_col and key_col in rec else f"{mapping.table_name}:{len(existing) + len(created_instances)}"
        inst = existing.get(ref)
        if not inst:
            display = str(rec.get(key_col) or attrs.get(key_prop) or f"{ent.name}-{len(created_instances) + 1}")
            inst = OntologyInstance(
                scenario_id=scenario.id,
                entity_id=ent.id,
                name=display,
                attributes=attrs,
                source="imported",
                source_ref=ref,
            )
            db.add(inst)
            db.flush()
            existing[ref] = inst
            created_instances.append(inst)
        row_instances.append(inst)

    # 自动推断关系实例：本表若存在指向其他实体主键列的列，则建立关系
    rels_created = 0
    other_mappings = db.execute(
        select(DataMapping).where(
            DataMapping.scenario_id == scenario.id,
            DataMapping.entity_id != ent.id,
        )
    ).scalars().all()
    for om in other_mappings:
        oent = db.get(OntologyEntity, om.entity_id)
        ods = db.get(DataSource, om.data_source_id)
        if not oent or not ods or ods.type == "file_bucket" or not om.table_name:
            continue
        # 找关系：oent -> ent 或 ent -> oent
        rel = None
        for r in scenario.relations:
            if r.source_entity_id == oent.id and r.target_entity_id == ent.id:
                rel = r
                break
            if r.source_entity_id == ent.id and r.target_entity_id == oent.id:
                rel = r
                break
        if not rel:
            continue
        okey_prop = next((p.name for p in oent.properties if p.is_key), None)
        okey_col = om.column_map.get(okey_prop) if okey_prop else None
        if not okey_col:
            continue
        # 本表中指向 oent 主键的列（列名相同或 okey_col 去掉/加上 _id）
        fk_col = None
        for col in columns:
            if col == okey_col or col == okey_col + "_id" or col.replace("_id", "") == okey_col.replace("_id", ""):
                fk_col = col
                break
        if not fk_col:
            continue
        # 其他实体的已导入实例：key 值 → 实例
        oexisting = {
            i.source_ref: i
            for i in db.execute(
                select(OntologyInstance).where(
                    OntologyInstance.entity_id == oent.id,
                    OntologyInstance.source == "imported",
                )
            ).scalars().all()
        }
        okey_by_value: dict[str, OntologyInstance] = {}
        for ref, inst in oexisting.items():
            if f"{om.table_name}:" in ref:
                okey_by_value[ref.split(":", 1)[1]] = inst

        for row, ent_inst in zip(rows, row_instances):
            rec = dict(zip(columns, row))
            fk_val = rec.get(fk_col)
            if fk_val is None:
                continue
            o_inst = okey_by_value.get(str(fk_val))
            if not ent_inst or not o_inst:
                continue
            if rel.source_entity_id == oent.id:
                s, t = o_inst, ent_inst
            else:
                s, t = ent_inst, o_inst
            exists = db.execute(
                select(RelationInstance).where(
                    RelationInstance.relation_id == rel.id,
                    RelationInstance.source_instance_id == s.id,
                    RelationInstance.target_instance_id == t.id,
                )
            ).scalars().first()
            if not exists:
                db.add(
                    RelationInstance(
                        scenario_id=scenario.id,
                        relation_id=rel.id,
                        source_instance_id=s.id,
                        target_instance_id=t.id,
                    )
                )
                rels_created += 1
    db.commit()
    return {"instances_created": len(created_instances), "relations_created": rels_created, "rows_scanned": len(rows)}
