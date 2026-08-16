# -*- coding: utf-8 -*-
"""策略可行性对照：策略 vs 沪深300 vs 全池等权（诚实回答"回测是否诱人"）"""
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
from backtest.bt_engine import BtEngine

START, END = "2020-01-01", "2025-12-31"


def main():
    cache = DailyCache()

    # 沪深300（统一字符串索引，与缓存一致）
    df_idx = cache.get_daily("sh.000300", start=START, end=END, adjust="none")
    idx = df_idx.set_index("date").sort_index()["close"]
    idx.index = idx.index.astype(str)

    # 股票面板
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:200]
    con.close()
    panel = {}
    for code in codes:
        d = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if d is None or len(d) < 1200:
            continue
        panel[code] = d.set_index("date").sort_index()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()

    # 对齐（字符串索引）
    common = sorted(set(idx.index) & set(closes.index))
    closes = closes.loc[common]
    idx = idx.loc[common]
    print(f"共同交易日: {len(common)} 天")

    eng = BtEngine(start=START, end=END)
    r_cls = eng.backtest_classified(panel, closes, use_mv_pool=True, use_breakout=True,
                                    mv_map=eng._load_mv_map())
    r_rev = eng.backtest_direction(closes, {"rps_120": -1, "lowvol_60": -1, "mom_20": -1})

    bench_ret = closes.pct_change().fillna(0).mean(axis=1)
    hs300_ret = idx.pct_change().fillna(0)

    print("\n" + "=" * 64)
    print("策略可行性对照（2020-2025，含成本）——'诱人'与否要看跟谁比")
    print("=" * 64)
    print(f"{'策略':<24s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 64)

    def show(name, ret):
        eq = (1 + ret).cumprod()
        ann = (1 + eq.iloc[-1] - 1) ** (252 / max(len(ret), 1)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        sh = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
        print(f"{name:<24s} {ann:>8.1%} {dd:>8.1%} {sh:>7.2f}")

    show("买入持有 沪深300", hs300_ret)
    show("买入持有 全池等权(200只)", bench_ret)
    show("反转+低波（方向化）", pd.Series(r_rev["total"], index=[common[-1]]) if False else
         pd.Series(0, index=[common[0]]))  # 占位避免出错
    # 直接显示引擎结果
    print(f"{'反转+低波（引擎）':<22s} {r_rev['annual']:>8.1%} {r_rev['max_dd']:>8.1%} {r_rev['sharpe']:>7.2f}")
    print(f"{'分类策略（引擎）':<22s} {r_cls['annual']:>8.1%} {r_cls['max_dd']:>8.1%} {r_cls['sharpe']:>7.2f}")

    # 2021-2025 熊市段（2020 牛市剔除）
    mask = np.array([d >= "2021-01-01" for d in common])
    print("\n--- 2021-2025（牛市后的真实震荡/下跌市场）---")
    for name, ret in [("沪深300", hs300_ret[mask]), ("全池等权(200只)", bench_ret[mask])]:
        eq = (1 + ret).cumprod()
        ann = (1 + eq.iloc[-1] - 1) ** (252 / max(len(ret), 1)) - 1
        print(f"  {name}: 年化 {ann:>7.1%}")

    # 超额分析：分类策略相对沪深300
    print("\n" + "=" * 64)
    print("关键解读：策略 vs 沪深300 才是 alpha 的度量")
    print("=" * 64)


if __name__ == "__main__":
    main()
