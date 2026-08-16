# -*- coding: utf-8 -*-
"""7z 批量入库（路径写死版，绕开 shell 传参截断问题）——2026-08-09 深夜"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.ingest_minute_7z import ingest_7z, _conn

DATA_DIR = Path(r"data\minute\download\【2】2026单年A股分钟日频-持续更新到年底\8.9日更新\2026(1)\每日数据")
MIN_DATE = "20260411"   # 4/11 之后为增量（4/10 前在 1m_price_zip parquet 中）


def log(msg):
    from datetime import datetime
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(BASE / "logs" / "batch_minute_7z_v2.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    # 已完成日期（minute.db 中已有数据的交易日）
    con = _conn()
    done = {r[0][:10].replace("-", "") for r in con.execute(
        "SELECT DISTINCT substr(trade_time,1,10) FROM minute_1m").fetchall()}
    con.close()
    # 待处理：按日期排序，过滤 (1) 副本 + 已完成 + 小于 MIN_DATE
    seen = set()
    todo = []
    for p in sorted(DATA_DIR.glob("*.7z")):
        stem = p.stem
        is_copy = stem.endswith("(1)")
        date_s = stem[:-3] if is_copy else stem
        if is_copy or date_s in seen or date_s in done:
            continue
        if date_s < MIN_DATE:
            continue
        seen.add(date_s)
        todo.append((date_s, p))
    log(f"待处理 7z: {len(todo)} 个（≥{MIN_DATE}，已去重，已完成 {len(done)} 天跳过）")
    ok = 0
    for date_s, p in todo:
        try:
            ds, n, err = ingest_7z(p)
            if n:
                ok += 1
                log(f"  {ds}: 入库 {n} 行")
            elif err:
                log(f"  [失败] {ds}: {err[:120]}")
        except Exception as e:
            log(f"  [异常] {date_s}: {str(e)[:100]}")
    log(f"批量完成: 成功 {ok}/{len(todo)}")


if __name__ == "__main__":
    main()
