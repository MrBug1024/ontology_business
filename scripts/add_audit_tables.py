"""为代理记账业务库新增年度审计相关表与示例数据（幂等）。"""
import sqlite3

DB = r"E:\work\test\backend\data\demo_bookkeeping.db"

con = sqlite3.connect(DB)
cur = con.cursor()

# 清理旧表
for t in ("audit_projects", "audit_papers", "confirmations", "audit_adjustments"):
    cur.execute(f"DROP TABLE IF EXISTS {t}")

cur.execute("""
CREATE TABLE audit_projects (
    project_id TEXT PRIMARY KEY,
    customer_id TEXT,
    audit_year TEXT,
    status TEXT,
    sign_date TEXT,
    materiality REAL,
    risk_level TEXT,
    opinion_type TEXT,
    lead_auditor TEXT,
    report_no TEXT
)
""")
cur.execute("""
CREATE TABLE audit_papers (
    paper_id TEXT PRIMARY KEY,
    project_id TEXT,
    paper_type TEXT,
    account_name TEXT,
    content TEXT,
    preparer TEXT,
    prepare_date TEXT,
    review_status TEXT
)
""")
cur.execute("""
CREATE TABLE confirmations (
    conf_id TEXT PRIMARY KEY,
    project_id TEXT,
    target TEXT,
    conf_type TEXT,
    send_date TEXT,
    reply_date TEXT,
    reply_status TEXT,
    reply_amount REAL,
    diff_amount REAL
)
""")
cur.execute("""
CREATE TABLE audit_adjustments (
    adj_id TEXT PRIMARY KEY,
    project_id TEXT,
    account TEXT,
    direction TEXT,
    amount REAL,
    reason TEXT,
    accepted TEXT,
    status TEXT
)
""")

projects = [
    ("AP001", "C001", "2025", "已完成", "2026-01-10", 500000, "中", "标准无保留意见", "张审计", "京创会审字[2026]第001号"),
    ("AP002", "C002", "2025", "进行中", "2026-02-01", 1000000, "高", "", "李审计", ""),
]
papers = [
    ("WP001", "AP001", "风险评估", "", "识别收入确认、应收账款为重大错报风险领域", "张审计", "2026-01-15", "已复核"),
    ("WP002", "AP001", "抽凭记录", "应收账款", "抽取12笔凭证，其中2笔缺少发票附件", "张审计", "2026-01-20", "已复核"),
    ("WP003", "AP001", "函证", "货币资金", "对3家银行发函，全部回函", "张审计", "2026-01-22", "已复核"),
    ("WP004", "AP002", "风险评估", "", "识别存货、关联方交易为高风险领域", "李审计", "2026-02-10", "复核中"),
]
confs = [
    ("CF001", "AP001", "工商银行", "银行", "2026-01-18", "2026-01-25", "已回函", 1500000, 0),
    ("CF002", "AP001", "建设银行", "银行", "2026-01-18", "2026-01-26", "已回函", 800000, 0),
    ("CF003", "AP001", "客户A", "应收", "2026-01-18", "2026-01-28", "已回函", 320000, 5000),
    ("CF004", "AP002", "工商银行", "银行", "2026-02-15", "", "未回函", 0, 0),
    ("CF005", "AP002", "客户B", "应收", "2026-02-15", "", "未回函", 0, 0),
]
adjs = [
    ("ADJ001", "AP001", "应收账款", "贷", 5000, "函证差异，客户A应收多计5000", "是", "已入账"),
    ("ADJ002", "AP001", "管理费用", "借", 12000, "业务招待费超扣除标准，纳税调增", "是", "已入账"),
    ("ADJ003", "AP002", "存货", "贷", 80000, "存货跌价准备计提不足", "否", "待沟通"),
]

cur.executemany("INSERT INTO audit_projects VALUES (?,?,?,?,?,?,?,?,?,?)", projects)
cur.executemany("INSERT INTO audit_papers VALUES (?,?,?,?,?,?,?,?)", papers)
cur.executemany("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?,?)", confs)
cur.executemany("INSERT INTO audit_adjustments VALUES (?,?,?,?,?,?,?,?)", adjs)

con.commit()

for t in ("audit_projects", "audit_papers", "confirmations", "audit_adjustments"):
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(t, n)
con.close()
print("OK")
