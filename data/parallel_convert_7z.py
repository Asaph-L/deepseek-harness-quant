# -*- coding: utf-8 -*-
"""data/parallel_convert_7z.py — 7z→parquet 并行加速版（6 worker）

2026-08-10 总指导：原串行脚本 12min/个 × 142 个 ≈ 30 小时 → 本脚本并行 6 路 ≈ 5 小时
复用 convert_7z_to_parquet.ingest_7z_file（原子写单日 parquet，天然无冲突）
已完成检查：OUT_DIR 已存在同日期 parquet → 跳过
"""
import sys
import time
from datetime import datetime
from pathlib import Path
from multiprocessing import Pool

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.convert_7z_to_parquet import ingest_7z_file, OUT_DIR  # noqa: E402

DEFAULT_DIR = Path(r"data/minute/download/【2】2026单年A股分钟日频-持续更新到年底/8.9日更新/2026(1)/每日数据")
N_WORKERS = 6


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(BASE / "logs" / f"parallel_convert_{datetime.now():%Y%m%d_%H%M%S}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    d = Path(DEFAULT_DIR)
    paths = sorted(d.glob("*.7z"))
    # 去重：(1) 副本跳过；同日期只取一个
    done = {p.stem for p in OUT_DIR.glob("*.parquet")}
    seen, todo = set(), []
    for p in paths:
        stem = p.stem
        if stem.endswith("(1)"):
            continue
        if stem in seen or stem in done:
            continue
        seen.add(stem)
        todo.append(p)
    log(f"并行转换启动: 待转 {len(todo)} 个（已完成 {len(done)} 跳过）| workers={N_WORKERS}")

    t0 = time.time()
    ok = 0
    with Pool(N_WORKERS) as pool:
        for i, (ds, n, err) in enumerate(pool.imap_unordered(ingest_7z_file, todo), 1):
            if n:
                ok += 1
                log(f"[{i}/{len(todo)}] {ds}: {n:,} 行 ✓")
            elif err:
                log(f"[{i}/{len(todo)}] {ds}: 失败 {err[:80]}")
            if i % 10 == 0:
                log(f"  进度 {i}/{len(todo)}（已用 {(time.time()-t0)/60:.1f} 分钟）")
    log(f"完成: 成功 {ok}/{len(todo)}，总耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
