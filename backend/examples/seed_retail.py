"""演示数据种子脚本：创建一个示例业务场景（零售销售分析），含本体、SQLite 数据源、文件桶。

运行：python backend/examples/seed_retail.py
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from app.config import DATA_DIR
from app.database import SessionLocal, init_db
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    LLMConfig,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
)
from app.services import datasource_service, doc_parser, ontology_service


def _ensure_scenario(db) -> BusinessScenario:
    from sqlalchemy import select

    s = db.execute(select(BusinessScenario).where(BusinessScenario.name == "零售销售分析")).scalars().first()
    if s:
        return s
    s = BusinessScenario(
        name="零售销售分析",
        description="面向连锁零售门店的销售、库存与客户分析场景，演示本体建模 + 数据源 + Agent 对话。",
        industry="零售",
        status="active",
    )
    db.add(s)
    db.flush()

    # ── 本体实体 ──
    customer = OntologyEntity(scenario_id=s.id, name="客户", icon="user", color="#0ea5e9", description="购买商品的客户")
    product = OntologyEntity(scenario_id=s.id, name="商品", icon="box", color="#22c55e", description="销售的商品 SKU")
    store = OntologyEntity(scenario_id=s.id, name="门店", icon="shop", color="#f59e0b", description="线下零售门店")
    order = OntologyEntity(scenario_id=s.id, name="订单", icon="document", color="#8b5cf6", description="客户下单记录")
    db.add_all([customer, product, store, order])
    db.flush()

    def add_props(entity, props: list[tuple[str, str, bool]]):
        for name, dtype, is_key in props:
            db.add(OntologyProperty(entity_id=entity.id, name=name, data_type=dtype, is_key=is_key))

    add_props(customer, [("客户ID", "string", True), ("姓名", "string", False), ("等级", "string", False), ("注册时间", "date", False)])
    add_props(product, [("商品ID", "string", True), ("名称", "string", False), ("类别", "string", False), ("单价", "number", False)])
    add_props(store, [("门店ID", "string", True), ("名称", "string", False), ("城市", "string", False)])
    add_props(order, [("订单ID", "string", True), ("客户ID", "string", False), ("门店ID", "string", False), ("下单时间", "datetime", False), ("金额", "number", False)])
    db.flush()

    # ── 关系 ──
    db.add(OntologyRelation(scenario_id=s.id, name="下单", source_entity_id=customer.id, target_entity_id=order.id, relation_type="1:N"))
    db.add(OntologyRelation(scenario_id=s.id, name="销售", source_entity_id=store.id, target_entity_id=order.id, relation_type="1:N"))
    db.add(OntologyRelation(scenario_id=s.id, name="包含商品", source_entity_id=order.id, target_entity_id=product.id, relation_type="N:M"))
    db.commit()
    return s


def _ensure_sqlite_source(db, scenario: BusinessScenario) -> DataSource:
    from sqlalchemy import select

    ds = db.execute(
        select(DataSource).where(DataSource.scenario_id == scenario.id, DataSource.type == "sqlite")
    ).scalars().first()
    if ds:
        return ds

    db_path = DATA_DIR / "demo_retail.db"
    _build_demo_sqlite(db_path)

    ds = DataSource(
        scenario_id=scenario.id,
        name="零售业务库",
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
        CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, level TEXT, register_date TEXT);
        CREATE TABLE products (product_id TEXT PRIMARY KEY, name TEXT, category TEXT, price REAL);
        CREATE TABLE stores (store_id TEXT PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT, store_id TEXT, order_time TEXT, amount REAL);
        """
    )
    customers = [
        ("C001", "张伟", "VIP", "2024-01-10"),
        ("C002", "李娜", "普通", "2024-02-15"),
        ("C003", "王强", "VIP", "2024-03-20"),
        ("C004", "刘洋", "普通", "2024-04-05"),
        ("C005", "陈静", "黄金", "2024-05-18"),
    ]
    products = [
        ("P001", "无线鼠标", "数码配件", 99.0),
        ("P002", "机械键盘", "数码配件", 399.0),
        ("P003", "保温杯", "生活用品", 59.0),
        ("P004", "笔记本支架", "办公用品", 129.0),
        ("P005", "蓝牙耳机", "数码配件", 299.0),
    ]
    stores = [
        ("S01", "北京朝阳店", "北京"),
        ("S02", "上海浦东店", "上海"),
        ("S03", "广州天河店", "广州"),
    ]
    orders = [
        ("O1001", "C001", "S01", "2025-06-01 10:20:00", 498.0),
        ("O1002", "C002", "S02", "2025-06-02 14:30:00", 59.0),
        ("O1003", "C003", "S01", "2025-06-03 09:15:00", 299.0),
        ("O1004", "C004", "S03", "2025-06-05 16:45:00", 129.0),
        ("O1005", "C005", "S02", "2025-06-08 11:00:00", 528.0),
        ("O1006", "C001", "S01", "2025-06-10 15:30:00", 99.0),
        ("O1007", "C003", "S03", "2025-06-12 10:00:00", 399.0),
        ("O1008", "C005", "S02", "2025-06-15 13:20:00", 59.0),
    ]
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
    cur.executemany("INSERT INTO stores VALUES (?,?,?)", stores)
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    conn.commit()
    conn.close()


def _ensure_mappings_and_instances(db, scenario: BusinessScenario, ds: DataSource) -> None:
    """创建实体→表的数据映射，并导入演示实例（含自动推断的关系实例）。"""
    from sqlalchemy import select

    # 实体 → 表 的映射（本体属性名 → 表列名）
    mapping_spec = {
        "客户": ("customers", {"客户ID": "customer_id", "姓名": "name", "等级": "level", "注册时间": "register_date"}),
        "商品": ("products", {"商品ID": "product_id", "名称": "name", "类别": "category", "单价": "price"}),
        "门店": ("stores", {"门店ID": "store_id", "名称": "name", "城市": "city"}),
        "订单": ("orders", {"订单ID": "order_id", "客户ID": "customer_id", "门店ID": "store_id", "下单时间": "order_time", "金额": "amount"}),
    }
    ents = {e.name: e for e in scenario.entities}
    # 先建立全部映射
    for ent_name, (table, col_map) in mapping_spec.items():
        ent = ents.get(ent_name)
        if not ent:
            continue
        old = db.execute(
            select(DataMapping).where(DataMapping.entity_id == ent.id)
        ).scalars().all()
        for o in old:
            db.delete(o)
        db.add(DataMapping(scenario_id=scenario.id, entity_id=ent.id, data_source_id=ds.id, table_name=table, column_map=col_map))
    db.commit()

    # 再逐个导入实例（导入会自动推断关系实例）
    for ent_name, (table, col_map) in mapping_spec.items():
        ent = ents.get(ent_name)
        if not ent:
            continue
        m = db.execute(
            select(DataMapping).where(DataMapping.entity_id == ent.id)
        ).scalars().first()
        if not m:
            continue
        try:
            ontology_service.import_instances_from_mapping(db, scenario, m, limit=50)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 导入 {ent_name} 实例失败: {exc}")


def _ensure_file_bucket(db, scenario: BusinessScenario) -> DataSource:
    from sqlalchemy import select

    ds = db.execute(
        select(DataSource).where(DataSource.scenario_id == scenario.id, DataSource.type == "file_bucket")
    ).scalars().first()
    if ds:
        return ds
    ds = DataSource(scenario_id=scenario.id, name="业务文档桶", type="file_bucket", config={}, status="ok")
    db.add(ds)
    db.commit()
    db.refresh(ds)

    # 写入示例文档并解析
    samples = {
        "销售政策.md": (
            "# 销售政策\n\n"
            "## 会员等级\n- VIP：年消费满 5000 元，享 9 折。\n- 黄金：年消费满 2000 元，享 95 折。\n- 普通：无折扣。\n\n"
            "## 退换货\n- 7 天内无理由退货。\n- 数码配件需保留包装。\n"
        ),
        "门店运营手册.txt": (
            "门店运营手册\n\n1. 每日开店前检查库存与价签。\n2. 高峰时段（10-12 点、15-18 点）增派收银。\n"
            "3. 每周盘点一次高价值商品。\n4. 客户投诉 24 小时内响应。\n"
        ),
        "月度销售目标.csv": "月份,门店,目标金额,实际金额\n2025-06,北京朝阳店,50000,52300\n2025-06,上海浦东店,45000,41200\n2025-06,广州天河店,40000,43800\n",
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


def _ensure_llm(db) -> None:
    from sqlalchemy import select

    if db.execute(select(LLMConfig).limit(1)).scalars().first():
        return
    db.add(
        LLMConfig(
            name="默认模型（请修改）",
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="",
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=4096,
            is_default=True,
        )
    )
    db.commit()


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    if not settings.uses_sqlite_database or settings.minio_configured:
        raise RuntimeError(
            "seed_retail 已在仅保留医保审计和代理记账的远端部署中封存；"
            "只能用于隔离 SQLite fixture"
        )
    init_db()
    db = SessionLocal()
    try:
        scenario = _ensure_scenario(db)
        ds = _ensure_sqlite_source(db, scenario)
        _ensure_mappings_and_instances(db, scenario, ds)
        _ensure_file_bucket(db, scenario)
        _ensure_llm(db)
        print("✅ 演示数据已就绪：业务场景「零售销售分析」+ SQLite 数据源 + 数据映射/实例 + 文件桶 + 默认 LLM 配置")
    finally:
        db.close()


if __name__ == "__main__":
    main()
