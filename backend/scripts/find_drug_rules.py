# -*- coding: utf-8 -*-
"""查找规则表中涉及药品、且药品在项目明细表中真实存在的规则"""
import re
import sqlite3

con = sqlite3.connect(r"f:\test\backend\data\yibao_audit.db")
cur = con.cursor()

cur.execute(
    "SELECT 领域序号, 国家问题清单 FROM 规则表 "
    "WHERE 国家问题清单 LIKE '%注射液%' OR 国家问题清单 LIKE '%胶囊%' "
    "OR 国家问题清单 LIKE '%滴眼液%' OR 国家问题清单 LIKE '%软膏%'"
)
rules = cur.fetchall()
print("药品类规则数:", len(rules))

names = set()
for _, desc in rules:
    m = re.findall(r"([\u4e00-\u9fa5A-Za-z]+?(?:注射液|胶囊|滴眼液|软膏|颗粒))", desc)
    names.update(m)
print("规则涉及药品:", sorted(names))

print("\n在项目明细表中存在的药品:")
found = []
for n in sorted(names):
    cur.execute("SELECT COUNT(*) FROM 项目明细表 WHERE 医保目录名称 LIKE ?", (f"%{n}%",))
    c = cur.fetchone()[0]
    if c > 0:
        found.append((n, c))
        print(f"  {n}: {c} 条")
if not found:
    print("  (无)")

# 看看数据里实际有哪些"注射液"
print("\n数据中实际存在的注射液(前30):")
cur.execute(
    "SELECT 医保目录名称, COUNT(*) c FROM 项目明细表 "
    "WHERE 医保目录名称 LIKE '%注射液' GROUP BY 医保目录名称 ORDER BY c DESC LIMIT 30"
)
for name, c in cur.fetchall():
    print(f"  {name}: {c}")
