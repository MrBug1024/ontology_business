# -*- coding: utf-8 -*-
"""验证规则8：护理/诊查费天数是否超住院天数"""
import sqlite3

con = sqlite3.connect(r"f:\test\backend\data\yibao_audit.db")
cur = con.cursor()

# 数据中护理类收费项目
cur.execute(
    "SELECT 医保目录名称, COUNT(*) c FROM 项目明细表 "
    "WHERE 医保目录名称 LIKE '%护理%' OR 医保目录名称 LIKE '%诊查%' "
    "GROUP BY 医保目录名称 ORDER BY c DESC LIMIT 20"
)
print("护理/诊查类收费项目:")
for name, c in cur.fetchall():
    print(f"  {name}: {c}")

# 住院记录中 住院天数 分布
cur.execute("SELECT COUNT(*), MIN(住院天数), MAX(住院天数) FROM 就诊表 WHERE 住院天数 IS NOT NULL AND 住院天数 > 0")
print("\n住院记录(住院天数>0):", cur.fetchone())

# 粗略验证：某住院患者护理费天数 vs 住院天数
cur.execute(
    """
    SELECT d.就诊ID, z.住院天数,
           COUNT(DISTINCT d.费用发生时间) AS 护理天数
    FROM 项目明细表 d
    JOIN 就诊表 z ON d.就诊ID = z.就诊ID
    WHERE d.医保目录名称 LIKE '%护理%' AND z.住院天数 > 0
    GROUP BY d.就诊ID, z.住院天数
    HAVING 护理天数 > z.住院天数
    LIMIT 10
    """
)
rows = cur.fetchall()
print("\n护理天数>住院天数 的就诊(前10):")
for r in rows:
    print(" ", r)
