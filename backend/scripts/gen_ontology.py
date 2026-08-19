# -*- coding: utf-8 -*-
"""调用 AI 生成本体并保存草稿"""
import json
import urllib.error
import urllib.request

SID = "cc5d3ff36d2a468596dfa9f8ef2995da"
DESC = (
    "医保违规审计业务场景：医保定点医疗机构的就诊、结算、费用明细数据，以及医保违规审计规则库。"
    "核心业务：根据审计规则（如药品限定支付条件、超标准收费、重复收费等），"
    "从就诊记录、结算记录、费用项目明细中筛查疑似违规记录。"
    "涉及实体：就诊（患者、诊断、住院天数）、结算（险种、金额）、费用项目明细（药品/诊疗项目、规格、数量、单价）、"
    "审计规则（违规类型、依据、限定条件）、定点医药机构、药品（规格、医保限定支付条件）。"
)

req = urllib.request.Request(
    f"http://127.0.0.1:8001/api/scenarios/{SID}/generate-ontology",
    data=json.dumps({"description": DESC}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    r = urllib.request.urlopen(req, timeout=300)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode())
    raise SystemExit(1)
data = json.loads(r.read().decode())
print("entities:", len(data["entities"]), "relations:", len(data["relations"]))
for e in data["entities"]:
    print(" -", e["name"], "|", [p["name"] for p in e["properties"]][:8])
for rel in data["relations"]:
    print(" ~", rel["source"], "--", rel["name"], "->", rel["target"], f"({rel['relation_type']})")
with open(r"f:\test\backend\scripts\ontology_draft.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("draft saved")
