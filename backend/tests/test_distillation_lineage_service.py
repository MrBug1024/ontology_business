from app.services.distillation_lineage_service import infer_lineage_candidates
from app.distillation_conversation_schemas import InvestigationToolCatalogOut
from app.services.distillation_conversation_tools import catalog


def _sample(evidence_key, table, fields, rows):
    return {
        "evidence_key": evidence_key,
        "content": {
            "kind": "database_sample",
            "table": {
                "name": table,
                "fields": [{"field_key": key, "name": name} for key, name in fields],
            },
            "rows": [{key: row.get(name) for key, name in fields} for row in rows],
        },
    }


def test_name_only_overlap_is_not_a_lineage_candidate():
    result = infer_lineage_candidates([
        _sample("left", "结果表", [("a", "业务编号")], [{"业务编号": "R-1"}, {"业务编号": "R-2"}]),
        _sample("right", "输入表", [("b", "业务编号")], [{"业务编号": "B-1"}, {"业务编号": "B-2"}]),
    ])

    assert result["candidates"] == []
    assert result["stats"]["candidate_count"] == 0


def test_value_containment_can_find_differently_named_fields():
    result = infer_lineage_candidates([
        _sample("result", "结果表", [("a", "输出键")], [{"输出键": "R-1"}, {"输出键": "R-2"}]),
        _sample("input", "输入表", [("b", "来源记录")], [{"来源记录": "R-1"}, {"来源记录": "R-2"}, {"来源记录": "R-3"}]),
    ])

    candidate = result["candidates"][0]
    assert candidate["source"]["field"] == "输出键"
    assert candidate["target"]["field"] == "来源记录"
    assert candidate["direction"] == "undirected"
    assert candidate["evidence"]["containment_rate"] == 1.0
    assert candidate["relation_type"] == "possible_link"
    assert candidate["key_shape"] == "one_side_unique"


def test_duplicate_target_values_are_reported_as_ambiguity():
    result = infer_lineage_candidates([
        _sample("result", "结果", [("a", "结果键")], [{"结果键": "R-1"}, {"结果键": "R-2"}]),
        _sample("input", "明细", [("b", "来源键")], [{"来源键": "R-1"}, {"来源键": "R-1"}, {"来源键": "R-2"}]),
    ])

    candidate = result["candidates"][0]
    assert candidate["relation_type"] == "possible_link"
    assert candidate["evidence"]["target_ambiguous_value_count"] == 1
    assert "重复值组" in candidate["explanation"]


def test_input_and_result_roles_add_a_conservative_lineage_direction():
    result = infer_lineage_candidates([
        {**_sample("result", "结果", [("a", "输出键")], [{"输出键": "R-1"}, {"输出键": "R-2"}]),
         "content": {**_sample("result", "结果", [("a", "输出键")], [{"输出键": "R-1"}, {"输出键": "R-2"}])["content"], "role": "result"}},
        {**_sample("input", "输入", [("b", "来源键")], [{"来源键": "R-1"}, {"来源键": "R-2"}]),
         "content": {**_sample("input", "输入", [("b", "来源键")], [{"来源键": "R-1"}, {"来源键": "R-2"}])["content"], "role": "input"}},
    ])

    candidate = result["candidates"][0]
    assert candidate["direction"] == "input_to_result"
    assert candidate["source"]["table"] == "输入"
    assert candidate["target"]["table"] == "结果"


def test_schema_only_read_is_skipped_without_failing_other_samples():
    result = infer_lineage_candidates([
        _sample("result", "结果", [("a", "键")], [{"键": "R-1"}, {"键": "R-2"}]),
        {"evidence_key": "schema", "content": {"kind": "database_catalog", "tables": []}},
        _sample("input", "输入", [("b", "来源")], [{"来源": "R-1"}, {"来源": "R-2"}]),
    ])

    assert len(result["candidates"]) == 1
    assert result["skipped_sources"] == [{
        "evidence_key": "schema",
        "reason": "资料只有结构或文本，没有可用于精确匹配的有界行样本",
    }]


def test_investigation_catalog_accepts_the_lineage_tool():
    items = catalog()
    response = InvestigationToolCatalogOut(
        default_tool_keys=[item["key"] for item in items if item["selectable"]],
        always_available_tool_keys=["ask_human", "propose_document"],
        tools=items,
    )

    assert "infer_data_lineage" in response.default_tool_keys
    assert len(response.tools) == 22
