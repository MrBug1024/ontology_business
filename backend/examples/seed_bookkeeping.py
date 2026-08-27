"""代理记账业务场景种子脚本：创建完整的代理记账业务场景。

包含：
- 本体建模（7个实体 + 6个关系）
- SQLite 数据源（含演示数据）
- 文件桶（业务逻辑文档）
- 数据映射 + 实例导入
- 操作（Actions）
- 规则（Rules）
- 工作流（Workflows）
- Agent（AI 代理记账助手）

运行：python backend/examples/seed_bookkeeping.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import DATA_DIR
from app.database import SessionLocal, init_db
from app.models import (
    Agent,
    BusinessScenario,
    DataMapping,
    DataSource,
    LLMConfig,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
)
from app.services import datasource_service, doc_parser, ontology_service


SCENARIO_NAME = "代理记账业务"


# ══════════════════════════════════════════════
# 1. 场景 + 本体
# ══════════════════════════════════════════════
def _ensure_scenario(db) -> BusinessScenario:
    from sqlalchemy import select

    s = db.execute(select(BusinessScenario).where(BusinessScenario.name == SCENARIO_NAME)).scalars().first()
    if s:
        return s

    s = BusinessScenario(
        name=SCENARIO_NAME,
        description=(
            "面向代理记账公司的完整业务场景，覆盖账务处理、税务申报、客户管理三大核心模块。"
            "支持一般纳税人/小规模纳税人差异化处理，内置业务招待费扣除、固定资产折旧、"
            "坏账准备计提、印花税税率等核心业务规则，以及月度记账申报工作流。"
        ),
        industry="财税服务",
        status="active",
    )
    db.add(s)
    db.flush()

    # ── 本体实体 ──
    customer = OntologyEntity(
        scenario_id=s.id, name="客户", icon="user", color="#0ea5e9",
        description="代理记账服务的客户企业，包含纳税人类型、行业、服务周期等信息",
    )
    account = OntologyEntity(
        scenario_id=s.id, name="会计科目", icon="grid", color="#22c55e",
        description="企业会计准则规定的会计科目，包含科目编码、类型、余额",
    )
    voucher = OntologyEntity(
        scenario_id=s.id, name="记账凭证", icon="document", color="#8b5cf6",
        description="会计记账凭证，记录每笔经济业务的借贷分录",
    )
    voucher_line = OntologyEntity(
        scenario_id=s.id, name="凭证分录", icon="list", color="#f59e0b",
        description="记账凭证中的具体分录行，引用会计科目",
    )
    tax_return = OntologyEntity(
        scenario_id=s.id, name="纳税申报表", icon="stamp", color="#ef4444",
        description="各税种纳税申报表，包含申报状态、应纳税额",
    )
    fin_statement = OntologyEntity(
        scenario_id=s.id, name="财务报表", icon="chart", color="#06b6d4",
        description="资产负债表、利润表等财务报表",
    )
    comm_record = OntologyEntity(
        scenario_id=s.id, name="沟通记录", icon="chat", color="#ec4899",
        description="与客户沟通的记录，包含月度确认、政策通知、风险提醒",
    )
    db.add_all([customer, account, voucher, voucher_line, tax_return, fin_statement, comm_record])
    db.flush()

    def add_props(entity, props: list[tuple[str, str, bool, bool, list[str] | None]]):
        for name, dtype, is_key, is_required, enum_vals in props:
            db.add(OntologyProperty(
                entity_id=entity.id, name=name, data_type=dtype,
                is_key=is_key, is_required=is_required,
                is_enum=enum_vals is not None,
                enum_values=enum_vals or [],
            ))

    add_props(customer, [
        ("客户ID", "string", True, True, None),
        ("企业名称", "string", False, True, None),
        ("纳税人类型", "string", False, True, ["小规模纳税人", "一般纳税人"]),
        ("行业", "string", False, True, ["制造业", "服务业", "商贸业"]),
        ("统一社会信用代码", "string", False, False, None),
        ("联系人", "string", False, False, None),
        ("联系电话", "string", False, False, None),
        ("服务开始日期", "date", False, False, None),
        ("服务到期日期", "date", False, False, None),
        ("状态", "string", False, False, ["服务中", "已到期", "已终止"]),
    ])
    add_props(account, [
        ("科目编码", "string", True, True, None),
        ("科目名称", "string", False, True, None),
        ("科目类型", "string", False, True, ["资产", "负债", "权益", "收入", "费用"]),
        ("期初余额", "number", False, False, None),
        ("本期借方发生额", "number", False, False, None),
        ("本期贷方发生额", "number", False, False, None),
        ("期末余额", "number", False, False, None),
    ])
    add_props(voucher, [
        ("凭证ID", "string", True, True, None),
        ("客户ID", "string", False, True, None),
        ("会计期间", "string", False, True, None),
        ("凭证号", "string", False, False, None),
        ("凭证日期", "date", False, False, None),
        ("摘要", "string", False, False, None),
        ("借方合计", "number", False, False, None),
        ("贷方合计", "number", False, False, None),
        ("状态", "string", False, False, ["草稿", "已审核", "已记账"]),
    ])
    add_props(voucher_line, [
        ("分录ID", "string", True, True, None),
        ("凭证ID", "string", False, True, None),
        ("科目编码", "string", False, True, None),
        ("科目名称", "string", False, False, None),
        ("借方金额", "number", False, False, None),
        ("贷方金额", "number", False, False, None),
        ("摘要", "string", False, False, None),
    ])
    add_props(tax_return, [
        ("申报表ID", "string", True, True, None),
        ("客户ID", "string", False, True, None),
        ("税种", "string", False, True, ["增值税", "企业所得税", "个人所得税", "印花税", "城建税", "教育费附加"]),
        ("所属期间", "string", False, True, None),
        ("申报状态", "string", False, False, ["未申报", "已生成", "待确认", "已申报", "已缴款"]),
        ("申报日期", "date", False, False, None),
        ("应纳税额", "number", False, False, None),
        ("实缴税额", "number", False, False, None),
    ])
    add_props(fin_statement, [
        ("报表ID", "string", True, True, None),
        ("客户ID", "string", False, True, None),
        ("会计期间", "string", False, True, None),
        ("报表类型", "string", False, True, ["资产负债表", "利润表", "现金流量表"]),
        ("资产总计", "number", False, False, None),
        ("负债总计", "number", False, False, None),
        ("权益总计", "number", False, False, None),
        ("营业收入", "number", False, False, None),
        ("营业成本", "number", False, False, None),
        ("净利润", "number", False, False, None),
    ])
    add_props(comm_record, [
        ("记录ID", "string", True, True, None),
        ("客户ID", "string", False, True, None),
        ("沟通日期", "date", False, False, None),
        ("沟通类型", "string", False, False, ["月度确认", "政策通知", "风险提醒", "续约提醒"]),
        ("沟通内容", "text", False, False, None),
        ("处理人", "string", False, False, None),
    ])
    db.flush()

    # ── 关系 ──
    db.add(OntologyRelation(scenario_id=s.id, name="拥有凭证", source_entity_id=customer.id, target_entity_id=voucher.id, relation_type="1:N", description="客户拥有记账凭证"))
    db.add(OntologyRelation(scenario_id=s.id, name="包含分录", source_entity_id=voucher.id, target_entity_id=voucher_line.id, relation_type="1:N", description="凭证包含分录行"))
    db.add(OntologyRelation(scenario_id=s.id, name="引用科目", source_entity_id=voucher_line.id, target_entity_id=account.id, relation_type="N:1", description="分录引用会计科目"))
    db.add(OntologyRelation(scenario_id=s.id, name="拥有申报表", source_entity_id=customer.id, target_entity_id=tax_return.id, relation_type="1:N", description="客户拥有纳税申报表"))
    db.add(OntologyRelation(scenario_id=s.id, name="拥有报表", source_entity_id=customer.id, target_entity_id=fin_statement.id, relation_type="1:N", description="客户拥有财务报表"))
    db.add(OntologyRelation(scenario_id=s.id, name="拥有沟通记录", source_entity_id=customer.id, target_entity_id=comm_record.id, relation_type="1:N", description="客户拥有沟通记录"))
    db.commit()
    return s


# ══════════════════════════════════════════════
# 2. SQLite 数据源 + 演示数据
# ══════════════════════════════════════════════
def _ensure_sqlite_source(db, scenario: BusinessScenario) -> DataSource:
    from sqlalchemy import select

    ds = db.execute(
        select(DataSource).where(DataSource.scenario_id == scenario.id, DataSource.type == "sqlite")
    ).scalars().first()
    if ds:
        return ds

    db_path = DATA_DIR / "demo_bookkeeping.db"
    _build_demo_sqlite(db_path)

    ds = DataSource(
        scenario_id=scenario.id,
        name="代理记账业务库",
        type="sqlite",
        config={"path": str(db_path)},
        status="ok",
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def _build_demo_sqlite(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            company_name TEXT,
            taxpayer_type TEXT,
            industry TEXT,
            credit_code TEXT,
            contact_person TEXT,
            contact_phone TEXT,
            service_start TEXT,
            service_end TEXT,
            status TEXT
        );
        CREATE TABLE accounts (
            account_code TEXT PRIMARY KEY,
            account_name TEXT,
            account_type TEXT,
            opening_balance REAL,
            debit_amount REAL,
            credit_amount REAL,
            closing_balance REAL
        );
        CREATE TABLE vouchers (
            voucher_id TEXT PRIMARY KEY,
            customer_id TEXT,
            period TEXT,
            voucher_no TEXT,
            voucher_date TEXT,
            summary TEXT,
            total_debit REAL,
            total_credit REAL,
            status TEXT
        );
        CREATE TABLE voucher_lines (
            line_id TEXT PRIMARY KEY,
            voucher_id TEXT,
            account_code TEXT,
            account_name TEXT,
            debit_amount REAL,
            credit_amount REAL,
            summary TEXT
        );
        CREATE TABLE tax_returns (
            return_id TEXT PRIMARY KEY,
            customer_id TEXT,
            tax_type TEXT,
            period TEXT,
            filing_status TEXT,
            filing_date TEXT,
            tax_amount REAL,
            paid_amount REAL
        );
        CREATE TABLE financial_statements (
            statement_id TEXT PRIMARY KEY,
            customer_id TEXT,
            period TEXT,
            statement_type TEXT,
            total_assets REAL,
            total_liabilities REAL,
            total_equity REAL,
            revenue REAL,
            cost REAL,
            net_profit REAL
        );
        CREATE TABLE communication_records (
            record_id TEXT PRIMARY KEY,
            customer_id TEXT,
            comm_date TEXT,
            comm_type TEXT,
            content TEXT,
            handler TEXT
        );
        """
    )

    # ── 客户 ──
    customers = [
        ("C001", "北京华信科技有限公司", "一般纳税人", "服务业", "91110108MA01ABCD1X", "张经理", "13800138001", "2025-01-01", "2026-12-31", "服务中"),
        ("C002", "上海恒达贸易有限公司", "小规模纳税人", "商贸业", "91310115MA02EFGH2Y", "李总", "13900139002", "2025-03-01", "2026-08-31", "服务中"),
        ("C003", "广州精工制造有限公司", "一般纳税人", "制造业", "91440101MA03IJKL3Z", "王厂长", "13700137003", "2024-07-01", "2026-06-30", "已到期"),
    ]
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)", customers)

    # ── 会计科目（C001 北京华信科技）──
    accounts_c001 = [
        ("1001", "库存现金", "资产", 5000, 0, 0, 5000),
        ("1002", "银行存款", "资产", 500000, 50000, 37830, 512170),
        ("1122", "应收账款", "资产", 80000, 0, 0, 80000),
        ("1601", "固定资产", "资产", 200000, 0, 0, 200000),
        ("1602", "累计折旧", "资产", 30000, 0, 2000, 32000),
        ("2202", "应付账款", "负债", 30000, 0, 0, 30000),
        ("2211", "应付职工薪酬", "负债", 30000, 0, 30000, 0),
        ("2221", "应交税费", "负债", 5000, 283, 5660, 0),
        ("4001", "实收资本", "权益", 500000, 0, 0, 500000),
        ("4103", "本年利润", "权益", 150000, 0, 0, 150000),
        ("5001", "主营业务收入", "收入", 0, 0, 47170, 47170),
        ("5401", "主营业务成本", "费用", 0, 20000, 0, 20000),
        ("5602", "管理费用", "费用", 0, 11717, 0, 11717),
    ]
    cur.executemany("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", accounts_c001)

    # ── 记账凭证（C001，2026-07）──
    vouchers = [
        ("V001", "C001", "2026-07", "记-001", "2026-07-05", "收到客户A技术服务费", 50000, 50000, "已记账"),
        ("V002", "C001", "2026-07", "记-002", "2026-07-10", "支付7月员工工资", 30000, 30000, "已记账"),
        ("V003", "C001", "2026-07", "记-003", "2026-07-15", "购买办公用品", 5000, 5000, "已记账"),
        ("V004", "C001", "2026-07", "记-004", "2026-07-31", "计提固定资产折旧", 2000, 2000, "已记账"),
        ("V005", "C001", "2026-07", "记-005", "2026-07-20", "缴纳7月增值税", 2830, 2830, "已记账"),
        ("V006", "C002", "2026-07", "记-001", "2026-07-08", "收到客户B货款", 30000, 30000, "已记账"),
        ("V007", "C002", "2026-07", "记-002", "2026-07-12", "支付供应商货款", 22000, 22000, "已记账"),
        ("V008", "C003", "2026-07", "记-001", "2026-07-03", "收到客户C设备款", 150000, 150000, "已记账"),
        ("V009", "C003", "2026-07", "记-002", "2026-07-18", "支付原材料采购款", 80000, 80000, "已记账"),
    ]
    cur.executemany("INSERT INTO vouchers VALUES (?,?,?,?,?,?,?,?,?)", vouchers)

    # ── 凭证分录 ──
    voucher_lines = [
        # V001: 收到客户A技术服务费 50000
        ("L001", "V001", "1002", "银行存款", 50000, 0, "收到客户A技术服务费"),
        ("L002", "V001", "5001", "主营业务收入", 0, 47170, "技术服务费收入"),
        ("L003", "V001", "2221", "应交税费", 0, 2830, "增值税销项税额"),
        # V002: 支付7月员工工资 30000
        ("L004", "V002", "2211", "应付职工薪酬", 30000, 0, "支付7月工资"),
        ("L005", "V002", "1002", "银行存款", 0, 30000, "银行转账支付工资"),
        # V003: 购买办公用品 5000
        ("L006", "V003", "5602", "管理费用", 4717, 0, "办公用品费用"),
        ("L007", "V003", "2221", "应交税费", 283, 0, "增值税进项税额"),
        ("L008", "V003", "1002", "银行存款", 0, 5000, "银行转账支付"),
        # V004: 计提固定资产折旧 2000
        ("L009", "V004", "5602", "管理费用", 2000, 0, "计提设备折旧"),
        ("L010", "V004", "1602", "累计折旧", 0, 2000, "累计折旧增加"),
        # V005: 缴纳7月增值税 2830
        ("L011", "V005", "2221", "应交税费", 2830, 0, "缴纳增值税"),
        ("L012", "V005", "1002", "银行存款", 0, 2830, "银行转账缴税"),
        # V006: C002 收到客户B货款 30000
        ("L013", "V006", "1002", "银行存款", 30000, 0, "收到客户B货款"),
        ("L014", "V006", "5001", "主营业务收入", 0, 29126, "商品销售收入"),
        ("L015", "V006", "2221", "应交税费", 0, 874, "增值税（简易计税）"),
        # V007: C002 支付供应商货款 22000
        ("L016", "V007", "5401", "主营业务成本", 22000, 0, "商品采购成本"),
        ("L017", "V007", "1002", "银行存款", 0, 22000, "银行转账支付"),
        # V008: C003 收到客户C设备款 150000
        ("L018", "V008", "1002", "银行存款", 150000, 0, "收到设备销售款"),
        ("L019", "V008", "5001", "主营业务收入", 0, 132743, "设备销售收入"),
        ("L020", "V008", "2221", "应交税费", 0, 17257, "增值税销项税额"),
        # V009: C003 支付原材料采购款 80000
        ("L021", "V009", "1403", "原材料", 70796, 0, "原材料采购"),
        ("L022", "V009", "2221", "应交税费", 9204, 0, "增值税进项税额"),
        ("L023", "V009", "1002", "银行存款", 0, 80000, "银行转账支付"),
    ]
    cur.executemany("INSERT INTO voucher_lines VALUES (?,?,?,?,?,?,?)", voucher_lines)

    # ── 纳税申报表 ──
    tax_returns = [
        ("TR001", "C001", "增值税", "2026-07", "已申报", "2026-07-20", 2830, 2830),
        ("TR002", "C001", "企业所得税", "2026-Q3", "未申报", "", 0, 0),
        ("TR003", "C001", "个人所得税", "2026-07", "已申报", "2026-07-20", 5000, 5000),
        ("TR004", "C001", "印花税", "2026-07", "已申报", "2026-07-20", 15, 15),
        ("TR005", "C001", "城建税", "2026-07", "已申报", "2026-07-20", 198, 198),
        ("TR006", "C001", "教育费附加", "2026-07", "已申报", "2026-07-20", 85, 85),
        ("TR007", "C002", "增值税", "2026-Q3", "已生成", "", 874, 0),
        ("TR008", "C002", "个人所得税", "2026-07", "已申报", "2026-07-20", 2000, 2000),
        ("TR009", "C003", "增值税", "2026-07", "已申报", "2026-07-20", 8053, 8053),
        ("TR010", "C003", "企业所得税", "2026-Q3", "未申报", "", 0, 0),
        ("TR011", "C003", "个人所得税", "2026-07", "已申报", "2026-07-20", 8000, 8000),
    ]
    cur.executemany("INSERT INTO tax_returns VALUES (?,?,?,?,?,?,?,?)", tax_returns)

    # ── 财务报表 ──
    fin_statements = [
        ("FS001", "C001", "2026-07", "资产负债表", 785000, 65000, 720000, 47170, 20000, 15170),
        ("FS002", "C001", "2026-07", "利润表", 0, 0, 0, 47170, 20000, 15170),
        ("FS003", "C002", "2026-07", "资产负债表", 150000, 30000, 120000, 29126, 22000, 7126),
        ("FS004", "C002", "2026-07", "利润表", 0, 0, 0, 29126, 22000, 7126),
        ("FS005", "C003", "2026-07", "资产负债表", 2000000, 500000, 1500000, 132743, 80000, 52743),
        ("FS006", "C003", "2026-07", "利润表", 0, 0, 0, 132743, 80000, 52743),
    ]
    cur.executemany("INSERT INTO financial_statements VALUES (?,?,?,?,?,?,?,?,?,?)", fin_statements)

    # ── 沟通记录 ──
    comm_records = [
        ("CR001", "C001", "2026-07-31", "月度确认", "7月收入47170元，成本20000元，净利润15170元。增值税2830元、个税5000元、印花税15元、城建税198元、教育费附加85元均已申报。Q3企业所得税待申报。", "AI助理"),
        ("CR002", "C001", "2026-07-15", "政策通知", "增值税小规模纳税人减免政策延续至2027年底。贵司为一般纳税人，不适用该减免，但可享受研发费用加计扣除政策。", "AI助理"),
        ("CR003", "C001", "2026-07-20", "风险提醒", "应收账款80000元中，有20000元账龄超过1年，建议按10%计提坏账准备2000元。另请注意业务招待费扣除限额为实际发生额60%与营业收入5‰取小。", "AI助理"),
        ("CR004", "C002", "2026-07-31", "月度确认", "7月收入29126元，成本22000元，净利润7126元。增值税874元（简易计税）已生成待申报。", "AI助理"),
        ("CR005", "C002", "2026-08-01", "续约提醒", "贵司代理记账服务将于2026-08-31到期，请确认是否续约。本期服务涵盖账务处理、税务申报、月度报表，共处理凭证12张。", "AI助理"),
        ("CR006", "C003", "2026-07-31", "月度确认", "7月收入132743元，成本80000元，净利润52743元。增值税8053元已申报。", "AI助理"),
        ("CR007", "C003", "2026-06-15", "续约提醒", "贵司代理记账服务已于2026-06-30到期，请尽快确认续约事宜，避免影响8月申报。", "AI助理"),
    ]
    cur.executemany("INSERT INTO communication_records VALUES (?,?,?,?,?,?)", comm_records)

    conn.commit()
    conn.close()


# ══════════════════════════════════════════════
# 3. 数据映射 + 实例导入
# ══════════════════════════════════════════════
def _ensure_mappings_and_instances(db, scenario: BusinessScenario, ds: DataSource) -> None:
    from sqlalchemy import select

    mapping_spec = {
        "客户": ("customers", {
            "客户ID": "customer_id", "企业名称": "company_name", "纳税人类型": "taxpayer_type",
            "行业": "industry", "统一社会信用代码": "credit_code", "联系人": "contact_person",
            "联系电话": "contact_phone", "服务开始日期": "service_start",
            "服务到期日期": "service_end", "状态": "status",
        }),
        "会计科目": ("accounts", {
            "科目编码": "account_code", "科目名称": "account_name", "科目类型": "account_type",
            "期初余额": "opening_balance", "本期借方发生额": "debit_amount",
            "本期贷方发生额": "credit_amount", "期末余额": "closing_balance",
        }),
        "记账凭证": ("vouchers", {
            "凭证ID": "voucher_id", "客户ID": "customer_id", "会计期间": "period",
            "凭证号": "voucher_no", "凭证日期": "voucher_date", "摘要": "summary",
            "借方合计": "total_debit", "贷方合计": "total_credit", "状态": "status",
        }),
        "凭证分录": ("voucher_lines", {
            "分录ID": "line_id", "凭证ID": "voucher_id", "科目编码": "account_code",
            "科目名称": "account_name", "借方金额": "debit_amount",
            "贷方金额": "credit_amount", "摘要": "summary",
        }),
        "纳税申报表": ("tax_returns", {
            "申报表ID": "return_id", "客户ID": "customer_id", "税种": "tax_type",
            "所属期间": "period", "申报状态": "filing_status", "申报日期": "filing_date",
            "应纳税额": "tax_amount", "实缴税额": "paid_amount",
        }),
        "财务报表": ("financial_statements", {
            "报表ID": "statement_id", "客户ID": "customer_id", "会计期间": "period",
            "报表类型": "statement_type", "资产总计": "total_assets",
            "负债总计": "total_liabilities", "权益总计": "total_equity",
            "营业收入": "revenue", "营业成本": "cost", "净利润": "net_profit",
        }),
        "沟通记录": ("communication_records", {
            "记录ID": "record_id", "客户ID": "customer_id", "沟通日期": "comm_date",
            "沟通类型": "comm_type", "沟通内容": "content", "处理人": "handler",
        }),
    }
    ents = {e.name: e for e in scenario.entities}

    for ent_name, (table, col_map) in mapping_spec.items():
        ent = ents.get(ent_name)
        if not ent:
            continue
        old = db.execute(select(DataMapping).where(DataMapping.entity_id == ent.id)).scalars().all()
        for o in old:
            db.delete(o)
        db.add(DataMapping(scenario_id=scenario.id, entity_id=ent.id, data_source_id=ds.id, table_name=table, column_map=col_map))
    db.commit()

    for ent_name, (table, col_map) in mapping_spec.items():
        ent = ents.get(ent_name)
        if not ent:
            continue
        m = db.execute(select(DataMapping).where(DataMapping.entity_id == ent.id)).scalars().first()
        if not m:
            continue
        try:
            ontology_service.import_instances_from_mapping(db, scenario, m, limit=50)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 导入 {ent_name} 实例失败: {exc}")


# ══════════════════════════════════════════════
# 4. 文件桶（业务文档）
# ══════════════════════════════════════════════
def _ensure_file_bucket(db, scenario: BusinessScenario) -> DataSource:
    from sqlalchemy import select

    ds = db.execute(
        select(DataSource).where(DataSource.scenario_id == scenario.id, DataSource.type == "file_bucket")
    ).scalars().first()
    if ds:
        return ds

    ds = DataSource(scenario_id=scenario.id, name="代理记账文档桶", type="file_bucket", config={}, status="ok")
    db.add(ds)
    db.commit()
    db.refresh(ds)

    samples = {
        "代理记账业务逻辑.md": (
            "# 代理记账业务逻辑\n\n"
            "## 一、账务处理逻辑\n\n"
            "### 1. 收入确认规则\n"
            "- 商贸业（商品销售）：客户取得商品控制权（发货、签收完成）时确认收入；预收货款不计入收入，挂合同负债。\n"
            "- 制造业：现货销售：货物出库、客户验收后确认；设备（履约周期超1年）：按照完工百分比（时段法）分期确认收入。\n"
            "- 服务业（咨询、运维、租赁）：属于时段履约义务，在服务提供的周期内，按月/按完工进度分期确认收入。\n"
            "- 开票与会计收入的差异处理：增值税开票时点≠会计收入确认时点。先开票、货/服务未提供：增值税申报缴税，会计账务计入「合同负债」，不确认营业收入；货已交付、暂时未开票：会计正常确认收入，增值税做未开票收入申报。\n"
            "- 跨期收入处理：严格遵循权责发生制，当年预收次年服务费、货款，全部计入合同负债，在后续履约的所属会计期间分批结转收入。\n\n"
            "### 2. 成本结转方法\n"
            "- 品种法：大批量、单步骤生产（食品、建材、采掘），按产品品类归集料、工、费，月末用约当产量划分在产品和完工产成品成本。\n"
            "- 分步法：多工序流水线连续生产（纺织、汽车、钢材），分生产车间逐步归集成本。\n"
            "- 分批法（订单法）：单件、小批量定制生产（设备定做、船舶、模具），按生产批号归集成本。\n"
            "- 服务业成本归集：直接把对应项目的人工、外包服务费、场地费、耗材，归集计入「合同履约成本」，在确认收入的同期同步结转为主营业务成本。\n\n"
            "### 3. 费用报销审核\n"
            "- 必须取得发票才可税前扣除的业务：外购货物、服务费、房租、运输费、住宿费、广告费等对外采购的支出。\n"
            "- 无需发票的业务支出：员工工资薪金、现金差旅补贴、福利费发放、违约金（非价外费用）、500元以内零星个人收款。\n"
            "- 业务招待费扣除限额：实际发生额的60%，且不能超过当年营业收入的5‰，二者取最小值。\n"
            "- 差旅费：交通费、住宿费凭发票全额扣除；公司制度标准内的差旅补贴不需要发票。\n"
            "- 会议费：可全额税前扣除，不属于招待费。完整附件：会议通知、参会签到表、会议现场资料、场地租赁发票。\n\n"
            "### 4. 固定资产处理\n"
            "- 确认标准：持有目的为生产经营使用、使用寿命超过1个会计年度的有形资产。实务代账通用口径：单价≥5000元作为固定资产。\n"
            "- 折旧方法：年限平均法（直线法，代理记账最常用）、工作量法、双倍余额递减法。\n"
            "- 税法最低折旧年限：房屋建筑物20年；机器设备10年；运输车辆4年；电子设备3年。\n\n"
            "### 5. 往来款项处理\n"
            "- 应收、应付严格按照合同、对账确认单入账；回款/付款时逐笔核销。\n"
            "- 坏账准备计提（账龄分析法）：1年内不提或少提；1~2年计提10%；2~3年计提30%；3年以上全额计提。\n"
            "- 应付账款长期无需支付：转入「营业外收入」，正常缴纳企业所得税。\n"
            "- 股东长期挂账的其他应收款：年度终了未归还，视同股东分红，代扣个税。\n\n"
            "## 二、税务申报逻辑\n\n"
            "### 1. 增值税申报\n"
            "- 一般纳税人：按月申报，采用一般计税，进项票可以抵扣销项。\n"
            "- 小规模纳税人：按季申报，简易计税，进项不能抵扣，现行有阶段性减免优惠。\n"
            "- 进项抵扣审核：专票、海关缴款书等合规凭证才可抵扣；用于福利、免税项目的进项必须做进项转出。\n"
            "- 视同销售：自产货物赠送、对外投资、无偿移送等，要核算销项税额进行纳税。\n"
            "- 留抵退税：一般纳税人为小微企业、制造业等行业，信用等级达标、无税务违规，期末有进项留抵可申请退还。\n\n"
            "### 2. 企业所得税预缴\n"
            "- 季度预缴算法：常规按账面实际利润预缴；核定征收用「应税收入×应税所得率」。\n"
            "- 亏损弥补：查账征收企业季度盈利可直接弥补近5年的以前年度亏损；核定征收不能弥补亏损。\n\n"
            "### 3. 企业所得税汇算清缴\n"
            "- 常见调增项目：业务招待费超支、广告费超标、无合规发票的支出、罚款滞纳金、职工福利费超限额、视同销售差异。\n"
            "- 常见调减项目：国债利息、居民企业分红等免税收入；研发费用100%/75%加计扣除；500万以内固定资产一次性税前扣除。\n\n"
            "### 4. 个人所得税申报\n"
            "- 累计预扣法：逐月累计全年收入，减去6万起征点、社保公积金、专项附加扣除，套用年度税率表计算当期应扣个税。\n"
            "- 年终奖二选一：①单独计税，奖金除以12找税率；②并入当年工资薪金，按综合所得计税。\n\n"
            "### 5. 其他税种\n"
            "- 印花税税率：借款合同0.005%；买卖合同、承揽合同、建设工程合同、运输合同、技术合同0.03%；租赁合同、保管合同、仓储合同、财产保险合同0.1%；土地使用权/房屋/股权转让书据0.05%；证券交易0.05%（2023年8月28日起减半）。\n"
            "- 城建税：以当期实际缴纳的增值税、消费税为基数，7%/5%/1%三档（按地区）。\n"
            "- 教育费附加3%、地方教育附加2%。\n"
            "- 残保金：按单位在职职工人数、安置残疾人比例计算，小微企业有减免政策。\n"
            "- 工会经费：按季度申报，社保金额为季度工资薪金合计数额的2%。\n\n"
            "## 三、客户管理逻辑\n\n"
            "### 1. 新接客户流程\n"
            "- 收集资料：营业执照、法人及办税人身份信息、银行账户信息、往期凭证账簿、科目余额表、电子税务账号密码、代理记账委托合同。\n"
            "- 期初数据录入：按交接时的期末科目余额表录入；无账套的盘点资产负债，建期初数。\n"
            "- 建账规范：按企业类型选择会计准则（小企业准则/企业准则），区分一般纳税人和小规模。\n\n"
            "### 2. 日常沟通规范\n"
            "- 月度确认：每月核对收入、成本票据、银行流水、开票数据，确认税款金额、申报数据。\n"
            "- 季度同步：利润情况、税负情况。\n"
            "- 政策变更：以文字形式推送客户，标注对该企业的具体影响和调整操作。\n"
            "- 风险提醒：发票异常、往来长期挂账、税负异常、费用缺票、股东借款等问题，提前告知风险点及整改方案。\n\n"
            "### 3. 客户档案管理\n"
            "- 档案内容：客户资质证照、委托合同、每期凭证报表、纳税申报表、交接单据、沟通记录、各类审批资料。\n"
            "- 保管期限：会计凭证、账簿、申报表保管30年；委托合同等其他资料永久留存。\n\n"
            "### 4. 到期续约流程\n"
            "- 到期前30天首次提醒，到期前15天再次跟进确认。\n"
            "- 总结当期服务内容、财税风险管控成果，说明后续服务保障，协商续约事宜。\n"
            "- 调价考量：市场财税政策变化、企业业务量增长、账务复杂度提升、额外增值服务增加、人力成本变动。\n"
        ),
        "税务申报指南.md": (
            "# 税务申报指南\n\n"
            "## 增值税\n"
            "- 一般纳税人：按月申报，一般计税方法，销项税额-进项税额=应纳税额\n"
            "- 小规模纳税人：按季申报，简易计税，销售额×征收率（1%或3%）\n"
            "- 申报期限：每月/季终了后15日内\n"
            "- 进项抵扣：专票、海关缴款书、农产品收购发票等合规凭证\n"
            "- 进项转出：用于集体福利、个人消费、免税项目的进项税额\n\n"
            "## 企业所得税\n"
            "- 季度预缴：按账面实际利润或应税收入×应税所得率\n"
            "- 年度汇算清缴：次年5月31日前\n"
            "- 税率：25%（一般）、20%（小型微利企业优惠）、15%（高新技术企业）\n"
            "- 纳税调整：业务招待费（60%且5‰取小）、广告费（15%限额）、研发费用加计扣除（100%）\n\n"
            "## 个人所得税\n"
            "- 工资薪金：累计预扣法，年度税率3%-45%\n"
            "- 专项附加扣除：子女教育、房贷利息、房租、赡养老人、继续教育、大病医疗\n"
            "- 年终奖：单独计税或并入综合所得，择优选择\n\n"
            "## 印花税\n"
            "- 借款合同：0.005%\n"
            "- 买卖合同/承揽/建设/运输/技术合同：0.03%\n"
            "- 租赁/保管/仓储/财产保险合同：0.1%\n"
            "- 产权转移书据：0.05%\n"
            "- 证券交易：0.05%（2023.8.28起减半）\n\n"
            "## 附加税费\n"
            "- 城建税：7%（市区）/5%（县城）/1%（其他）\n"
            "- 教育费附加：3%\n"
            "- 地方教育附加：2%\n"
            "- 计税依据：实际缴纳的增值税+消费税\n"
        ),
        "费用扣除标准.md": (
            "# 费用税前扣除标准\n\n"
            "## 业务招待费\n"
            "- 扣除限额：实际发生额×60% 与 营业收入×5‰ 取小\n"
            "- 必备附件：正规餐饮/招待发票、招待事由、参与人员名单、业务对接记录\n\n"
            "## 广告费和业务宣传费\n"
            "- 扣除限额：营业收入×15%（化妆品/医药/饮料制造25%）\n"
            "- 超支部分可结转以后年度扣除\n\n"
            "## 职工福利费\n"
            "- 扣除限额：工资薪金总额×14%\n\n"
            "## 工会经费\n"
            "- 扣除限额：工资薪金总额×2%\n"
            "- 需取得工会经费收入专用收据\n\n"
            "## 职工教育经费\n"
            "- 扣除限额：工资薪金总额×8%\n"
            "- 超支部分可结转以后年度扣除\n\n"
            "## 利息支出\n"
            "- 向金融企业借款：全额扣除\n"
            "- 向非金融企业借款：不超过金融企业同期同类贷款利率计算的数额\n\n"
            "## 捐赠支出\n"
            "- 公益性捐赠：年度利润总额×12%以内扣除\n"
            "- 超支部分可结转以后3年扣除\n\n"
            "## 资产损失\n"
            "- 需按规定向税务机关申报\n"
            "- 坏账损失：需有相关证据（注销证明、破产判决、催收记录等）\n"
        ),
        "客户管理流程.md": (
            "# 客户管理流程\n\n"
            "## 新接客户\n"
            "1. 签订代理记账委托合同\n"
            "2. 收集资料：营业执照、法人/办税人身份证、银行账户、往期凭证、科目余额表\n"
            "3. 期初数据录入：按期末科目余额表录入资产、负债、权益\n"
            "4. 建账：按企业类型选择会计准则，设置科目\n"
            "5. 交接签字：形成交接单据，双方签字确认\n\n"
            "## 月度服务\n"
            "1. 收集原始凭证（发票、银行流水、工资表等）\n"
            "2. 票据归类+索引\n"
            "3. 编制会计分录\n"
            "4. 勾稽校验（借贷平衡）\n"
            "5. 生成纳税申报表\n"
            "6. 金额判断：<1000元AI直接申报，≥1000元客户确认后申报\n"
            "7. 申报回执反馈\n"
            "8. 生成月度财务报表\n"
            "9. 客户月度沟通确认\n\n"
            "## 季度服务\n"
            "1. 季度增值税申报（小规模纳税人）\n"
            "2. 季度企业所得税预缴\n"
            "3. 季度利润/税负情况同步\n\n"
            "## 年度服务\n"
            "1. 年度企业所得税汇算清缴（次年5月31日前）\n"
            "2. 年度财务报表审计（如需）\n"
            "3. 年度档案整理归档\n\n"
            "## 风险提醒\n"
            "- 发票异常：缺票、假票、票面信息不符\n"
            "- 往来长期挂账：应收账款超1年、应付账款超2年\n"
            "- 税负异常：税负率明显低于行业平均水平\n"
            "- 股东借款：年度终了未归还，视同分红代扣个税\n"
            "- 费用缺票：大额支出无合规发票\n\n"
            "## 到期续约\n"
            "- 到期前30天：首次提醒，总结服务内容\n"
            "- 到期前15天：再次跟进，协商续约\n"
            "- 调价因素：政策变化、业务量增长、复杂度提升、增值服务、人力成本\n"
        ),
    }
    for name, content in samples.items():
        bf = datasource_service.save_bucket_file(ds, name, content.encode("utf-8"))
        db.add(bf)
        db.commit()
        db.refresh(bf)
        r = doc_parser.parse_file(bf.stored_path, bf.filename)
        bf.status = "parsed" if r["status"] == "success" else "error"
        bf.parsed_text = r.get("text", "")
        bf.error = "" if r["status"] == "success" else r.get("message", "")
        db.commit()
    return ds


# ══════════════════════════════════════════════
# 5. 操作（Actions）
# ══════════════════════════════════════════════
def _ensure_actions(db, scenario: BusinessScenario, ds: DataSource) -> None:
    from sqlalchemy import select

    existing = db.execute(select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)).scalars().all()
    if existing:
        return

    ents = {e.name: e for e in scenario.entities}

    actions_spec = [
        {
            "entity": "客户",
            "name": "查询客户列表",
            "description": "查询所有代理记账客户，支持按纳税人类型、行业、状态筛选",
            "input_schema": {
                "type": "object",
                "properties": {
                    "taxpayer_type": {"type": "string", "description": "纳税人类型（可选）：小规模纳税人/一般纳税人"},
                    "industry": {"type": "string", "description": "行业（可选）：制造业/服务业/商贸业"},
                    "status": {"type": "string", "description": "状态（可选）：服务中/已到期/已终止"},
                },
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT customer_id, company_name, taxpayer_type, industry, contact_person, contact_phone, service_start, service_end, status FROM customers WHERE 1=1 {filter}",
            },
            "precondition": "",
            "postcondition": "返回客户列表",
        },
        {
            "entity": "客户",
            "name": "查询客户详情",
            "description": "查询指定客户的详细信息，包含纳税人类型、行业、服务周期",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户ID"},
                },
                "required": ["customer_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT * FROM customers WHERE customer_id = '{customer_id}'",
            },
            "precondition": "customer_id 必须存在",
            "postcondition": "返回客户详情",
        },
        {
            "entity": "记账凭证",
            "name": "查询凭证列表",
            "description": "查询指定客户的记账凭证列表，支持按会计期间筛选",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户ID"},
                    "period": {"type": "string", "description": "会计期间（可选），如 2026-07"},
                },
                "required": ["customer_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT voucher_id, period, voucher_no, voucher_date, summary, total_debit, total_credit, status FROM vouchers WHERE customer_id = '{customer_id}' {period_filter} ORDER BY voucher_date",
            },
            "precondition": "customer_id 必须存在",
            "postcondition": "返回凭证列表",
        },
        {
            "entity": "凭证分录",
            "name": "查询凭证分录",
            "description": "查询指定凭证的详细分录，包含科目编码、借贷金额",
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_id": {"type": "string", "description": "凭证ID"},
                },
                "required": ["voucher_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT line_id, account_code, account_name, debit_amount, credit_amount, summary FROM voucher_lines WHERE voucher_id = '{voucher_id}'",
            },
            "precondition": "voucher_id 必须存在",
            "postcondition": "返回凭证分录",
        },
        {
            "entity": "会计科目",
            "name": "查询科目余额",
            "description": "查询所有会计科目的余额，包含期初、本期发生额、期末",
            "input_schema": {
                "type": "object",
                "properties": {
                    "account_type": {"type": "string", "description": "科目类型（可选）：资产/负债/权益/收入/费用"},
                },
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT account_code, account_name, account_type, opening_balance, debit_amount, credit_amount, closing_balance FROM accounts {type_filter} ORDER BY account_code",
            },
            "precondition": "",
            "postcondition": "返回科目余额表",
        },
        {
            "entity": "纳税申报表",
            "name": "查询申报状态",
            "description": "查询指定客户的各税种申报状态，包含应纳税额、实缴税额",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户ID"},
                    "period": {"type": "string", "description": "所属期间（可选）"},
                },
                "required": ["customer_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT return_id, tax_type, period, filing_status, filing_date, tax_amount, paid_amount FROM tax_returns WHERE customer_id = '{customer_id}' {period_filter} ORDER BY tax_type",
            },
            "precondition": "customer_id 必须存在",
            "postcondition": "返回申报状态列表",
        },
        {
            "entity": "财务报表",
            "name": "查询财务报表",
            "description": "查询指定客户的财务报表，包含资产负债表和利润表",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户ID"},
                    "period": {"type": "string", "description": "会计期间（可选）"},
                },
                "required": ["customer_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT statement_id, period, statement_type, total_assets, total_liabilities, total_equity, revenue, cost, net_profit FROM financial_statements WHERE customer_id = '{customer_id}' {period_filter} ORDER BY period DESC",
            },
            "precondition": "customer_id 必须存在",
            "postcondition": "返回财务报表",
        },
        {
            "entity": "沟通记录",
            "name": "查询沟通记录",
            "description": "查询指定客户的沟通记录，包含月度确认、政策通知、风险提醒",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户ID"},
                    "comm_type": {"type": "string", "description": "沟通类型（可选）：月度确认/政策通知/风险提醒/续约提醒"},
                },
                "required": ["customer_id"],
            },
            "executor_type": "sql",
            "executor_config": {
                "data_source_id": ds.id,
                "sql": "SELECT record_id, comm_date, comm_type, content, handler FROM communication_records WHERE customer_id = '{customer_id}' {type_filter} ORDER BY comm_date DESC",
            },
            "precondition": "customer_id 必须存在",
            "postcondition": "返回沟通记录",
        },
    ]

    for spec in actions_spec:
        ent = ents.get(spec["entity"])
        if not ent:
            continue
        db.add(OntologyAction(
            scenario_id=scenario.id,
            entity_id=ent.id,
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
            executor_type=spec["executor_type"],
            executor_config=spec["executor_config"],
            precondition=spec["precondition"],
            postcondition=spec["postcondition"],
            enabled=True,
        ))
    db.commit()


# ══════════════════════════════════════════════
# 6. 规则（Rules）
# ══════════════════════════════════════════════
def _ensure_rules(db, scenario: BusinessScenario) -> None:
    from sqlalchemy import select

    existing = db.execute(select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)).scalars().all()
    if existing:
        return

    ents = {e.name: e for e in scenario.entities}

    rules_spec = [
        {
            "entity": "记账凭证",
            "name": "勾稽校验-借贷平衡",
            "description": "记账凭证的借方合计必须等于贷方合计，否则凭证不平衡，需要重新编制",
            "condition": {
                "op": "and",
                "conditions": [
                    {"field": "借方合计", "op": "is_not_null", "value": ""},
                    {"field": "贷方合计", "op": "is_not_null", "value": ""},
                ],
            },
            "action_on_match": "凭证借贷平衡，可以记账",
            "severity": "critical",
        },
        {
            "entity": "纳税申报表",
            "name": "小额申报免确认",
            "description": "应纳税额小于1000元时，AI可直接申报无需客户确认；≥1000元需客户确认后申报",
            "condition": {
                "field": "应纳税额",
                "op": "<",
                "value": 1000,
            },
            "action_on_match": "AI直接申报，无需客户确认",
            "severity": "info",
        },
        {
            "entity": "客户",
            "name": "服务到期预警",
            "description": "客户服务状态为已到期时，需要立即提醒续约",
            "condition": {
                "field": "状态",
                "op": "==",
                "value": "已到期",
            },
            "action_on_match": "触发续约提醒流程，通知客户和内部负责人",
            "severity": "warning",
        },
        {
            "entity": "纳税申报表",
            "name": "未申报预警",
            "description": "申报状态为未申报的税种需要提醒，避免逾期申报产生滞纳金",
            "condition": {
                "field": "申报状态",
                "op": "==",
                "value": "未申报",
            },
            "action_on_match": "提醒客户尽快完成申报，避免逾期",
            "severity": "warning",
        },
        {
            "entity": "客户",
            "name": "一般纳税人月度申报",
            "description": "一般纳税人需要按月申报增值税，小规模纳税人按季申报",
            "condition": {
                "field": "纳税人类型",
                "op": "==",
                "value": "一般纳税人",
            },
            "action_on_match": "按月生成增值税申报表，提醒客户在次月15日前完成申报",
            "severity": "info",
        },
        {
            "entity": "客户",
            "name": "小规模纳税人季度申报",
            "description": "小规模纳税人按季申报增值税，享受阶段性减免优惠",
            "condition": {
                "field": "纳税人类型",
                "op": "==",
                "value": "小规模纳税人",
            },
            "action_on_match": "按季生成增值税申报表，提醒客户在季终了后15日内完成申报",
            "severity": "info",
        },
    ]

    for spec in rules_spec:
        ent = ents.get(spec["entity"])
        db.add(OntologyRule(
            scenario_id=scenario.id,
            entity_id=ent.id if ent else None,
            name=spec["name"],
            description=spec["description"],
            condition=spec["condition"],
            action_on_match=spec["action_on_match"],
            severity=spec["severity"],
            enabled=True,
        ))
    db.commit()


# ══════════════════════════════════════════════
# 7. 工作流（Workflows）
# ══════════════════════════════════════════════
def _ensure_workflows(db, scenario: BusinessScenario) -> None:
    from sqlalchemy import select

    existing = db.execute(select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)).scalars().all()
    if existing:
        return

    # 获取 actions 和 rules 的 ID
    actions = {a.name: a for a in db.execute(select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)).scalars().all()}
    rules = {r.name: r for r in db.execute(select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)).scalars().all()}

    # ── 工作流1：月度记账申报流程 ──
    wf1_nodes = [
        {"id": "start", "type": "start", "name": "开始", "position": {"x": 0, "y": 200}, "data": {}},
        {
            "id": "n1", "type": "action", "name": "查询客户信息",
            "position": {"x": 200, "y": 200},
            "data": {
                "action_id": actions.get("查询客户详情", OntologyAction()).id if "查询客户详情" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}"},
            },
        },
        {
            "id": "n2", "type": "action", "name": "查询凭证列表",
            "position": {"x": 400, "y": 200},
            "data": {
                "action_id": actions.get("查询凭证列表", OntologyAction()).id if "查询凭证列表" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}", "period": "{{params.period}}"},
            },
        },
        {
            "id": "n3", "type": "action", "name": "查询科目余额",
            "position": {"x": 600, "y": 200},
            "data": {
                "action_id": actions.get("查询科目余额", OntologyAction()).id if "查询科目余额" in actions else "",
                "params": {},
            },
        },
        {
            "id": "n4", "type": "action", "name": "查询申报状态",
            "position": {"x": 800, "y": 200},
            "data": {
                "action_id": actions.get("查询申报状态", OntologyAction()).id if "查询申报状态" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}", "period": "{{params.period}}"},
            },
        },
        {
            "id": "n5", "type": "rule", "name": "小额判断",
            "position": {"x": 1000, "y": 200},
            "data": {
                "rule_id": rules.get("小额申报免确认", OntologyRule()).id if "小额申报免确认" in rules else "",
                "record": {"应纳税额": "{{params.tax_amount}}"},
            },
        },
        {
            "id": "n6", "type": "llm", "name": "生成月度报告",
            "position": {"x": 1200, "y": 100},
            "data": {
                "system": "你是代理记账专家，根据查询到的数据生成月度服务报告。",
                "prompt": (
                    "请根据以下数据生成月度服务报告：\n"
                    "客户信息：{{n1.result}}\n"
                    "凭证列表：{{n2.result}}\n"
                    "科目余额：{{n3.result}}\n"
                    "申报状态：{{n4.result}}\n"
                    "小额判断：{{n5.result}}\n\n"
                    "报告应包含：1)本月账务处理摘要 2)税务申报情况 3)风险提示 4)下月工作计划"
                ),
            },
        },
        {
            "id": "n7", "type": "llm", "name": "生成确认通知",
            "position": {"x": 1200, "y": 300},
            "data": {
                "system": "你是代理记账专家，生成需要客户确认的申报通知。",
                "prompt": (
                    "本月应纳税额≥1000元，需要客户确认后申报。请生成确认通知：\n"
                    "客户信息：{{n1.result}}\n"
                    "申报状态：{{n4.result}}\n\n"
                    "通知应包含：1)各税种应纳税额 2)申报期限 3)请客户确认是否申报"
                ),
            },
        },
        {
            "id": "end", "type": "end", "name": "结束",
            "position": {"x": 1400, "y": 200},
            "data": {"summary": "月度记账申报流程完成"},
        },
    ]

    wf1_edges = [
        {"id": "e1", "source": "start", "target": "n1", "label": ""},
        {"id": "e2", "source": "n1", "target": "n2", "label": ""},
        {"id": "e3", "source": "n2", "target": "n3", "label": ""},
        {"id": "e4", "source": "n3", "target": "n4", "label": ""},
        {"id": "e5", "source": "n4", "target": "n5", "label": ""},
        {"id": "e6", "source": "n5", "target": "n6", "label": "true"},
        {"id": "e7", "source": "n5", "target": "n7", "label": "false"},
        {"id": "e8", "source": "n6", "target": "end", "label": ""},
        {"id": "e9", "source": "n7", "target": "end", "label": ""},
    ]

    db.add(OntologyWorkflow(
        scenario_id=scenario.id,
        name="月度记账申报流程",
        description=(
            "完整的月度代理记账流程：查询客户信息→查询凭证→查询科目余额→查询申报状态→"
            "小额判断（<1000元直接申报/≥1000元客户确认）→生成月度报告/确认通知"
        ),
        trigger_type="manual",
        trigger_config={"customer_id": "客户ID", "period": "会计期间（如2026-07）", "tax_amount": "应纳税额"},
        nodes=wf1_nodes,
        edges=wf1_edges,
        enabled=True,
    ))

    # ── 工作流2：客户月度沟通流程 ──
    wf2_nodes = [
        {"id": "start", "type": "start", "name": "开始", "position": {"x": 0, "y": 200}, "data": {}},
        {
            "id": "n1", "type": "action", "name": "查询客户信息",
            "position": {"x": 200, "y": 200},
            "data": {
                "action_id": actions.get("查询客户详情", OntologyAction()).id if "查询客户详情" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}"},
            },
        },
        {
            "id": "n2", "type": "action", "name": "查询沟通记录",
            "position": {"x": 400, "y": 200},
            "data": {
                "action_id": actions.get("查询沟通记录", OntologyAction()).id if "查询沟通记录" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}"},
            },
        },
        {
            "id": "n3", "type": "action", "name": "查询申报状态",
            "position": {"x": 600, "y": 200},
            "data": {
                "action_id": actions.get("查询申报状态", OntologyAction()).id if "查询申报状态" in actions else "",
                "params": {"customer_id": "{{params.customer_id}}"},
            },
        },
        {
            "id": "n4", "type": "rule", "name": "到期预警",
            "position": {"x": 800, "y": 200},
            "data": {
                "rule_id": rules.get("服务到期预警", OntologyRule()).id if "服务到期预警" in rules else "",
                "record": {"状态": "{{n1.result.rows[0].status}}"},
            },
        },
        {
            "id": "n5", "type": "llm", "name": "生成沟通内容",
            "position": {"x": 1000, "y": 100},
            "data": {
                "system": "你是代理记账专家，根据客户数据生成月度沟通内容。",
                "prompt": (
                    "请根据以下数据生成月度客户沟通内容：\n"
                    "客户信息：{{n1.result}}\n"
                    "历史沟通：{{n2.result}}\n"
                    "申报状态：{{n3.result}}\n"
                    "到期预警：{{n4.result}}\n\n"
                    "沟通内容应包含：1)本月服务总结 2)税务申报情况 3)风险提示 4)续约提醒（如适用）"
                ),
            },
        },
        {
            "id": "n6", "type": "llm", "name": "生成续约通知",
            "position": {"x": 1000, "y": 300},
            "data": {
                "system": "你是代理记账专家，生成服务到期续约通知。",
                "prompt": (
                    "客户服务已到期，请生成续约通知：\n"
                    "客户信息：{{n1.result}}\n"
                    "历史沟通：{{n2.result}}\n\n"
                    "通知应包含：1)服务到期提醒 2)本期服务总结 3)续约优惠 4)联系方式"
                ),
            },
        },
        {
            "id": "end", "type": "end", "name": "结束",
            "position": {"x": 1200, "y": 200},
            "data": {"summary": "客户月度沟通流程完成"},
        },
    ]

    wf2_edges = [
        {"id": "e1", "source": "start", "target": "n1", "label": ""},
        {"id": "e2", "source": "n1", "target": "n2", "label": ""},
        {"id": "e3", "source": "n2", "target": "n3", "label": ""},
        {"id": "e4", "source": "n3", "target": "n4", "label": ""},
        {"id": "e5", "source": "n4", "target": "n5", "label": "false"},
        {"id": "e6", "source": "n4", "target": "n6", "label": "true"},
        {"id": "e7", "source": "n5", "target": "end", "label": ""},
        {"id": "e8", "source": "n6", "target": "end", "label": ""},
    ]

    db.add(OntologyWorkflow(
        scenario_id=scenario.id,
        name="客户月度沟通流程",
        description=(
            "客户月度沟通流程：查询客户信息→查询历史沟通→查询申报状态→"
            "到期预警判断（已到期→续约通知/服务中→月度沟通）→生成沟通内容"
        ),
        trigger_type="manual",
        trigger_config={"customer_id": "客户ID"},
        nodes=wf2_nodes,
        edges=wf2_edges,
        enabled=True,
    ))
    db.commit()


# ══════════════════════════════════════════════
# 8. Agent
# ══════════════════════════════════════════════
SYSTEM_PROMPT = """你是 AI 代理记账专家，精通中国会计准则和税法，服务于代理记账公司。

## 核心能力

### 1. 账务处理
- **收入确认**：商贸业在客户取得商品控制权时确认；制造业按完工百分比；服务业按月/按进度分期确认。
- **成本结转**：制造业用品种法/分步法/分批法；服务业归集到合同履约成本。
- **费用审核**：
  - 业务招待费：实际发生额×60% 与 营业收入×5‰ 取小
  - 差旅费：凭发票全额扣除；制度标准内补贴无需发票
  - 会议费：全额扣除，需完整附件（通知、签到表、现场资料、场地发票）
- **固定资产**：单价≥5000元确认；折旧年限：房屋20年、机器10年、车辆4年、电子3年
- **往来款项**：坏账准备按账龄计提（1-2年10%、2-3年30%、3年以上100%）

### 2. 税务申报
- **增值税**：一般纳税人按月（进项可抵扣）；小规模按季（简易计税，有减免）
- **企业所得税**：季度预缴（账面利润或应税收入×所得率）；年度汇算清缴（纳税调整）
- **个人所得税**：累计预扣法；年终奖可单独计税或并入综合所得
- **印花税**：借款合同0.005%、买卖合同0.03%、租赁合同0.1%、产权转移0.05%
- **附加税**：城建税7%/5%/1%、教育费附加3%、地方教育附加2%

### 3. 客户管理
- **新接客户**：收集营业执照、法人信息、银行账户、往期凭证、科目余额表
- **月度确认**：核对收入、成本票据、银行流水、开票数据
- **风险提醒**：发票异常、往来长期挂账、税负异常、费用缺票、股东借款
- **到期续约**：到期前30天首次提醒，15天再次跟进

### 4. 工作流程
凭证收集 → 票据归类 → 分录编制 → 勾稽校验 → 申报表生成 → 金额判断（<1000元直接申报，≥1000元客户确认）→ 申报回执 → 财务报表 → 客户沟通

## 工具使用指南
- 用 `list_tables` 查看表结构
- 用 `run_sql` 查询具体数据（注意 LIMIT 控制行数）
- 用 `search_documents` 检索业务规则文档（如"业务招待费扣除标准"）
- 用 `read_document` 读取完整文档
- 用 `list_actions` 查看可用操作
- 用 `execute_action` 执行查询操作
- 用 `list_rules` 查看业务规则
- 用 `evaluate_rule` 评估规则是否命中
- 用 `list_workflows` 查看工作流
- 用 `execute_workflow` 执行完整工作流

## 回答规范
1. 先查询数据，再给出分析和建议
2. 引用具体数据（金额、日期、科目）
3. 涉及税务计算时，列出计算过程
4. 发现风险时，明确标注风险等级和整改建议
5. 回答要专业、简洁、有条理"""


def _ensure_agent(db, scenario: BusinessScenario, ds_sqlite: DataSource, ds_bucket: DataSource) -> None:
    from sqlalchemy import select

    existing = db.execute(select(Agent).where(Agent.scenario_id == scenario.id)).scalars().first()
    if existing:
        return

    llm = db.execute(select(LLMConfig).where(LLMConfig.is_default == True).limit(1)).scalars().first()  # noqa: E712
    if not llm:
        print("  ⚠️ 未找到默认 LLM 配置，Agent 将使用空 LLM")
        llm_id = None
    else:
        llm_id = llm.id

    # 获取技能 ID
    from app.models import Skill
    skills = db.execute(select(Skill).where(Skill.enabled == True)).scalars().all()  # noqa: E712
    skill_ids = [s.id for s in skills]

    db.add(Agent(
        name="AI 代理记账助手",
        description=(
            "精通中国会计准则和税法的代理记账专家，支持账务处理、税务申报、客户管理。"
            "可查询客户凭证、科目余额、申报状态，评估业务规则，执行月度记账申报工作流。"
        ),
        scenario_id=scenario.id,
        llm_config_id=llm_id,
        system_prompt=SYSTEM_PROMPT,
        skill_ids=skill_ids,
        mcp_ids=[],
        data_source_ids=[ds_sqlite.id, ds_bucket.id],
        temperature=0.2,
        max_tokens=4096,
    ))
    db.commit()


# ══════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════
def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    if not settings.uses_sqlite_database or settings.minio_configured:
        raise RuntimeError(
            "seed_bookkeeping 仅供迁移前的隔离 SQLite fixture 使用；"
            "MySQL/MinIO 环境禁止运行，请使用版本化迁移"
        )
    init_db()
    db = SessionLocal()
    try:
        print("📦 创建代理记账业务场景...")
        scenario = _ensure_scenario(db)
        print(f"  ✅ 场景: {scenario.name} (id={scenario.id})")

        print("📊 创建 SQLite 数据源...")
        ds_sqlite = _ensure_sqlite_source(db, scenario)
        print(f"  ✅ 数据源: {ds_sqlite.name} (id={ds_sqlite.id})")

        print("🔗 创建数据映射 + 导入实例...")
        _ensure_mappings_and_instances(db, scenario, ds_sqlite)
        print("  ✅ 映射和实例已就绪")

        print("📁 创建文件桶...")
        ds_bucket = _ensure_file_bucket(db, scenario)
        print(f"  ✅ 文件桶: {ds_bucket.name} (id={ds_bucket.id})")

        print("⚡ 创建操作（Actions）...")
        _ensure_actions(db, scenario, ds_sqlite)
        print("  ✅ 8 个操作已就绪")

        print("📏 创建规则（Rules）...")
        _ensure_rules(db, scenario)
        print("  ✅ 6 条规则已就绪")

        print("🔄 创建工作流（Workflows）...")
        _ensure_workflows(db, scenario)
        print("  ✅ 2 个工作流已就绪")

        print("🤖 创建 Agent...")
        _ensure_agent(db, scenario, ds_sqlite, ds_bucket)
        print("  ✅ Agent 已就绪")

        # Keep the reusable demo seed and the current production-style Agent
        # contract in one idempotent path.  The upgrade adds the AP001 audit
        # object/link model and native DOCX/XLSX template Actions without
        # weakening the governed semantic-query boundary.
        from examples import upgrade_bookkeeping_audit

        print("📎 配置年度审计本体与正式附件模板...")
        upgrade_bookkeeping_audit.upgrade(db)
        print("  ✅ AP001 年审查询与附件动作已就绪")

        print(f"\n✅ 代理记账业务场景已就绪！")
        print(f"   场景 ID: {scenario.id}")
        print(f"   数据源: {ds_sqlite.name} + {ds_bucket.name}")
        print(f"   运行: python backend/examples/seed_bookkeeping.py")
    finally:
        db.close()


if __name__ == "__main__":
    main()
