# -*- coding: utf-8 -*-
"""列出规则表全部规则摘要"""
import sqlite3

con = sqlite3.connect(r"f:\test\backend\data\yibao_audit.db")
cur = con.cursor()
cur.execute("SELECT 领域序号, 国家问题清单, 违规类型 FROM 规则表")
for seq, desc, vtype in cur.fetchall():
    print(f"[{seq}] ({vtype}) {desc[:80]}")
