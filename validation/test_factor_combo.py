# -*- coding: utf-8 -*-
"""
validation/test_factor_combo.py — ★多因子叠加筛选：有效性与统计学风险（2026-08-07）

研究问题：
  1. 因子池"有意义"的因子全部叠加，筛出的股票是否有意义（分层单调/超额/样本外）？
  2. 叠加过程有什么统计学风险（多重检验/数据窥探/过拟合）？

实验设计（月度截面，2020-2026，全市场 5500 只）：
  A. 8 因子等权合成（z-score）：
     技术面（反用=实证方向）：lowvol_60 / mom_120 / mom_20 / near_high_250 / new_high_250
     基本面：roe / nyoy / 加速度（sq_net_yoy 环比变化）
  B. 分层测试：综合分 5 分位 → 各层次季度收益 → 单调性 + Top层超额
  C. 样本外：2020-2022 vs 2023-2025 分段对比
  D. ★随机因子对照（多重检验演示）：20 组白噪声因子同样合成+分层，
     Top 层收益分布 vs 真实因子——若随机也能"选优"，即数据窥探风险实证
  E. 单因子 vs 合成 IC 对比（叠加是否真提升）

输出：output/factor_combo_report.json + 控制台摘要
"""
import json
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

CACHE = Path(r"data/cache")
OUT = BASE / "output" / "factor_combo_report.json"

TECH_FACTORS = ["lowvol_60", "mom_120", "mom_20", "near_high_250", "new_high_250"]
FUND_FACTORS = ["roe", "nyoy", "accel"]
ALL_FACTORS = TECH_FACTORS + FUND_FACTORS
# 实证方向（1=因子值越大越好；-1=反用）
DIRECTION = {f: -1 for f in TECH_FACTORS}   # 低波/超跌/距高点远 → 好（动量反向实证）
DIRECTION.update({"roe": 1, "nyoy": 1, "accel": 1})


def load_daily():
    """全市场日线 close（qfq）→ 日面板"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    df = pd.read_sql(
        "SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND date>='2019-06-01' AND close>0",
        con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    return p


def load_fund(end="2026-08-07"):
    """基本面（PIT 报告期对齐季度末）：roe/nyoy（小数）+ 加速度"""
    con = sqlite3.connect(str(CACHE / "finance.db"))
    rows = con.execute(
        "SELECT code, period, roe, sq_net_yoy FROM finance_report "
        "WHERE period>='2020-03-31'").fetchall()
    con.close()
    f = pd.DataFrame(rows, columns=["code", "period", "roe", "nyoy"])
    f["code"] = f["code"].str.upper()
    f["code"] = f["code"].apply(lambda c: c if "." in c else c + (".SH" if c[:2] in ("60", "68") else ".SZ"))
    f["accel"] = f.groupby("code")["nyoy"].diff()      # 加速度 = 同比变化
    f = f.set_index(["period", "code"]).sort_index()    # ★双索引：period 筛选 + code 截面唯一
    return f


def month_ends(px):
    """月末交易日列表（2020-2026）"""
    me = px.resample("ME").last().dropna(how="all").index
    return [d for d in me if 2020 <= d.year <= 2026]


def factor_panel(px, fund):
    """每个月末截面的 8 因子（全市场）× 方向修正 → {month: DataFrame(因子分)}"""
    close = px.astype(float)
    panels = {}
    # ★因子序列重采样到日历月末，与 month_ends 的 label 对齐
    vol60 = close.pct_change().rolling(60, min_periods=40).std().resample("ME").last()
    mom120 = (close / close.shift(120) - 1).resample("ME").last()
    mom20 = (close / close.shift(20) - 1).resample("ME").last()
    hi250 = close.rolling(250, min_periods=150).max().resample("ME").last()
    close_m = close.resample("ME").last()
    for d in month_ends(px):
        m = str(d)[:7]
        row = {}
        row["lowvol_60"] = -vol60.loc[d]                 # 已取反（低波=好）
        row["mom_120"] = -mom120.loc[d]                  # 动量反向
        row["mom_20"] = -mom20.loc[d]
        row["near_high_250"] = -(close_m.loc[d] / hi250.loc[d] - 1)  # 距高点远=好（反用）
        row["new_high_250"] = -((close_m.loc[d] / hi250.loc[d] - 1) > -0.02).astype(float) * 1.0
        df = pd.DataFrame(row)
        # 基本面（该月末最近已披露报告期：报告期 ≤ 月末 的最近一期截面，PIT 近似）
        valid = fund[fund.index.get_level_values("period") <= d.strftime("%Y-%m-%d")]
        if len(valid):
            last_p = valid.index.get_level_values("period").max()
            fund_m = valid.loc[last_p]            # DataFrame（index=code，该期全量）
            df["roe"] = fund_m["roe"].reindex(df.index)
            df["nyoy"] = fund_m["nyoy"].reindex(df.index)
            df["accel"] = fund_m["accel"].reindex(df.index)
        panels[m] = df
    return panels


def composite(panels, factors=ALL_FACTORS):
    """每期末：因子 z-score 等权合成 → 综合分 Series"""
    out = {}
    for m, df in panels.items():
        z = pd.DataFrame(index=df.index)
        for f in factors:
            if f not in df.columns:
                continue
            s = df[f]
            s = s.replace([np.inf, -np.inf], np.nan)
            mu, sd = s.mean(), s.std()
            if pd.notna(mu) and sd and sd > 0:
                z[f] = (s - mu) / sd
        z = z.dropna(axis=1)
        if z.empty:
            continue
        out[m] = z.mean(axis=1)
    return pd.DataFrame(out).T


def forward_ret(px, m):
    """月度收益（下月末/本月末-1）"""
    me = px.resample("ME").last()
    r = me.pct_change()
    r.index = [str(x)[:7] for x in r.index]
    return r


def layer_test(score_df, ret_df, label=""):
    """综合分 → 5 分层 → 次月收益统计（用当月分、下月收益）"""
    months = sorted(set(score_df.index) & set(ret_df.index))
    layers = {i: [] for i in range(1, 6)}
    top_excess, mono = [], []
    for i in range(len(months) - 1):
        m, nm = months[i], months[i + 1]
        s = score_df.loc[m].dropna()
        if len(s) < 100:
            continue
        q = pd.qcut(s.rank(method="first"), 5, labels=False) + 1
        r = ret_df.loc[nm]
        for L in range(1, 6):
            codes = s[q == L].index
            rv = r.reindex(codes).dropna()
            if len(rv):
                layers[L].append(rv.mean())
        # 单调性：Q5-Q1
        r5 = r.reindex(s[q == 5].index).dropna().mean()
        r1 = r.reindex(s[q == 1].index).dropna().mean()
        r_all = r.reindex(s.index).dropna().mean()
        if pd.notna(r5) and pd.notna(r1) and pd.notna(r_all):
            top_excess.append((r5 - r_all) * 100)
            mono.append(1 if r5 > r1 else 0)
    res = {"label": label, "n_months": len(months),
           "layer_avg": {f"Q{k}": round(np.mean(v) * 100, 2) if v else None for k, v in layers.items()},
           "Q5_Q1_spread": round((np.mean(layers[5]) - np.mean(layers[1])) * 100, 2) if layers[5] and layers[1] else None,
           "top_excess_avg": round(np.mean(top_excess), 2) if top_excess else None,
           "top_win": round(100 * np.mean([1 if x > 0 else 0 for x in top_excess]), 1) if top_excess else None,
           "monotonic_ratio": round(100 * np.mean(mono), 1) if mono else None}
    return res


def random_factor_test(px, fund, n_trials=20, seed=42):
    """★随机因子对照：白噪声因子同样分层，Top 层超额分布（多重检验演示）"""
    rng = np.random.default_rng(seed)
    r = forward_ret(px, None)
    months = [str(d)[:7] for d in month_ends(px)]
    me = px.resample("ME").last()
    me.index = [str(x)[:7] for x in me.index]
    excesses = []
    for t in range(n_trials):
        fake = pd.DataFrame(
            {c: rng.normal(0, 1, len(me)) for c in px.columns}, index=me.index)
        # 与真实同口径：月末截面分 5 层 → 次月收益
        exc = []
        for i in range(len(months) - 1):
            m, nm = months[i], months[i + 1]
            if m not in fake.index or nm not in me.index:
                continue
            s = fake.loc[m].dropna()
            if len(s) < 100:
                continue
            q = pd.qcut(s.rank(method="first"), 5, labels=False) + 1
            rv = r.loc[nm].reindex(s[q == 5].index).dropna()
            rall = r.loc[nm].reindex(s.index).dropna()
            if len(rv) and len(rall):
                exc.append((rv.mean() - rall.mean()) * 100)
        if exc:
            excesses.append(np.mean(exc))
    return {"n_trials": n_trials, "mean_excess": round(float(np.mean(excesses)), 2),
            "std": round(float(np.std(excesses)), 2),
            "max": round(float(np.max(excesses)), 2),
            "pct_positive": round(100 * np.mean([1 if x > 0 else 0 for x in excesses]), 1),
            "trials": [round(x, 2) for x in excesses]}


def main():
    print("加载日线面板 ...", flush=True)
    px = load_daily()
    print("加载基本面 ...", flush=True)
    fund = load_fund()
    print("计算因子面板 ...", flush=True)
    panels = factor_panel(px, fund)
    ret = forward_ret(px, None)
    print("合成 8 因子并分层测试 ...", flush=True)
    score_all = composite(panels)
    r_all = layer_test(score_all, ret, "8因子合成(全区间)")
    # 样本外分段
    score_1 = score_all[score_all.index < "2023-01"]
    score_2 = score_all[score_all.index >= "2023-01"]
    r1 = layer_test(score_1, ret, "2020-2022")
    r2 = layer_test(score_2, ret, "2023-2026")
    # 技术面 vs 基本面单独
    r_tech = layer_test(composite(panels, TECH_FACTORS), ret, "技术面5因子")
    r_fund = layer_test(composite(panels, FUND_FACTORS), ret, "基本面3因子")
    # 随机因子对照
    print("随机因子对照（20 组白噪声）...", flush=True)
    rnd = random_factor_test(px, fund, n_trials=20)
    report = {"all": r_all, "seg1": r1, "seg2": r2,
              "tech_only": r_tech, "fund_only": r_fund, "random": rnd,
              "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    # 控制台摘要
    print("\n=== 多因子叠加筛选实证 ===")
    for k in ("all", "seg1", "seg2", "tech_only", "fund_only"):
        r = report[k]
        print(f"{r['label']:<12}: 各层 {r['layer_avg']} | Q5-Q1 {r['Q5_Q1_spread']}pp | "
              f"Top超额 {r['top_excess_avg']}pp/月 胜率{r['top_win']}% 单调{r['monotonic_ratio']}%")
    print(f"\n=== 随机因子对照（多重检验演示）===")
    print(f"  20 组白噪声因子 Top 层超额: 均值 {rnd['mean_excess']}pp ± {rnd['std']}pp, "
          f"最大 {rnd['max']}pp, 正数占比 {rnd['pct_positive']}%")
    print(f"  真实因子 Top 超额: {r_all['top_excess_avg']}pp/月")
    return 0


if __name__ == "__main__":
    sys.exit(main())
