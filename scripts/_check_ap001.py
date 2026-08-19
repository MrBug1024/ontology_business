# -*- coding: utf-8 -*-
"""修正 AP001 项目数据，使其与真实案例（北京****有限公司 2023 年审）一致。"""
import sqlite3
con = sqlite3.connect("backend/data/demo_bookkeeping.db")
cur = con.cursor()
cur.execute(
    """UPDATE audit_projects SET
        audit_year='2023',
        sign_date='2024-05-20',
        materiality=50000.0,
        lead_auditor='lmh',
        report_no='京创会审字[2024]第3999号'
       WHERE project_id='AP001'"""
)
con.commit()
print("updated rows:", cur.rowcount)
cur.execute("SELECT project_id,audit_year,sign_date,materiality,lead_auditor,report_no FROM audit_projects WHERE project_id='AP001'")
print([d[0] for d in cur.description])
print(cur.fetchone())
print("\n--- audit_reports ---")
cur.execute("SELECT report_id,report_no,report_type,opinion_type,report_date,preparer,reviewer,review_date,status FROM audit_reports WHERE project_id='AP001'")
print([d[0] for d in cur.description])
for r in cur.fetchall():
    print(r)
print("\n--- audited_statements ---")
cur.execute("SELECT statement_id,statement_type,period,total_assets,total_liabilities,total_equity,total_revenue,net_profit,status FROM audited_statements WHERE project_id='AP001'")
for r in cur.fetchall():
    print(r)
print("\n--- statement_notes ---")
cur.execute("SELECT note_id,note_no,note_title,status FROM statement_notes WHERE project_id='AP001'")
for r in cur.fetchall():
    print(r)
print("\n--- review_records ---")
cur.execute("SELECT review_id,review_level,reviewer,review_date,review_result,status FROM review_records WHERE project_id='AP001'")
for r in cur.fetchall():
    print(r)
