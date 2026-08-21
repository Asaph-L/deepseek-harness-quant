# -*- coding: utf-8 -*-
"""factors/opportunities/backtest_pv_consensus.py — pv_consensus 胜率回测（B-6 补历史胜率 · 外包 AI-1 · 2026-08-09）

★目的：第 7 类机会 pv_consensus（量价五强共识）当前 winrate=0.60 为初始基准，
本脚本用历史数据重算五强信号（与因子池 run_pool 同口径：direction<0 取负转正 → rank(pct=True)），
hits≥4 触发 → 1/3/6 月持有回测（PIT：T+1 开盘、披露窗对齐）。

★双口径（2026-08-09 主程序方向疑点裁决前都做）：
  mode_a：rank ≤ 0.20 命中（scan.py load_external_signals 当前口径）
  mode_b：rank ≥ 0.80 命中（因子池"好=rank 大"语义的修正口径）
  输出两份胜率，由主程序裁决最终覆盖 winrate_approx。

输出：logs/pv_consensus_winrates.json
用法：
  python factors/opportunities/backtest_pv_consensus.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.opportunities.backtest_winrate import _add_months

BARS_DB = r"data/cache/bars.db"
OUT = BASE / "logs" / "pv_consensus_winrates.json"

FIVE = ["turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol"]
HIT_GE = 4          # 五强命中 ≥4 才算共识（与 load_external_signals 一致）
START = "2020-01-01"


def load_bars() -> pd.DataFrame:
    """qfq 日线：code/date/close/turn/volume（轻量，只取回测需要列）"""
    con = sqlite3.connect(BARS_DB)
    df = pd.read_sql(
        "SELECT code, date, close, turn, volume FROM daily_bar "
        "WHERE adjust='qfq' AND date>='2019-06-01' "
        "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'", con)
    con.close()
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype("category")
    return df


def five_factor_ranks(win: pd.DataFrame) -> pd.DataFrame:
    """月末窗口 → 五强因子转正值 + 截面 rank（与因子池 run_pool 同口径）
    返回 {code: {ft: rank}}；rank ∈ (0,1)，好=大（因子池语义）"""
    close = win.pivot(index="date", columns="code", values="close").ffill()
    turn = win.pivot(index="date", columns="code", values="turn").ffill()
    ret = close.pct_change()

    t20 = turn.rolling(20, min_periods=10).mean()
    f = pd.DataFrame(index=close.columns)
    # turnover（direction=-1 → 取负，低换手=值大）
    f["turnover"] = -t20.iloc[-1]
    # lowvol（direction=-1 → 取负，低波=值大）
    vol = ret.rolling(60, min_periods=40).std().iloc[-1] * np.sqrt(252)
    f["lowvol"] = -vol
    # reversal20（direction=+1，跌得多=值大）
    f["reversal20"] = -(close.iloc[-1] / close.shift(20).iloc[-1] - 1)
    # sentiment（direction=+1，换手异动大=值大）
    base = turn.rolling(60, min_periods=30).mean()
    f["sentiment"] = (t20 / base.replace(0, np.nan)).iloc[-1]
    # turn_mid_prox（direction=-1 → 取负，接近中位=值大）
    med = t20.median(axis=1)
    f["turn_mid_prox"] = -(t20.sub(med, axis=0)).iloc[-1]

    ranks = pd.DataFrame(index=close.columns)
    for ft in FIVE:
        ranks[ft] = f[ft].rank(pct=True)          # 与 run_pool 一致：rank(pct=True) 升序
    return ranks.dropna()


def pv_consensus_codes(win: pd.DataFrame, mode: str = "ge80", hit_ge: int = HIT_GE) -> list:
    """★触发函数（可复用，backtest_winrate 主框架 --with-pv-consensus 调用）
    win: 窗口日线（需含 close/turn 列）
    mode: 'ge80' = rank≥0.80 命中（好=rank大，诊断建议口径）
          'le20' = rank≤0.20 命中（scan.py load_external_signals 当前口径）
    → 触发代码列表
    """
    ranks = five_factor_ranks(win)
    if ranks.empty:
        return []
    if mode == "ge80":
        hits = (ranks >= 0.80).sum(axis=1)
    else:
        hits = (ranks <= 0.20).sum(axis=1)
    return list(hits[hits >= hit_ge].index)


def run() -> dict:
    print("加载日线...", flush=True)
    bars = load_bars()
    dates = pd.DatetimeIndex(sorted(bars["date"].unique()))
    by_code = {c: g.sort_values("date") for c, g in bars.groupby("code")}

    # 月度回测日
    month_ends = []
    cur = pd.Timestamp(START)
    while cur <= dates.max():
        mask = (dates >= cur) & (dates < _add_months(cur, 1))
        if mask.any():
            month_ends.append(dates[mask][-1])
        cur = _add_months(cur, 1)
    print(f"回测窗口 {month_ends[0].date()} ~ {month_ends[-1].date()}，{len(month_ends)} 期", flush=True)

    # {mode: {horizon: [(ret, dd)]}}
    samples = {"mode_a": {h: [] for h in (1, 3, 6)},
               "mode_b": {h: [] for h in (1, 3, 6)}}

    for k, t in enumerate(month_ends):
        win = bars[(bars["date"] > t - pd.Timedelta(days=330)) & (bars["date"] <= t)]
        if len(win) < 2000:
            continue
        ranks = five_factor_ranks(win)
        if ranks.empty:
            continue
        # 双口径命中
        hit_a = (ranks <= 0.20).sum(axis=1)
        hit_b = (ranks >= 0.80).sum(axis=1)
        codes_a = list(hit_a[hit_a >= HIT_GE].index)
        codes_b = list(hit_b[hit_b >= HIT_GE].index)

        for mode, codes in (("mode_a", codes_a), ("mode_b", codes_b)):
            for code in codes:
                g = by_code.get(code)
                if g is None or len(g) < 40:
                    continue
                gdates = pd.DatetimeIndex(g["date"])
                ixs = np.where(gdates > t)[0]
                if len(ixs) == 0:
                    continue
                buy_i = int(ixs[0])
                buy_px = float(g["open"].iloc[buy_i]) if "open" in g.columns else None
                if buy_px is None:
                    # 轻量加载未取 open → 用次日收盘近似？不，需 open。这里回退用 close 当日
                    buy_px = float(g["close"].iloc[buy_i])
                closes = g["close"].astype(float).reset_index(drop=True)
                for h in (1, 3, 6):
                    r = _holding_stats_light(closes, buy_i, buy_px, gdates, h)
                    if r:
                        samples[mode][h].append(r)
        if (k + 1) % 12 == 0:
            print(f"  {k + 1}/{len(month_ends)} 期 ({t.date()}) A:{len(codes_a)} B:{len(codes_b)}", flush=True)

    # 汇总
    result = {"mode_a": {}, "mode_b": {}}
    for mode in ("mode_a", "mode_b"):
        for h, lst in samples[mode].items():
            if not lst:
                result[mode][str(h)] = {"n": 0}
                continue
            rets = np.array([r[0] for r in lst])
            dds = np.array([r[1] for r in lst])
            wins, losses = rets[rets > 0], rets[rets <= 0]
            result[mode][str(h)] = {
                "n": int(len(rets)),
                "winrate": round(float((rets > 0).mean()), 4),
                "avg_ret": round(float(rets.mean()), 4),
                "med_ret": round(float(np.median(rets)), 4),
                "max_dd": round(float(dds.min()), 4),
                "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 4)
                            if len(wins) and len(losses) and losses.mean() != 0 else None,
            }

    out = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "meta": {
            "note": "pv_consensus 历史胜率回测（B-6）：五强量价因子（turn_mid_prox/sentiment/turnover/reversal20/lowvol）"
                    "历史重算（与因子池 run_pool 同口径：direction<0 取负转正 + rank(pct=True)），hits≥4 触发，"
                    "T+1 开盘买入，持有 1/3/6 月；PIT 严格",
            "mode_a": "rank ≤ 0.20 命中（scan.py 当前口径，待主程序确认方向）",
            "mode_b": "rank ≥ 0.80 命中（因子池'好=rank大'语义口径）",
            "period": f"{month_ends[0].date()} ~ {month_ends[-1].date()}",
            "n_months": len(month_ends),
            "caveat": "历史五强为 bars 重算近似（因子池 daily_scores 仅当日）；sentiment/turn_mid_prox 需 turn 列，"
                      "bars.db turn 口径可能与因子池 data_loader 有差异（见 data_loader）",
        },
        "results": result,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _holding_stats_light(px: pd.Series, buy_i: int, buy_price: float,
                         dates: pd.DatetimeIndex, months: int):
    """持有 months 自然月：到期最近交易日收盘卖出；返回 (ret, max_dd) 或 None
    （复用 backtest_winrate._holding_stats 的口径：标准价格回撤）"""
    if buy_price is None or buy_price <= 0 or buy_i >= len(px) - 1:
        return None
    target = _add_months(dates[buy_i], months)
    sell_mask = dates[buy_i + 1:] > target
    if sell_mask.any():
        sell_i = buy_i + 1 + int(np.argmax(np.asarray(sell_mask)))
        if sell_i >= len(px):
            return None
        path = px.iloc[buy_i:sell_i + 1] / buy_price - 1
    else:
        path = px.iloc[buy_i:] / buy_price - 1
    path = path.replace([np.inf, -np.inf], np.nan).dropna()
    if len(path) < 2:
        return None
    ret = float(path.iloc[-1])
    if not (-2.0 < ret < 2.0):
        return None
    rel = path.iloc[1:]
    if len(rel) == 0:
        dd = 0.0
    else:
        price = 1.0 + rel
        dd = float((price / price.cummax() - 1).min())
        if dd != dd or dd < -1.0:
            dd = -1.0
    return ret, dd


if __name__ == "__main__":
    r = run()
    print("\n===== pv_consensus 胜率（双口径） =====")
    for mode, label in (("mode_a", "A: rank≤0.20（scan 当前口径）"), ("mode_b", "B: rank≥0.80（好=rank大）")):
        print(f"--- {label} ---")
        for h in (1, 3, 6):
            v = r["results"][mode][str(h)]
            if v.get("n"):
                print(f"  {h}月: n={v['n']:5d} 胜率{v['winrate']:.1%} 均{v['avg_ret']:+.1%} 回撤{v['max_dd']:.1%}")
            else:
                print(f"  {h}月: 样本不足")
    print(f"\n已存 {OUT}")
