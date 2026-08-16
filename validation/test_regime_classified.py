# -*- coding: utf-8 -*-
"""完整主链路整合验证：Regime 择时 × 分类策略选股（M5 阶段）
验证"抓周期 × 分类选股"完整系统的联动价值：
  调仓日：① Regime 判定总仓位（五档 → 现金比例）→ ② 分类三池选股 → 三池权重 × 总仓位

对照：
  A. 分类策略（无 Regime，满仓）——当前 bt_engine backtest_classified
  B. 完整系统（分类策略 + Regime 控仓）
  C. 完整系统 + Regime（沪深300 已验证控仓效果）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS
from strategy.stock_state import classify_series
from strategy.breakout_confirm import breakout_filter
from strategy.timing import RegimeDetector
from strategy.style_timing import small_large_strength, pool_weight_shift

START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001
HARD_STOP, HIGH_DD = 0.07, 0.08
MV_MAP_CSV = r"data/cache\circ_mv_map.csv"


def load(limit=200):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:limit]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    return panel, closes


def load_big(limit=800, min_len=1000):
    """扩大样本加载：优先取数据完整度高的股票（2019 起即可，min_len 放宽）"""
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    # 按数据行数排序，取最完整的 limit 只
    rows = []
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < min_len:
            continue
        rows.append((len(df), code, df))
    rows.sort(reverse=True)
    rows = rows[:limit]
    panel = {}
    for _, code, df in rows:
        panel[code] = df.set_index("date").sort_index()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    return panel, closes


def load_index():
    cache = DailyCache()
    df = cache.get_daily("sh.000300", start=START, end=END, adjust="none")
    s = df.set_index("date").sort_index()["close"]
    s.index = s.index.astype(str)
    return s


def load_mv():
    try:
        m = pd.read_csv(MV_MAP_CSV)
        m["code6"] = m["code"].astype(str).str[:6]
        return dict(zip(m["code6"], m["mv_yi"]))
    except Exception:
        return None


def build_score(closes, direction):
    panels = {}
    for name, sign in direction.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    return score / max(len(panels), 1)


def rebalance_dates(closes_idx):
    ym = closes_idx.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(closes_idx).groupby(ym).max()
    dates = [month_ends[m] for m in rb if m in month_ends.index]
    return [d for d in dates if START <= str(d) <= END]


def regime_cash_at(idx, me, confirm=None, cooldown=None):
    """调仓日的 Regime 现金比例（只用截至当日的历史，防未来函数）
    ★2026-08-07 修正：真实 OHLC（原 close 近似导致 ADX 失真）；
    ★低频化：confirm/cooldown 读 params（20 日确认 + 20 日冷却，防 whipsaw）"""
    hist = idx[idx.index <= str(me)]
    if len(hist) < 220:
        return 0.0
    if confirm is None or cooldown is None:
        import yaml
        try:
            cfg = yaml.safe_load((Path(__file__).resolve().parent.parent /
                                  "config" / "params.yaml").read_text(encoding="utf-8"))
            rg = (cfg or {}).get("regime", {}) or {}
            confirm = confirm if confirm is not None else rg.get("confirm_days", 20)
            cooldown = cooldown if cooldown is not None else rg.get("cooldown_days", 20)
        except Exception:
            confirm, cooldown = confirm or 20, cooldown or 20
    rd = RegimeDetector({"confirm_days": confirm, "cooldown_days": cooldown})
    cache = DailyCache()
    df = cache.get_daily("sh.000300", start=None, end=str(me), adjust="none")
    d = df.set_index("date").sort_index()
    d.index = d.index.astype(str)
    d = d[d.index <= str(me)]
    win = d.iloc[-500:]
    dfi = pd.DataFrame({"close": win["close"].astype(float),
                        "high": win["high"].astype(float),
                        "low": win["low"].astype(float)})
    state = "choppy"
    for i in range(len(win)):
        state = rd.update(dfi.iloc[: i + 1])
    return rd.cash_ratio()


def run_classified(panel, closes, mv_map=None, use_regime=False, idx=None,
                   use_style_timing=False, strength_series=None):
    """分类策略（三池），可选叠加 Regime 总仓位 + 风格权重动态化（CS-29）"""
    def_score = build_score(closes, {"rps_120": -1, "lowvol_60": -1})
    atk_score = build_score(closes, {"near_high_250": 1, "mom_120": 1})
    neutral_score = build_score(closes, {"lowvol_60": -1})

    states_all = {code: classify_series(d["close"]) for code, d in panel.items()}
    brk_all = {code: breakout_filter(d) for code, d in panel.items()}

    mv_median = None
    if mv_map:
        mvs = [mv_map.get(c.split(".")[0], np.nan) for c in panel]
        mv_median = np.nanmedian(mvs)

    rdates = rebalance_dates(closes.index)
    dates = closes.index
    n = len(dates)

    left_hold, right_hold, neutral_hold = {}, {}, {}
    w_left, w_right, w_neutral = 0.0, 0.0, 0.0
    total_weight = 1.0
    cost_total, daily = 0.0, []

    for di in range(1, n):
        day, prev = dates[di], dates[di - 1]
        day_ret = 0.0
        rets = []
        for hold in (left_hold, right_hold, neutral_hold):
            for code in hold:
                d = panel[code]
                if prev in d.index and day in d.index:
                    rets.append(d.loc[day, "close"] / d.loc[prev, "close"] - 1)
        if rets:
            day_ret = np.mean(rets)
        daily.append(total_weight * (w_left + w_right + w_neutral) * day_ret)

        # right 池止损
        if right_hold:
            to_sell = []
            for code, (bp, hi, bd) in right_hold.items():
                d = panel[code]
                if day not in d.index:
                    continue
                cur = d.loc[day, "close"]
                if pd.isna(cur):
                    continue
                hi2 = max(hi, cur)
                right_hold[code] = (bp, hi2, bd)
                if cur / bp - 1 <= -HARD_STOP or cur / hi2 - 1 <= -HIGH_DD:
                    to_sell.append(code)
            if to_sell:
                sell_w = len(to_sell) / 10 * w_right * total_weight
                w_right -= len(to_sell) / 10 * w_right
                cost_total += COST * sell_w
                for code in to_sell:
                    del right_hold[code]

        # 调仓日
        if day in rdates and di > 252:
            pos = closes.index.get_loc(day)
            st_day = {code: st.iloc[pos] for code, st in states_all.items() if pos < len(st)}
            left_codes = [c for c, s in st_day.items() if s == "left"]
            right_codes = [c for c, s in st_day.items() if s == "right"]
            neutral_codes = [c for c, s in st_day.items() if s == "neutral"]

            # 市值分池
            if mv_map and mv_median is not None:
                small_right = [c for c in right_codes
                               if mv_map.get(c.split(".")[0], np.nan) < mv_median]
                if small_right:
                    right_codes = [c for c in right_codes if c not in small_right]
                    left_codes = left_codes + small_right
            # 突破确认
            if right_codes:
                right_codes = [c for c in right_codes
                               if c in brk_all and pos < len(brk_all[c]) and brk_all[c].iloc[pos]]

            # 三池权重（归一化）
            pools = [("left", left_codes), ("right", right_codes), ("neutral", neutral_codes)]
            sizes = {k: max(len(v), 1) for k, v in pools}
            total = sum(sizes.values())
            ws = {k: max(v / total, 0.10) for k, v in sizes.items()}
            wsum = sum(ws.values())
            w_left, w_right, w_neutral = ws["left"] / wsum, ws["right"] / wsum, ws["neutral"] / wsum

            # 风格权重动态化（CS-29）：小盘强→超配 left，大盘强→超配 right
            if use_style_timing and strength_series is not None:
                shift = pool_weight_shift(strength_series, day)
                # left 池权重 +shift，right 池权重 -shift（从 right 借给 left）
                w_left = min(w_left + shift, 0.85)
                w_right = max(w_right - shift, 0.10)
                # 重新归一化
                wsum2 = w_left + w_right + w_neutral
                w_left, w_right, w_neutral = w_left / wsum2, w_right / wsum2, w_neutral / wsum2

            # Regime 总仓位
            if use_regime and idx is not None:
                cash = regime_cash_at(idx, day)
                total_weight = 1.0 - cash
            else:
                total_weight = 1.0

            def _pick(score_col, codes, w, k_cap):
                hold = {}
                if not codes or w <= 0:
                    return hold
                sc = score_col[codes].dropna()
                if len(sc) >= 1:
                    k = max(1, min(k_cap, len(sc)))
                    for c in sc.nlargest(k).index:
                        if day in panel[c].index:
                            px = panel[c].loc[day, "close"]
                            if not pd.isna(px):
                                hold[c] = (float(px), float(px), str(day))
                return hold

            left_hold = _pick(def_score.iloc[pos], left_codes, w_left, 6)
            right_hold = _pick(atk_score.iloc[pos], right_codes, w_right, 4)
            neutral_hold = _pick(neutral_score.iloc[pos], neutral_codes, w_neutral, 4)
            cost_total += COST * (len(left_hold) + len(right_hold) + len(neutral_hold)) * total_weight

    ret = pd.Series(daily, index=dates[1:]) - cost_total / max(n - 1, 1)
    return ret


def metrics(ret):
    eq = (1 + ret).cumprod()
    tot = eq.iloc[-1] - 1
    ann = (1 + tot) ** (252 / max(len(ret), 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sh = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    return ann, dd, sh


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="样本量：200 小样本 / 800 扩样本")
    args = ap.parse_args()

    print(f"加载数据（limit={args.limit}）...")
    if args.limit > 300:
        panel, closes = load_big(limit=args.limit)
    else:
        panel, closes = load(limit=args.limit)
    idx = load_index()
    mv_map = load_mv()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 指数 {len(idx)} 天 | 市值映射 {'OK' if mv_map else '无'}")

    print("\n" + "=" * 60)
    print("完整主链路验证：Regime 择时 × 分类选股（2020-2025 含成本季度）")
    print("=" * 60)
    print(f"{'策略':<30s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 60)

    r_cls = run_classified(panel, closes, mv_map=mv_map, use_regime=False)
    r_full = run_classified(panel, closes, mv_map=mv_map, use_regime=True, idx=idx)

    # 风格权重动态化：预计算大小盘相对强度
    strength = small_large_strength(closes, mv_map) if mv_map else None
    r_style = run_classified(panel, closes, mv_map=mv_map, use_regime=True, idx=idx,
                             use_style_timing=True, strength_series=strength)

    # 对照：沪深300 买入持有
    bench = idx.pct_change().fillna(0).dropna()
    bench = bench[bench.index.isin(closes.index)]

    print("\n" + "=" * 60)
    print("完整主链路验证：Regime 择时 × 分类选股 × 风格权重（2020-2025 含成本季度）")
    print("=" * 60)
    print(f"{'策略':<30s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 60)

    for name, r in [("A 分类策略(满仓)", r_cls), ("B 完整系统(Regime+分类)", r_full),
                    ("C B+风格权重动态化", r_style),
                    ("D 沪深300 买入持有", bench)]:
        ann, dd, sh = metrics(r)
        print(f"{name:<30s} {ann:>8.1%} {dd:>8.1%} {sh:>7.2f}")

    a1, d1, s1 = metrics(r_cls)
    a2, d2, s2 = metrics(r_full)
    a3, d3, s3 = metrics(r_style)
    print(f"\n对比: Regime 控仓 {'改善' if s2 > s1 else '未改善'}（夏普 {s1:.2f}→{s2:.2f}）")
    print(f"      风格权重动态化 {'改善' if s3 > s2 else '未改善'}（夏普 {s2:.2f}→{s3:.2f}）")


if __name__ == "__main__":
    main()
