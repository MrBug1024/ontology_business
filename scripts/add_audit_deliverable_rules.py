# -*- coding: utf-8 -*-
"""为代理记账业务场景新增审计产出物规则（幂等：先查后建）。"""
import urllib.request, json

BASE = "http://127.0.0.1:3009/api/scenarios"
SID = "56e2006148e8499e8599f5c7c8145e60"

def post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req))

d = json.load(urllib.request.urlopen(f"{BASE}/{SID}"))
entities = {e["name"]: e["id"] for e in d["entities"]}
existing = {r["name"] for r in d["rules"]}

RULES = [
    {
        "entity_id": entities["经审计财务报表"],
        "name": "报表勾稽校验-资产负债平衡",
        "description": "经审定的资产负债表必须满足 资产总计 = 负债总计 + 权益总计，否则报表不能出具",
        "condition": {"field": "状态", "op": "==", "value": "已审定"},
        "action_on_match": "校验 资产总计 = 负债总计 + 权益总计（容差±1元），不平衡时退回重编并复核",
        "trigger_action_ids": [],
        "severity": "critical",
        "enabled": True,
    },
    {
        "entity_id": entities["审计报告"],
        "name": "审计报告未出具预警",
        "description": "审计项目已完成但审计报告仍为草稿状态，需尽快完成三级复核并出具",
        "condition": {"field": "状态", "op": "==", "value": "草稿"},
        "action_on_match": "提醒主审会计师完成三级复核（项目-部门-主任）后出具正式报告",
        "trigger_action_ids": [],
        "severity": "warning",
        "enabled": True,
    },
    {
        "entity_id": entities["复核记录"],
        "name": "三级复核未完成预警",
        "description": "审计报告出具前必须完成三级复核（一级/二级/三级），存在待复核记录时不得出具报告",
        "condition": {"field": "状态", "op": "==", "value": "待复核"},
        "action_on_match": "存在待复核级次：按 一级→二级→三级 顺序完成复核后方可出具报告；复核不通过需整改底稿后重新提交",
        "trigger_action_ids": [],
        "severity": "critical",
        "enabled": True,
    },
]

for r in RULES:
    if r["name"] in existing:
        print("skip", r["name"])
        continue
    res = post(f"/{SID}/rules", r)
    print("created", r["name"], res["id"])
