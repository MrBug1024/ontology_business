# -*- coding: utf-8 -*-
"""直接测试 LLM 调用，定位 generate-ontology 400 原因"""
import sys
sys.path.insert(0, r"f:\test\backend")

from sqlalchemy import select
from app.database import SessionLocal
from app.models import LLMConfig
from app.services import llm_service

db = SessionLocal()
llm = db.execute(select(LLMConfig).where(LLMConfig.is_default == True).limit(1)).scalars().first()  # noqa: E712
print("llm:", llm.name if llm else None, llm.model if llm else "")
try:
    resp = llm_service.chat(
        llm,
        [{"role": "user", "content": "只输出 JSON：{\"ok\": true}"}],
        temperature=0.3,
        max_tokens=100,
    )
    print("resp:", resp)
except Exception as exc:
    print("ERROR:", type(exc).__name__, exc)
db.close()
