import sqlite3
c = sqlite3.connect(r"E:\work\test\backend\data\demo_bookkeeping.db")
for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    print(t, "->", cols)
