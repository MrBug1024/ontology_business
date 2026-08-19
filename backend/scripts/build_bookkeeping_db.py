"""生成代理记账业务 SQLite 演示数据库到 backend/data/demo_bookkeeping.db"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DATA_DIR
from app.seed_bookkeeping import _build_demo_sqlite

db_path = DATA_DIR / "demo_bookkeeping.db"
_build_demo_sqlite(db_path)
print(f"OK: {db_path}  size={db_path.stat().st_size} bytes")
