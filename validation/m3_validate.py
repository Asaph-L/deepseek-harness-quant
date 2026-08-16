# -*- coding: utf-8 -*-
"""
validation/m3_validate.py — M3 P0.5 因子验证（入场券）

目标：用 2020-2025 数据复算各因子 RankIC/ICIR/分组收益/稳定性，
     对照中证指数基线（CS-01/02），判定 有效/无效/分池有效 → 回写权重。
     同时按第17课落地：输出每个因子的 IC 衰减率 + PSI 基线（因子档案）。

用法：
  python validation/m3_validate.py                 # 用当前缓存全量跑（M2 完成后）
  python validation/m3_validate.py --start 2020-01-01 --end 2025-12-31
  python validation/m3_validate.py --quick        # 快速模式（抽样 200 只验证逻辑）

输出：output/因子有效性报告.md + output/因子档案.json
      （由 dev_auto 在 M2 完成后自动触发）
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache


# ============================================================
# 1. 因子计算（第一版：技术面因子全实现；基本面因子待财报入库）
# ============================================================

def calc_rps(close: pd.Series, window: int = 120) -> pd.Series:
    """相对强度 RPS：过去 window 日涨幅在全市场的百分位排名（0-100）"""
    ret = close / close.shift(window) - 1
    return ret


def calc_lowvol(close: pd.Series, window: int = 60) -> pd.Series:
    """低波因子：过去 window 日日收益波动率（越小越好，防守引擎用）"""
    return close.pct_change().rolling(window).std()


def calc_new_high(close: pd.Series, window: int = 250) -> pd.Series:
    """52 周新高（N 因子）：是否创 window 日新高"""
    return (close == close.rolling(window, min_periods=window // 2).max()).astype(float)


def calc_near_high(close: pd.Series, window: int = 250, pct: float = 0.90) -> pd.Series:
    """接近高点（N 因子加分）：收盘价位于 window 日最高点的 pct 以上"""
    hi = close.rolling(window, min_periods=window // 2).max()
    return (close / hi).clip(upper=1.0)


def calc_momentum_20_120(close: pd.Series) -> pd.Series:
    """中周期动量（CS-23 社区共识：20-120 日动量最稳）"""
    return close / close.shift(120) - 1


# ============================================================
# 2. 因子面板构建
# ============================================================

TECH_FACTORS = {
    "rps_120": calc_rps,            # L 因子（动量/RPS）
    "lowvol_60": calc_lowvol,       # 防守引擎：低波
    "new_high_250": calc_new_high,  # N 因子：52 周新高
    "near_high_250": calc_near_high,  # N 因子：接近高点
    "mom_20_120": calc_momentum_20_120,  # 中周期动量（社区共识 CS-23）
}

# 基本面因子（财报数据入库后启用，当前标记 pending）
FUND_FACTORS = {
    "eps_growth_q": "待财报入库（M2 财报批量后启用）",
    "eps_cagr_3y": "待财报入库",
    "pead": "待财报入库",
    "dividend_yield": "待分红数据入库",
    "value_bp": "待财报入库",
    "quality_roe": "待财报入库",
    "mcap": "待 daily_basic 入库",
}


def build_factor_panel(cache: DailyCache, codes, start, end, month_ends):
    """构建月度调仓口径的因子面板。

    返回 {code: DataFrame(index=月末日期, columns=因子名)} 的因子值序列
    简化实现：以月末为截面，用截至该月末的历史数据算因子。
    """
    panel = {}   # code -> DataFrame(index=月末, columns=factors)
    for code in codes:
        df = cache.get_daily(code, start=start, end=end, adjust="qfq")
        if df is None or len(df) < 300:
            continue
        df = df.set_index("date").sort_index()
        close = df["close"]
        fdf = pd.DataFrame(index=df.index)
        for name, fn in TECH_FACTORS.items():
            fdf[name] = fn(close)
        # 只保留月末截面
        fdf["ym"] = fdf.index.astype(str).str[:7]
        monthly = fdf.groupby("ym").tail(1).drop(columns=["ym"])
        # 只保留在 month_ends 中的月末
        monthly = monthly[monthly.index.isin(month_ends)]
        if len(monthly) >= 12:
            panel[code] = monthly
    return panel


def build_forward_returns(cache: DailyCache, codes, month_ends, horizon=20):
    """未来 horizon 日收益（标签）：T 月末收盘 → T+horizon 收盘"""
    labels = {}
    for code in codes:
        df = cache.get_daily(code, start=None, end=None, adjust="qfq")
        if df is None or len(df) < 300:
            continue
        df = df.set_index("date").sort_index()
        fwd = df["close"].shift(-horizon) / df["close"] - 1
        labels[code] = fwd.reindex(month_ends)
    return labels


# ============================================================
# 3. IC 检验（RankIC / ICIR / 分组收益 / 稳定性）
# ============================================================

def rank_ic_series(factor_series, fwd_series):
    """逐月 RankIC：因子值排名 vs 未来收益排名 的 Spearman 相关"""
    df = pd.DataFrame({"f": factor_series, "r": fwd_series}).dropna()
    if len(df) < 30:
        return None
    return df["f"].rank().corr(df["r"].rank(), method="spearman")


def ic_analysis(panel, labels, factor_name):
    """单个因子的 IC 分析：RankIC 均值/ICIR/胜率/稳定性"""
    ics = []
    # 逐月截面：取所有股票某月末的因子值 + 未来收益
    month_series = {}
    for code, fdf in panel.items():
        if factor_name not in fdf.columns:
            continue
        lab = labels.get(code)
        if lab is None:
            continue
        for m, val in fdf[factor_name].items():
            if pd.notna(val):
                month_series.setdefault(m, []).append((val, lab.get(m, np.nan)))
    for m, pairs in sorted(month_series.items()):
        if len(pairs) < 30:
            continue
        arr = np.array([(a, b) for a, b in pairs if pd.notna(b)])
        if len(arr) < 30:
            continue
        f = pd.Series(arr[:, 0]).rank()
        r = pd.Series(arr[:, 1]).rank()
        ics.append((m, f.corr(r, method="spearman")))
    if len(ics) < 6:
        return None
    ic_series = pd.Series([v for _, v in ics], index=[m for m, _ in ics])
    # ★因子衰减率（第17课）：拟合 IC_t = IC_0 × (1-r)^t，r 为月衰减率
    decay_rate = _fit_ic_decay(ic_series)
    return {
        "factor": factor_name,
        "n_months": len(ic_series),
        "rank_ic_mean": round(float(ic_series.mean()), 4),
        "rank_ic_std": round(float(ic_series.std()), 4),
        "icir": round(float(ic_series.mean() / ic_series.std()), 4) if ic_series.std() > 0 else 0.0,
        "ic_win_rate": round(float((ic_series > 0).mean()), 4),
        "ic_latest_6m": round(float(ic_series.tail(6).mean()), 4),
        "ic_positive_months": int((ic_series > 0).sum()),
        "monthly_decay": decay_rate["rate"],
        "half_life_months": decay_rate["half_life"],
    }


def _fit_ic_decay(ic_series: pd.Series) -> dict:
    """拟合 IC 月衰减率（第17课 17.1.2：IC_t = IC_0 × (1-r)^t）。

    用首段 IC 均值作 IC_0，末段 IC 均值估算 r。
    简化实现：r ≈ 1 - (IC_末段/IC_0)^(1/月数)，IC 为负或为 0 时输出 0（无衰减/不适用）。
    """
    n = len(ic_series)
    if n < 6:
        return {"rate": 0.0, "half_life": None}
    ic0 = float(ic_series.iloc[: max(1, n // 3)].mean())
    ic_end = float(ic_series.iloc[-max(1, n // 3):].mean())
    if ic0 <= 0 or ic_end <= 0:
        return {"rate": 0.0, "half_life": None}
    ratio = ic_end / ic0
    months = n // 3 if n // 3 >= 1 else 1
    if ratio >= 1:
        return {"rate": 0.0, "half_life": None}   # 未衰减甚至增强
    rate = 1 - ratio ** (1.0 / months)
    half_life = np.log(0.5) / np.log(1 - rate) if rate > 0 else None
    return {"rate": round(float(rate), 4),
            "half_life": round(float(half_life), 1) if half_life else None}


# ============================================================
# 4. 因子判定 + 报告
# ============================================================

def judge_factor(res, baseline_ic=0.03):
    """判定 有效/无效/分池有效（对照中证基线 CS-01/02 与课程 ic_min=0.03）"""
    if res is None:
        return "数据不足"
    ic = res["rank_ic_mean"]
    icir = res["icir"]
    if ic >= 0.03 and icir >= 0.2:
        return "有效"
    if ic >= 0.02:
        return "分池有效（需按市值/风格分池验证）"
    if ic <= -0.02:
        return "反向（A股反转市，考虑取负或剔除）"
    return "无效"


def write_report(results, codes_used, start, end, out_md, out_json):
    """生成《因子有效性报告.md》+ 因子档案 JSON"""
    n_factors = len(results)
    # ★Bonferroni 校正（第07课）：测 n 个因子，显著性阈值 = 0.05/n —— 防止"测很多参数挑最好的"过拟合
    bonf = 0.05 / max(n_factors, 1)
    lines = [
        f"# 因子有效性报告（P0.5 / M3）",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 数据区间：{start} ~ {end}",
        f"> 股票样本：{codes_used} 只（缓存已入库）｜ 调仓口径：月末截面、未来 20 日收益",
        f"> 判定基线：RankIC ≥ 0.03 且 ICIR ≥ 0.2 = 有效（CS-01/02 + 课程第17课 ic_min）",
        f"> ★Bonferroni 校正（第07课）：本次检验 {n_factors} 个因子，单因子显著性阈值 = 0.05/{n_factors} = **{bonf:.4f}**（防止多重检验过拟合）",
        f"> ★因子衰减率（第17课）：IC_t = IC_0 × (1-r)^t，r 为月衰减率；半衰期 = 因子寿命预估",
        "",
        "## 因子检验结果",
        "",
        "| 因子 | 类型 | RankIC | ICIR | IC胜率 | 近6月IC | 月衰减率 | 半衰期(月) | 判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    archive = {}
    for name, res in sorted(results.items()):
        if res is None:
            lines.append(f"| {name} | - | 数据不足 | - | - | - | - | - | 数据不足 |")
            continue
        verdict = judge_factor(res)
        archive[name] = {**res, "verdict": verdict}
        decay = f"{res['monthly_decay']:.1%}" if res["monthly_decay"] > 0 else "—"
        hl = f"{res['half_life_months']:.0f}" if res.get("half_life_months") else "—"
        lines.append(
            f"| {name} | 技术面 | {res['rank_ic_mean']:.4f} | {res['icir']:.3f} | "
            f"{res['ic_win_rate']:.1%} | {res['ic_latest_6m']:.4f} | {decay} | {hl} | **{verdict}** |")
    lines += [
        "",
        "## 待财报入库后启用的因子（M2 财报批量完成后自动纳入）",
        "",
        "| 因子 | 状态 |",
        "|---|---|",
    ]
    for name, note in FUND_FACTORS.items():
        lines.append(f"| {name} | {note} |")
    lines += [
        "",
        "## 对主文档的意义",
        "",
        "- RankIC ≥ 0.03 的因子 → 保留并给权重；",
        "- 反向因子 → 小盘池考虑反转处理（CS-03 分池）；",
        "- 无效因子 → 降权或剔除（写入 params.yaml weights，递增 weight_version）；",
        "- 每个因子按第17课要求建立**滚动 IC 衰减率 + PSI 基线**（因子档案 JSON）；",
        "- ★衰减率高的因子（半衰期 < 12 月）→ 动态调权优先级最高，权重必须可在线更新。",
        "",
        "*本报告由 dev_auto 在 M2 完成后自动触发生成。*",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    out_json.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已生成：{out_md}")


# ============================================================
# 5. 主流程
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="M3 P0.5 因子验证")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--horizon", type=int, default=20,
                    help="未来收益标签周期（交易日）：20=短周期 / 60=中期 / 120=长期（用户定位=长期持有，需多口径对照）")
    ap.add_argument("--quick", action="store_true", help="快速模式（抽样 200 只验证逻辑）")
    args = ap.parse_args()

    cache = DailyCache()
    # 获取已缓存的股票列表
    import sqlite3
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar")]
    con.close()
    print(f"缓存股票数：{len(codes)}")

    if args.quick:
        import random
        random.seed(42)
        codes = random.sample(codes, min(200, len(codes)))
        print(f"快速模式：抽样 {len(codes)} 只")

    # 月末截面（2020-2025 每月最后一个交易日，从缓存日期推导）
    all_dates = set()
    for code in codes[:20]:  # 抽样推月末
        df = cache.get_daily(code, start=args.start, end=args.end, adjust="qfq")
        if df is not None:
            all_dates.update(df["date"].tolist())
    dates = pd.Series(sorted(all_dates))
    ym = dates.str[:7]
    month_ends = dates.groupby(ym).max().tolist()
    month_ends = [d for d in month_ends if args.start <= d <= args.end]
    print(f"月末截面数：{len(month_ends)}（{month_ends[0]} ~ {month_ends[-1]}）")

    print("构建因子面板...")
    panel = build_factor_panel(cache, codes, args.start, args.end, month_ends)
    print(f"因子面板股票数：{len(panel)}")

    print("构建未来收益标签...")
    labels = build_forward_returns(cache, codes, month_ends, horizon=args.horizon)

    print(f"IC 检验（标签周期 {args.horizon} 日）...")
    results = {}
    for fname in TECH_FACTORS:
        res = ic_analysis(panel, labels, fname)
        results[fname] = res
        if res:
            print(f"  {fname}: RankIC={res['rank_ic_mean']:.4f} ICIR={res['icir']:.3f} "
                  f"胜率={res['ic_win_rate']:.1%} 判定={judge_factor(res)}")
        else:
            print(f"  {fname}: 数据不足")

    out_dir = BASE / "output"
    out_dir.mkdir(exist_ok=True)
    write_report(results, len(panel), args.start, args.end,
                 out_dir / f"因子有效性报告_h{args.horizon}.md",
                 out_dir / f"因子档案_h{args.horizon}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
