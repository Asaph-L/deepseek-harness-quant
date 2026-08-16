# -*- coding: utf-8 -*-
"""
validation/test_pool_layers.py — ★三层池实证（2026-08-07，低频纪律：大观察池→小决策池）

对 2020-2025 每个季度调仓点回算三层池，比较次季度收益：
  决策池（技术确认+严格筛选） vs 观察池（四因子 Top） vs 全池（通过硬过滤）
验证"严格筛选提升质量"：决策池应显著跑赢观察池与全池，且规模小（5-15 只）

用法：python validation/test_pool_layers.py（后台，约 30-50 分钟）
输出：output/pool_layers_test.json + 控制台摘要
"""
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

from strategy.ranking_v2 import rank
from strategy.pool_layers import build_layers

OUT = BASE / "output" / "pool_layers_test.json"


def quarter_ends(start="2020-03-01", end="2025-12-31"):
    """季度末交易日列表（用 bars.db 月末真实交易日）"""
    import sqlite3
    con = sqlite3.connect(r"data/cache/bars.db")
    rows = con.execute(
        "SELECT substr(date,1,7) ym, MAX(date) md FROM daily_bar "
        "WHERE code='SH.000300' AND date>=? AND date<=? GROUP BY ym ORDER BY ym",
        (start, end)).fetchall()
    con.close()
    out = []
    for ym, md in rows:
        y, m = int(ym[:4]), int(ym[5:7])
        if m in (3, 6, 9, 12):
            out.append(md)
    return out


def next_q_returns(codes, qd, next_qd):
    """从 qd 到 next_qd 的收益（qfq close 比值，%）；缺失返回 NaN"""
    import sqlite3
    con = sqlite3.connect(r"data/cache/bars.db")
    ret = {}
    ph = ",".join("?" * len(codes))
    if not codes:
        con.close()
        return ret
    rows = con.execute(
        f"SELECT code, date, close FROM daily_bar WHERE adjust='qfq' "
        f"AND code IN ({ph}) AND date IN (?,?)", (*codes, qd, next_qd)).fetchall()
    con.close()
    by = {}
    for c, d, p in rows:
        by.setdefault(c, {})[d] = p
    for c in codes:
        d = by.get(c, {})
        if qd in d and next_qd in d and d[qd]:
            ret[c] = (d[next_qd] / d[qd] - 1) * 100
    return ret


def main():
    qends = quarter_ends()
    # 对齐：每个调仓点取当月末，下季度末
    rows = []
    for i in range(len(qends) - 1):
        qd, nqd = qends[i], qends[i + 1]
        try:
            rk = rank(qd, 100)
        except Exception as e:
            print(f"  {qd} rank 失败: {e}", flush=True)
            continue
        if "top" not in rk:
            continue
        layers = build_layers(rk, capital=200_000, regime_cash=None)
        watch_codes = [t["code"] for t in layers["watch"]]
        dec_codes = [t["code"] for t in layers["decision"]]
        r_watch = next_q_returns(watch_codes, qd, nqd)
        r_dec = next_q_returns(dec_codes, qd, nqd)
        rows.append({
            "qd": qd, "nqd": nqd,
            "n_watch": len(watch_codes), "n_cand": layers["n_candidate"],
            "n_dec": len(dec_codes),
            "watch_ret": (sum(r_watch.values()) / len(r_watch)) if r_watch else None,
            "dec_ret": (sum(r_dec.values()) / len(r_dec)) if r_dec else None,
            "dec_excess": ((sum(r_dec.values()) / len(r_dec)) - (sum(r_watch.values()) / len(r_watch)))
                          if r_dec and r_watch else None,
            "n_watch_ret": len(r_watch), "n_dec_ret": len(r_dec),
        })
        tag = "✓" if (rows[-1]["dec_excess"] or 0) > 0 else "✗"
        print(f"  {qd}→{nqd}: watch {rows[-1]['n_watch']}只 {rows[-1]['watch_ret']:+.1f}% | "
              f"dec {rows[-1]['n_dec']}只 {rows[-1]['dec_ret']:+.1f}% | 超额 {rows[-1]['dec_excess']:+.1f}pp {tag}",
              flush=True)

    df = pd.DataFrame(rows)
    OUT.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 汇总 ===")
    if df.empty:
        print("无有效样本")
        return 0
    print(f"季度数: {len(df)} | 决策池规模: 中位数 {df['n_dec'].median():.0f} 只（min {df['n_dec'].min()} / max {df['n_dec'].max()}）")
    print(f"观察池平均季度收益: {df['watch_ret'].mean():+.2f}% | 决策池: {df['dec_ret'].mean():+.2f}%")
    print(f"★决策池超额: 平均 {df['dec_excess'].mean():+.2f}pp/季 | 胜率 {100 * (df['dec_excess'] > 0).mean():.0f}%")
    if df["n_dec_ret"].sum() > 0:
        print(f"样本量: 观察池 {df['n_watch_ret'].sum()} 只·季 / 决策池 {df['n_dec_ret'].sum()} 只·季")
    return 0


if __name__ == "__main__":
    sys.exit(main())
