# -*- coding: utf-8 -*-
"""validation/timing_explore.py — 择时信号探索：领先型逻辑 vs 滞后回撤（2026-08-14）

背景：满仓主义大波段（回撤触发）经 T+1 复核后跑输满仓（8.6% vs 11.5%），根因是回撤=滞后指标。
本脚本探索另一套逻辑——领先型信号（动量/均线/波动率），全部 T+1 执行，看哪些能真跑赢满仓。

信号（月度，沪深300 2005+ 全历史，T+1 次月执行）：
  mom12   12月动量 > 0
  mom12t  12月动量 三档（>5% 满仓 / -5~5% 半仓 / <-5% 空仓）
  mom6    6月动量 > 0
  mom3    3月动量 > 0
  ma6/12  均线金叉（6月均线 > 12月均线）
  ma12t   价格 > 12月均线
  mom+ma  动量>0 且 价格>12月均线（双确认）
  vol     波动率防守（3年分位 高→空 / 中→半 / 低→满）

基准：全池等权月收益 eq（bars.db qfq）
用法：python validation/timing_explore.py
"""
import sys
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
CACHE = Path(r"data/cache")

FULL, HALF, DEF = 1.0, 0.5, 0.0


def load_eq():
    con = sqlite3.connect(str(CACHE / "bars.db"))
    df = pd.read_sql("SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    m = p.resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    return m.pct_change().mean(axis=1).dropna()


def load_hs300():
    """沪深300 月末收盘（baostock 缓存 2005+，缺则回落 bars.db 2019+）"""
    pq = BASE / "output" / "hs300_monthly.parquet"
    if pq.exists():
        d = pd.read_parquet(pq)["close"].resample("ME").last()
        d.index = d.index.strftime("%Y-%m")
        return d.astype(float)
    con = sqlite3.connect(str(CACHE / "bars.db"))
    rows = con.execute("SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
    con.close()
    return pd.Series({str(x[0])[:7]: float(x[1]) for x in rows}).sort_index()


def build_signals(m):
    mom12 = m / m.shift(12) - 1
    mom6 = m / m.shift(6) - 1
    mom3 = m / m.shift(3) - 1
    ma6 = m.rolling(6, min_periods=3).mean()
    ma12 = m.rolling(12, min_periods=6).mean()
    vol = m.pct_change().rolling(12, min_periods=6).std() * np.sqrt(12)
    volq = vol.rolling(36, min_periods=18).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)

    def S(mask, full=1.0, half=None, def_=0.0):
        return pd.Series(np.where(mask, full, def_), index=m.index)

    sig = {}
    sig["mom12"] = S(mom12 > 0)
    sig["mom12三档"] = S(mom12 > 0.05, full=1.0, def_=0.0).where(
        (mom12 >= -0.05) | (mom12 > 0.05), 0.0)
    # 上面写法绕，重写三档：
    sig["mom12三档"] = pd.Series(np.where(mom12 > 0.05, 1.0, np.where(mom12 < -0.05, 0.0, 0.5)), index=m.index)
    sig["mom6"] = S(mom6 > 0)
    sig["mom3"] = S(mom3 > 0)
    sig["均线金叉6/12"] = S(ma6 > ma12)
    sig["价>12月均线"] = S(m > ma12)
    sig["动量+均线双确认"] = S((mom12 > 0) & (m > ma12))
    sig["波动率防守"] = pd.Series(np.where(volq < 0.5, 1.0, np.where(volq < 0.8, 0.5, 0.0)), index=m.index)
    return sig


def perf(r):
    r = r.dropna()
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1
    mdd = (nav / nav.cummax() - 1).min()
    sh = r.mean() / r.std(ddof=1) * np.sqrt(12) if r.std(ddof=1) > 0 else 0
    return ann, mdd, sh, nav.iloc[-1]


def evaluate(pos, eq):
    """T+1：月末信号 → 次月执行（pos.shift(1)），起始满仓"""
    r = (eq * pos.shift(1).fillna(1.0)).dropna()
    return perf(r), r


def report(eq, sig, since):
    eqw = eq[eq.index >= since]
    rows = []
    # 基准
    a, d, sh, nav = perf(eqw)
    rows.append(("满仓基准", a, d, sh, nav, None))
    for name, pos in sig.items():
        posw = pos[eq.index >= since]
        (a, d, sh, nav), r = evaluate(posw, eqw)
        n_out = int((posw == 0.0).sum())
        sw = int((posw.diff() != 0).sum())
        rows.append((name, a, d, sh, nav, f"{n_out}月空/{sw}调"))
    return rows


def main():
    eq = load_eq()
    m = load_hs300()
    sig = build_signals(m)
    # 对齐 eq 索引
    sig = {k: v.reindex(eq.index) for k, v in sig.items()}

    for since, title in [("2019-08", "2019-08 至今（原 18.3% 窗口，数据可靠）"),
                         ("2010-02", "2010-02 至今（含回填历史，长周期）")]:
        print("=" * 78)
        print(f"【{title}】T+1 执行")
        print("=" * 78)
        rows = report(eq, sig, since)
        print(f"{'信号':<18}{'年化':>8}{'最大回撤':>10}{'夏普':>7}{'净值':>8}   {'备注':<16}")
        for name, a, d, sh, nav, note in rows:
            note_s = note or ""
            marker = ""
            if name == "满仓基准":
                marker = " ← 基准"
            print(f"{name:<18}{a*100:>+7.1f}%{d*100:>9.1f}%{sh:>7.2f}{nav:>8.2f}   {note_s}{marker}")
        print()


if __name__ == "__main__":
    main()
