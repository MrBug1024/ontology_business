# -*- coding: utf-8 -*-
"""验证 Agent 系统提示词已注入本体摘要"""
import sys
sys.path.insert(0, r"f:\test\backend")

from app.database import SessionLocal
from app.models import BusinessScenario
from app.services.agent_engine import ontology_summary_for

SID = "cc5d3ff36d2a468596dfa9f8ef2995da"
db = SessionLocal()
try:
    s = db.get(BusinessScenario, SID)
    summary = ontology_summary_for(s)
    print("=== 本体摘要（将注入 Agent 系统提示词）===")
    print(summary)
    print(f"\n[统计] 实体 {len(s.entities)} 个，关系 {len(s.relations)} 条，摘要 {len(summary)} 字符")
finally:
    db.close()
