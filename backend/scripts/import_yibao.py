"""将 F:\医保违规审计历史数据 下的 Excel 历史数据导入 SQLite（供审计场景 run_sql 使用）。"""
import sqlite3
import time

import openpyxl

DB = r"f:\test\backend\data\yibao_audit.db"
SRC = r"F:\医保违规审计历史数据"

# 表名 -> (文件名, 表头所在行(1-based))
TABLES = {
    "就诊表": ("就诊表.xlsx", 1),
    "结算表": ("结算表.xlsx", 1),
    "项目明细表": ("项目明细表.xlsx", 1),
    "规则表": ("规则表.xlsx", 3),  # 前两行为标题
}


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    for tname, (fname, header_row) in TABLES.items():
        t0 = time.time()
        wb = openpyxl.load_workbook(f"{SRC}\\{fname}", read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        for _ in range(header_row - 1):
            next(it)
        hdr = next(it)
        cols = []
        seen: dict[str, int] = {}
        for i, h in enumerate(hdr):
            h = str(h).strip() if h is not None else f"col{i}"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
            cols.append(h)
        cur.execute(f'DROP TABLE IF EXISTS "{tname}"')
        cur.execute(f'CREATE TABLE "{tname}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
        ph = ",".join("?" * len(cols))
        insql = f'INSERT INTO "{tname}" VALUES ({ph})'
        batch: list[tuple] = []
        n = 0
        for row in it:
            if all(v is None for v in row):
                continue
            batch.append(tuple(v for v in row))
            if len(batch) >= 2000:
                cur.executemany(insql, batch)
                n += len(batch)
                batch = []
                if n % 40000 == 0:
                    print(tname, n, "rows", round(time.time() - t0, 1), "s", flush=True)
        if batch:
            cur.executemany(insql, batch)
            n += len(batch)
        con.commit()
        wb.close()
        print("DONE", tname, "rows", n, round(time.time() - t0, 1), "s", flush=True)
    # 建索引加速审计查询
    for idx in [
        'CREATE INDEX IF NOT EXISTS idx_mingxi_zy ON "项目明细表"("医保目录名称")',
        'CREATE INDEX IF NOT EXISTS idx_mingxi_jz ON "项目明细表"("就诊ID")',
        'CREATE INDEX IF NOT EXISTS idx_jz_id ON "就诊表"("就诊ID")',
        'CREATE INDEX IF NOT EXISTS idx_js_id ON "结算表"("就诊ID")',
    ]:
        cur.execute(idx)
    con.commit()
    con.close()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
