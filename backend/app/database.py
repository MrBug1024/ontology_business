"""SQLAlchemy engine / session management."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_data_sources_nullable_scenario() -> None:
    """SQLite 不支持 ALTER COLUMN：若 data_sources.scenario_id 仍为 NOT NULL，则重建表使其可空。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            'SELECT "notnull" FROM pragma_table_info(\'data_sources\') WHERE name = \'scenario_id\''
        ).fetchone()
        if not row or not row[0]:
            return  # 已是可空（或表不存在），无需迁移
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            """
            CREATE TABLE data_sources_new (
                id VARCHAR(32) NOT NULL,
                scenario_id VARCHAR(32),
                name VARCHAR(200) NOT NULL,
                type VARCHAR(30) NOT NULL,
                config JSON,
                status VARCHAR(20),
                last_error TEXT,
                created_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(scenario_id) REFERENCES business_scenarios (id) ON DELETE CASCADE
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO data_sources_new SELECT id, scenario_id, name, type, config, status, last_error, created_at FROM data_sources"
        )
        conn.exec_driver_sql("DROP TABLE data_sources")
        conn.exec_driver_sql("ALTER TABLE data_sources_new RENAME TO data_sources")
        conn.exec_driver_sql("CREATE INDEX ix_data_sources_scenario_id ON data_sources (scenario_id)")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _migrate_workflows_dag() -> None:
    """SQLite 的 create_all 不会给已有表加列：为 ontology_workflows 补 nodes/edges 列。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = [
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info('ontology_workflows')").fetchall()
        ]
        if not cols:
            return
        if "nodes" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN nodes JSON DEFAULT '[]'")
        if "edges" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN edges JSON DEFAULT '[]'")


def init_db() -> None:
    # Import models so they register on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_data_sources_nullable_scenario()
    _migrate_workflows_dag()
