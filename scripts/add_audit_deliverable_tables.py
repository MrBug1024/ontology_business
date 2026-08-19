"""为代理记账业务库新增审计产出物相关表与案例数据（幂等）。

数据来源：北京有限公司-京创会审字[2024]第3999号-标准无保留意见 案例
- 经审计财务报表（资产负债表/利润表合计数）
- 审计报告（标准无保留意见）
- 报表附注（货币资金/应收账款/固定资产等）
- 管理建议书
- 三级复核记录
"""
import sqlite3

DB = r"E:\work\test\backend\data\demo_bookkeeping.db"
con = sqlite3.connect(DB)
cur = con.cursor()

for t in ("audit_reports", "audited_statements", "statement_notes", "review_records"):
    cur.execute(f"DROP TABLE IF EXISTS {t}")

# 1. 审计报告（含审计报告正文与管理建议书）
cur.execute("""
CREATE TABLE audit_reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT,
    report_no TEXT,
    report_type TEXT,
    opinion_type TEXT,
    report_date TEXT,
    preparer TEXT,
    reviewer TEXT,
    review_date TEXT,
    status TEXT,
    content_summary TEXT
)
""")

# 2. 经审计财务报表（四表合计数）
cur.execute("""
CREATE TABLE audited_statements (
    statement_id TEXT PRIMARY KEY,
    project_id TEXT,
    statement_type TEXT,
    period TEXT,
    total_assets REAL,
    total_liabilities REAL,
    total_equity REAL,
    total_revenue REAL,
    net_profit REAL,
    status TEXT
)
""")

# 3. 报表附注
cur.execute("""
CREATE TABLE statement_notes (
    note_id TEXT PRIMARY KEY,
    project_id TEXT,
    note_no TEXT,
    note_title TEXT,
    note_content TEXT,
    status TEXT
)
""")

# 4. 复核记录（三级复核）
cur.execute("""
CREATE TABLE review_records (
    review_id TEXT PRIMARY KEY,
    project_id TEXT,
    review_level TEXT,
    reviewer TEXT,
    review_date TEXT,
    review_result TEXT,
    issues_found TEXT,
    status TEXT
)
""")

# ===== 案例数据：AP001（北京****有限公司 2023 年审，标准无保留意见）=====
# 审计报告
cur.executemany(
    "INSERT INTO audit_reports VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    [
        ("AR001", "AP001", "京创会审字[2024]第3999号", "审计报告", "标准无保留意见",
         "2024-05-20", "lmh", "yyj", "2024-04-06", "已出具",
         "审计了北京****有限公司2023年度财务报表，包括2023年12月31日资产负债表、2023年度利润表、现金流量表、所有者权益变动表及附注。财务报表在所有重大方面按照企业会计准则编制，公允反映财务状况、经营成果和现金流量。"),
        ("AR002", "AP001", "京创会建字[2024]第3999号", "管理建议书", "标准无保留意见",
         "2024-05-20", "lmh", "yyj", "2024-04-06", "已出具",
         "针对审计过程中识别的内部控制缺陷提出建议：应收账款函证样本量不足、银行存款零余额账户未函证、截止性测试未抽查期后大额凭证、研发费用未见立项结项资料等。"),
    ],
)

# 经审计财务报表（案例精确合计数）
cur.executemany(
    "INSERT INTO audited_statements VALUES (?,?,?,?,?,?,?,?,?,?)",
    [
        ("AS001", "AP001", "资产负债表", "2023-12-31",
         7956640.49, 10530199.94, -2573559.45, None, None, "已审定"),
        ("AS002", "AP001", "利润表", "2023年度",
         None, None, None, 48036043.11, 1949126.37, "已审定"),
        ("AS003", "AP001", "现金流量表", "2023年度",
         None, None, None, None, None, "已审定"),
        ("AS004", "AP001", "所有者权益变动表", "2023年度",
         None, None, -2573559.45, None, None, "已审定"),
    ],
)

# 报表附注（案例关键附注）
cur.executemany(
    "INSERT INTO statement_notes VALUES (?,?,?,?,?,?)",
    [
        ("SN001", "AP001", "五(一)", "货币资金",
         "期末数2,521,324.82元，期初数4,830,136.97元。其中：库存现金26,743.25元、银行存款2,494,581.57元。", "已审定"),
        ("SN002", "AP001", "五(二)", "应收账款",
         "期末数24,000.00元，期初数28,200.00元。期末未审数-1,694,550.05元，经期末负值重分类调整1,718,550.05元后审定数24,000.00元。账龄1年以下。", "已审定"),
        ("SN003", "AP001", "五(五)", "固定资产",
         "期末数13,421.80元，期初数18,295.75元。本期折旧4,704.00元。", "已审定"),
        ("SN004", "AP001", "五(十一)", "应付账款",
         "期末数6,159,930.00元，期初数8,574,600.00元。", "已审定"),
        ("SN005", "AP001", "五(十九)", "未分配利润",
         "期末数-5,573,559.45元，期初数-7,522,685.82元。本期净利润1,949,126.37元。", "已审定"),
    ],
)

# 三级复核记录
cur.executemany(
    "INSERT INTO review_records VALUES (?,?,?,?,?,?,?,?)",
    [
        ("RV001", "AP001", "一级复核", "yyj", "2024-04-06", "通过",
         "底稿索引号规范，前三阶段底稿齐全；函证样本量偏少，已补充说明。", "已完成"),
        ("RV002", "AP001", "二级复核", "张审计", "2024-04-15", "通过",
         "风险评估与重要性水平确定合理；应收账款负值重分类调整恰当。", "已完成"),
        ("RV003", "AP001", "三级复核", "李审计", "2024-05-10", "通过",
         "审计意见类型恰当，标准无保留意见；报告要素完整。", "已完成"),
    ],
)

con.commit()

# 校验
for t in ("audit_reports", "audited_statements", "statement_notes", "review_records"):
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"{t}: {n} 行")
con.close()
print("OK")
