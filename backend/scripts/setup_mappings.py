# -*- coding: utf-8 -*-
"""为医保违规审计场景创建数据映射（实体 ↔ 实际表/列）"""
import json
import urllib.request

SID = "cc5d3ff36d2a468596dfa9f8ef2995da"
DS_SQLITE = "a2d20a398ed744e7839acb910f377d6a"  # 医保审计业务库

BASE = "http://127.0.0.1:8001/api"


def get(url):
    return json.loads(urllib.request.urlopen(f"{BASE}{url}", timeout=30).read().decode())


def post(url, payload):
    req = urllib.request.Request(
        f"{BASE}{url}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


# 1. 获取场景实体
detail = get(f"/scenarios/{SID}")
entities = {e["name"]: e["id"] for e in detail.get("entities", [])}
print("实体:", entities)

# 2. 定义映射：实体 -> (表, 列映射)
#    列映射: 本体属性名 -> 实际列名
MAPPINGS = {
    "规则": ("规则表", {
        "规则ID": "序号",
        "规则名称": "国家问题清单",
        "规则类型": "违规类型",
        "规则内容": "国家违规参考示例",
        "优先级": "领域序号",
    }),
    "药品": ("项目明细表", {
        "药品ID": "医保目录编码",
        "药品名称": "医保目录名称",
        "药品规格": "规格",
        "生产厂家": "商品名",
    }),
    "违规记录": ("项目明细表", {
        "违规ID": "记账流水号",
        "关联业务数据": "就诊ID",
        "关联规则": "医保目录名称",
        "违规类型": "医疗收费项目类别",
        "违规详情": "明细项目费用总额",
        "违规时间": "费用发生时间",
    }),
    "业务数据": ("就诊表", {
        "业务数据ID": "就诊ID",
        "数据名称": "人员姓名",
        "上传时间": "开始时间",
    }),
}

created = []
for ent_name, (table, colmap) in MAPPINGS.items():
    eid = entities.get(ent_name)
    if not eid:
        print(f"  跳过 {ent_name}（无此实体）")
        continue
    try:
        m = post(f"/scenarios/{SID}/mappings", {
            "entity_id": eid,
            "data_source_id": DS_SQLITE,
            "table_name": table,
            "column_map": colmap,
        })
        created.append((ent_name, table, m["id"]))
        print(f"  ✓ {ent_name} -> {table} (mapping {m['id']})")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {ent_name}: {exc}")

print(f"\n共创建 {len(created)} 条数据映射")
