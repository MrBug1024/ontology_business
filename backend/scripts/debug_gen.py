# -*- coding: utf-8 -*-
"""复现 generate_ontology 的 LLM 调用，打印原始输出"""
import sys
sys.path.insert(0, r"f:\test\backend")

from sqlalchemy import select
from app.database import SessionLocal
from app.models import LLMConfig
from app.services import llm_service
from app.services.ontology_service import _GEN_PROMPT, _extract_json

DESC = (
    "医保违规审计业务场景：医保定点医疗机构的就诊、结算、费用明细数据，以及医保违规审计规则库。"
    "核心业务：根据审计规则（如药品限定支付条件、超标准收费、重复收费等），"
    "从就诊记录、结算记录、费用项目明细中筛查疑似违规记录。"
    "涉及实体：就诊（患者、诊断、住院天数）、结算（险种、金额）、费用项目明细（药品/诊疗项目、规格、数量、单价）、"
    "审计规则（违规类型、依据、限定条件）、定点医药机构、药品（规格、医保限定支付条件）。"
)

db = SessionLocal()
llm = db.execute(select(LLMConfig).where(LLMConfig.is_default == True).limit(1)).scalars().first()  # noqa: E712
db.close()

resp = llm_service.chat(
    llm,
    [
        {"role": "system", "content": "你只输出 JSON。"},
            {"role": "user", "content": _GEN_PROMPT.replace("{description}", DESC[:3000])},
    ],
    temperature=0.3,
    max_tokens=4096,
)
content = resp.get("content", "")
print("=== content length:", len(content))
print("=== content head ===")
print(content[:500])
print("=== content tail ===")
print(content[-500:])
try:
    data = _extract_json(content)
    print("=== JSON OK, entities:", len(data.get("entities", [])))
except Exception as exc:
    print("=== JSON FAIL:", type(exc).__name__, exc)
