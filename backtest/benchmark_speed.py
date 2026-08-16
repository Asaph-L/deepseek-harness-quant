# -*- coding: utf-8 -*-
"""回测速度基准（2026-08-14 向量化改造验收）

克隆桌面 1.txt（聚宽"年化151%大市值策略"）的结构做测试基准：
  股票池（HS300 级 ~300 只）→ 月度调仓 → 多因子截面排名求和 → Top5 等权满仓。
  （1.txt 用 营收增长率+市值+Beta 三个因子；本基准用等价的 3 个量价因子占位，
    因向量化改造点正是「因子截面计算」，与因子具体来源无关。）

对比：因子计算「逐列 apply(axis=0)」（改造前） vs 「整矩阵向量化」（改造后）。
用法：python backtest/benchmark_speed.py [--stocks 300] [--start 2020-01-01] [--end 2025-12-31]
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS

# "1.txt" 策略的三个因子占位（结构等价：都是截面算因子→排名）
FACTORS = ["rps_120", "lowvol_60", "near_high_250"]
DIRS = {"rps_120": -1, "lowvol_60": -1, "near_high_250": 1}


def load_panel(codes, start, end):
    cache = DailyCache()
    batch = cache.get_daily_batch(codes, start=start, end=end, adjust="qfq",
                                  fields=["close"])
    series = {}
    for code, df in batch.items():
        if df is None or len(df) < 250:
            continue
        series[code] = df.set_index("date").sort_index()["close"]
    if not series:
        return None
    # 全交易日历：一次 SQL DISTINCT date（避免 set.intersection 塌缩 + 慢）
    import sqlite3
    con = sqlite3.connect(cache.db_path)
    calendar = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date>=? AND date<=? ORDER BY date",
        (start, end))]
    con.close()
    df = pd.DataFrame({c: series[c].reindex(calendar) for c in series}).ffill()
    # 丢弃前导 NaN 过多（上市晚）的列
    return df.dropna(axis=1, thresh=250)


def factor_apply(closes, name):
    """改造前：逐列 apply"""
    return closes.apply(lambda col: FACTOR_FUNCS[name](col.astype(float)), axis=0)


def factor_vec(closes, name):
    """改造后：整矩阵向量化"""
    return FACTOR_FUNCS[name](closes.astype(float))


def monthly_top5_backtest(closes, score, topn=5):
    """月度调仓 Top N 等权（1.txt 结构），返回日收益序列"""
    ym = closes.index.astype(str).str[:7]
    month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    ret = pd.Series(0.0, index=closes.index)
    for i, me in enumerate(month_ends):
        pos = closes.index.get_loc(me)
        if pos < 120:
            continue
        sc = score.iloc[pos].dropna()
        if len(sc) < topn:
            continue
        picks = sc.nlargest(topn).index
        nxt = month_ends[i + 1] if i + 1 < len(month_ends) else closes.index[-1]
        nxt_pos = closes.index.get_loc(nxt) if nxt in closes.index else len(closes) - 1
        seg = closes.iloc[pos + 1: nxt_pos + 1].pct_change().fillna(0)
        if len(seg):
            ret.loc[seg.index] = seg[picks].mean(axis=1)
    return ret


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=300)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--archive", type=lambda x: x.lower() in ("1", "true", "yes"),
                    default=True, help="存档 + 生成可视化 HTML（默认开）")
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(r"data\cache\bars.db")
    # 选「回测窗口内」有 ≥1000 交易日的股票，保证面板 ~1400 天（贴近真实全量回测）
    codes = [r[0] for r in con.execute(
        "SELECT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%' "
        "AND date >= ? AND date <= ? "
        "GROUP BY code HAVING COUNT(*) >= 1000 LIMIT ?",
        (args.start, args.end, args.stocks))]
    con.close()
    print(f"股票池: {len(codes)} 只（窗口内均 ≥1000 交易日）")

    t0 = time.time()
    closes = load_panel(codes, args.start, args.end)
    t_load = time.time() - t0
    if closes is None:
        print("面板加载失败")
        return
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只（加载 {t_load:.1f}s）")

    # ===== 因子计算：apply vs 向量化 =====
    print("\n== 因子计算（3 因子）==")
    t_apply = {}
    t_vec = {}
    for name in FACTORS:
        t0 = time.time()
        a = factor_apply(closes, name)
        t_apply[name] = time.time() - t0
        t0 = time.time()
        v = factor_vec(closes, name)
        t_vec[name] = time.time() - t0
        same = np.allclose(a.values, v.values, equal_nan=True)
        print(f"  {name:<14s} apply={t_apply[name]:.2f}s  vec={t_vec[name]:.3f}s  "
              f"加速 {t_apply[name]/max(t_vec[name],1e-6):.0f}x  结果一致={same}")

    ta, tv = sum(t_apply.values()), sum(t_vec.values())
    print(f"  合计: apply={ta:.2f}s  vec={tv:.3f}s  加速 {ta/max(tv,1e-6):.0f}x")

    # ===== 完整月度 Top5 回测 =====
    def build_score(mode):
        score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for name in FACTORS:
            raw = factor_apply(closes, name) if mode == "apply" else factor_vec(closes, name)
            score = score + (raw * DIRS[name]).rank(axis=1, pct=True)
        return score / len(FACTORS)

    vec_ret = None
    for mode in ("apply", "vec"):
        t0 = time.time()
        score = build_score(mode)
        ret = monthly_top5_backtest(closes, score, args.topn)
        t_bt = time.time() - t0
        eq = (1 + ret).cumprod()
        total = eq.iloc[-1] - 1
        print(f"\n完整回测 [{mode:<5s}]: 因子+排名+月度Top{args.topn} = {t_bt:.2f}s | 累计 {total:.1%}")
        if mode == "vec":
            vec_ret = ret

    # ===== 存档 + 可视化（向量化结果，等权基准对照）=====
    if args.archive and vec_ret is not None:
        bench_ret = closes.pct_change().fillna(0).mean(axis=1)
        from backtest.bt_report import archive, list_archives
        res = archive(vec_ret, benchmark=bench_ret,
                      params={"name": f"Top{args.topn}技术三因子", "topn": args.topn,
                              "factors": str(FACTORS), "start": args.start, "end": args.end,
                              "stocks": closes.shape[1], "load_s": round(t_load, 1),
                              "vec_s": round(sum(t_vec.values()), 3)},
                      name=f"top{args.topn}_3factor", category="策略", factors=list(FACTORS))
        m = res["metrics"]
        print(f"\n已存档: {res['json_path']}")
        print(f"可视化: {res['html_path']}")
        print(f"指标: 年化 {m['annual_return']:.1%} | 回撤 {m['max_drawdown']:.1%} | "
              f"夏普 {m['sharpe']:.2f} | 月胜率 {m['monthly_win_rate']:.1%}")


if __name__ == "__main__":
    main()
