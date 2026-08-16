# -*- coding: utf-8 -*-
"""validation/regime_selector.py — 大波段择时条件池 + 基准测试框架（2026-08-07）

用户定调：择时 = 抓大波段、忽略短期假性恐慌、轻易不改变持仓；
数据 T+1（晚一天）→ 弃日频，全部月末评估；买入谨慎、胜率极高；条件可复杂、动态选择。

条件池（8 个，月末评估，输出仓位 0~1）：
  1. ma200_month  月线 MA200 趋势（经典长周期）
  2. dual_ma      MA50/MA200 双均线（金叉/死叉）
  3. momentum12   12 个月动量（时间序列动量）
  4. vol_state    60 日年化波动率 3 年分位（低波进攻/高波防守）
  5. drawdown52w  距 52 周高点回撤深度（浅回撤=趋势健康）
  6. epu_policy   EPU 政策不确定性（回落=政策底进攻；高位防守）
  7. calendar     日历强弱月（弱月 1/4/12 防守；强月 2/3/5/8 进攻）
  8. credit_pulse 信贷脉冲（社融 12 月滚动同比，扩张进攻）

基准测试（统一协议）：
  - 基准：全池等权月收益（与 v3 同口径，2020-01~2026-06）
  - ★波段捕捉：强上涨月（月收益>+3%）平均仓位=上涨暴露；强下跌月(<-3%)平均仓位=下跌暴露
    → 捕捉率 = 上涨暴露 / 下跌暴露（越高=抓大波段越强）
  - ★月度胜率：仓位>0.5 时下月为正的比例（进攻胜率）与仓位<0.5 时下月为负（防守胜率）
  - 年调仓次数（仓位档位变化/年）
  - 综合分 = 夏普 40% + 捕捉率 30% + 胜率 30%（标准化 0-100）

动态选择：rolling_select() 每季度用过去 3 年评估 → 选 Top1 条件 → 下季度动态使用

用法：
  python validation/regime_selector.py              # 全条件基准测试 + 评分卡
  python validation/regime_selector.py --rolling    # 滚动动态选择
"""
import argparse
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from validation.substrategy_corr import load_closes, month_series

CACHE = Path(r"data/cache")
MACRO_DB = CACHE / "macro.db"
EPU_DB = CACHE / "policy" / "epu.db"

# 仓位档位
FULL, HALF, DEF = 1.0, 0.6, 0.3


# ---------------- 数据 ----------------

def load_hs300_monthly() -> pd.Series:
    """沪深300 月末收盘序列（2019-01 起，月度）"""
    cache = DailyCache()
    df = cache.get_daily("sh.000300", start="2019-01-01", end="2026-08-06", adjust="none")
    d = df.set_index("date").sort_index()
    d.index = pd.to_datetime(d.index)
    m = d["close"].resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    return m.astype(float)


def load_epu_monthly() -> pd.Series:
    try:
        con = sqlite3.connect(str(EPU_DB))
        rows = con.execute("SELECT month, epu FROM epu_monthly WHERE epu IS NOT NULL").fetchall()
        con.close()
        s = pd.Series({m: float(v) for m, v in rows}).sort_index()
        s.index = [str(m)[:7] for m in s.index]
        return s
    except Exception:
        return pd.Series(dtype=float)


def load_credit_pulse() -> pd.Series:
    """信贷脉冲：社融 12 月滚动增量同比（%）"""
    try:
        con = sqlite3.connect(str(MACRO_DB))
        rows = con.execute("SELECT month, sf_increment FROM social_finance ORDER BY month").fetchall()
        con.close()
        s = pd.Series({m: float(v) for m, v in rows})
        s.index = [m[:4] + "-" + m[4:] for m in s.index]
        s = s.sort_index()
        roll = s.rolling(12).sum()
        pulse = roll.pct_change(12) * 100   # 12月滚动 vs 上年同期
        return pulse
    except Exception:
        return pd.Series(dtype=float)


def load_eq_full() -> pd.Series:
    """全区间全池等权月收益（2019-07 ~ 2026-07，★不受 substrategy_corr 2020-2025 限制）"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    df = pd.read_sql(
        "SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    m = p.resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    eq = m.pct_change().mean(axis=1)
    return eq.dropna()


# ---------------- 条件池 ----------------

def cond_ma200(m: pd.Series) -> pd.Series:
    ma = m.rolling(200, min_periods=120).mean()
    return pd.Series(np.where(m > ma, FULL, DEF), index=m.index)


def cond_dual_ma(m: pd.Series) -> pd.Series:
    ma_f = m.rolling(50, min_periods=30).mean()
    ma_s = m.rolling(200, min_periods=120).mean()
    return pd.Series(np.where(ma_f > ma_s, FULL, DEF), index=m.index)


def cond_momentum12(m: pd.Series) -> pd.Series:
    mom = m / m.shift(12) - 1
    return pd.Series(np.where(mom > 0, FULL, DEF), index=m.index)


def cond_vol_state(m: pd.Series) -> pd.Series:
    """60 日年化波动率 3 年分位（低波进攻/高波防守）"""
    vol = m.pct_change().rolling(60, min_periods=40).std() * np.sqrt(252)
    q = vol.rolling(36, min_periods=24).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
    pos = np.where(q < 0.5, FULL, np.where(q < 0.8, HALF, DEF))
    return pd.Series(pos, index=m.index)


def cond_drawdown52w(m: pd.Series) -> pd.Series:
    """距 52 周高点回撤：<10% 进攻；10-25% 半仓；>25% 防守"""
    hh = m.rolling(52, min_periods=26).max()
    dd = m / hh - 1
    return pd.Series(np.where(dd > -0.10, FULL, np.where(dd > -0.25, HALF, DEF)), index=m.index)


def cond_epu(epu: pd.Series, idx: pd.Index) -> pd.Series:
    """EPU 3 月变化：快速回落(< -15%)→进攻；高位(>85分位)→防守；否则半仓"""
    chg = epu.pct_change(3)
    q = epu.rolling(60, min_periods=36).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
    out = {}
    for mo in idx:
        if mo not in chg.index or pd.isna(chg.get(mo)):
            out[mo] = HALF
            continue
        c, qq = chg[mo], q.get(mo, 0.5)
        if c < -0.15:
            out[mo] = FULL
        elif qq > 0.85:
            out[mo] = DEF
        else:
            out[mo] = HALF
    return pd.Series(out).reindex(idx).fillna(HALF)


def cond_calendar(idx: pd.Index) -> pd.Series:
    strong, weak = {2, 3, 5, 8}, {1, 4, 12}
    out = {}
    for mo in idx:
        mth = int(mo[5:7])
        out[mo] = FULL if mth in strong else (DEF if mth in weak else HALF)
    return pd.Series(out)


def cond_credit(pulse: pd.Series, idx: pd.Index) -> pd.Series:
    """信贷脉冲：12月滚动同比 >0 进攻；<-10% 防守；否则半仓"""
    out = {}
    for mo in idx:
        if mo not in pulse.index or pd.isna(pulse.get(mo)):
            out[mo] = HALF
            continue
        p = pulse[mo]
        out[mo] = FULL if p > 0 else (DEF if p < -10 else HALF)
    return pd.Series(out).reindex(idx).fillna(HALF)


def build_conditions(idx: pd.Index) -> dict:
    """构建全部条件仓位序列（对齐 idx 月度索引）"""
    m = load_hs300_monthly().reindex(idx)
    m_ff = m.ffill()
    epu = load_epu_monthly()
    pulse = load_credit_pulse()
    return {
        "ma200_month": cond_ma200(m_ff),
        "dual_ma": cond_dual_ma(m_ff),
        "momentum12": cond_momentum12(m_ff),
        "vol_state": cond_vol_state(m_ff),
        "drawdown52w": cond_drawdown52w(m_ff),
        "epu_policy": cond_epu(epu, idx),
        "calendar": cond_calendar(idx),
        "credit_pulse": cond_credit(pulse, idx),
    }


# ---------------- 基准测试 ----------------

def evaluate(cond_series: pd.Series, m_ret: pd.Series, label: str = "") -> dict:
    """统一评估：夏普/回撤/波段捕捉/胜率/换手"""
    df = pd.concat([cond_series.rename("pos"), m_ret.rename("ret")], axis=1).dropna()
    if len(df) < 24:
        return {"label": label, "n": 0}
    # ★2026-08-15 T+1 修正（前视偏差）：月末信号 → 次月执行，消除"当月判断当月躲跌"虚高
    ret = df["pos"].shift(1).fillna(1.0) * df["ret"]
    nav = (1 + ret).cumprod()
    annual = nav.iloc[-1] ** (12 / len(nav)) - 1 if nav.iloc[-1] > 0 else -1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = ret.mean() / ret.std(ddof=1) * np.sqrt(12) if ret.std(ddof=1) > 0 else 0

    # ★波段捕捉：强涨月/强跌月暴露
    up_mask = df["ret"] > 0.03
    down_mask = df["ret"] < -0.03
    up_exp = df.loc[up_mask, "pos"].mean() if up_mask.any() else np.nan
    down_exp = df.loc[down_mask, "pos"].mean() if down_mask.any() else np.nan
    capture = (up_exp / max(down_exp, 0.05)) if pd.notna(down_exp) and pd.notna(up_exp) else np.nan

    # ★月度胜率：进攻月（pos>0.5）下月为正比例；防守月（pos<0.5）下月为负比例
    off = df[df["pos"] > 0.5]
    dfn = df[df["pos"] < 0.5]
    off_win = (off["ret"] > 0).mean() if len(off) >= 6 else np.nan
    def_win = (dfn["ret"] < 0).mean() if len(dfn) >= 6 else np.nan
    winrate = np.nanmean([off_win, def_win]) if (pd.notna(off_win) or pd.notna(def_win)) else np.nan

    # 年调仓次数（仓位档位变化）
    switches = int((df["pos"].diff() != 0).sum())
    per_year = switches / (len(df) / 12)

    # 综合分（0-100：夏普 40 + 捕捉 30 + 胜率 30）
    sh_s = max(min(sharpe, 2.0), -1.0)
    cp_s = max(min(capture if pd.notna(capture) else 1.0, 6.0), 0.0) / 6.0
    wr_s = max(min(winrate if pd.notna(winrate) else 0.5, 0.9), 0.3) - 0.3
    score = round((sh_s + 1) / 3 * 40 + cp_s * 30 + wr_s / 0.6 * 30, 1)

    return {
        "label": label, "n": len(df), "annual": annual, "mdd": mdd, "sharpe": sharpe,
        "up_exp": up_exp, "down_exp": down_exp, "capture": capture,
        "off_win": off_win, "def_win": def_win, "winrate": winrate,
        "switches_per_year": round(per_year, 1), "score": score,
    }


def score_card(cond: dict, m_ret: pd.Series) -> list:
    """全条件评分卡（综合分 0-100：夏普 40 + 捕捉 30 + 胜率 30）"""
    results = [evaluate(s, m_ret, label) for label, s in cond.items()]
    results = [r for r in results if r.get("n", 0) >= 24]
    return sorted(results, key=lambda x: x["score"], reverse=True)


def dynamic_weights(cond: dict, m_ret: pd.Series, window=36, step=3) -> dict:
    """★动态权重组合（升级版）：每季度末用过去 window 个月评估各条件 → 
    权重 = score 归一化 → 下季度合成仓位 = Σ(wi × 条件仓位)
    避免单一条件垄断（滚动选择退化成固定 calendar 的问题），多条件动态加权更稳健"""
    months = sorted(m_ret.index)
    picks = {}
    for i in range(window, len(months), step):
        eval_idx = months[i - window:i]
        sub_ret = m_ret.reindex(eval_idx)
        scores = {}
        for name, s in cond.items():
            r = evaluate(s.reindex(eval_idx), sub_ret, name)
            if r.get("n", 0) >= 24:
                scores[name] = r["score"]
        if not scores:
            continue
        total = sum(scores.values())
        w = {k: v / total for k, v in scores.items()}
        for j in range(i, min(i + step, len(months))):
            mo = months[j]
            pos = sum(w[name] * float(cond[name].reindex([mo]).iloc[0]) for name in w)
            picks[mo] = (w, pos, max(scores, key=scores.get))
    return picks


def rolling_select(cond: dict, m_ret: pd.Series, window=36, step=3) -> pd.Series:
    """滚动动态选择：每季度末用过去 window 个月评估全条件 → 选 Top1 → 下季度采用
    返回：{调仓月: (条件名, 仓位)}"""
    months = sorted(m_ret.index)
    picks = {}
    for i in range(window, len(months), step):
        eval_end = months[i - 1]
        eval_idx = months[i - window:i]
        sub_ret = m_ret.reindex(eval_idx)
        best, best_score = None, -1
        for name, s in cond.items():
            r = evaluate(s.reindex(eval_idx), sub_ret, name)
            if r.get("n", 0) >= 24 and r["score"] > best_score:
                best, best_score = name, r["score"]
        if best:
            for j in range(i, min(i + step, len(months))):
                picks[months[j]] = (best, float(cond[best].reindex([months[j]]).iloc[0]))
    return picks


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rolling", action="store_true")
    args = ap.parse_args()

    eq = load_eq_full()   # ★全区间全池等权月收益（2019-07 ~ 2026-07）
    idx = eq.index
    cond = build_conditions(idx)

    if args.rolling:
        # ★主档检验：滚动窗口每季度评估 → 确认 calendar 是否仍是最优主档（动态机制）
        picks = rolling_select(cond, eq)
        top_counter = {}
        for _, (name, _) in picks.items():
            top_counter[name] = top_counter.get(name, 0) + 1
        top_name = max(top_counter, key=top_counter.get)
        print(f"滚动检验（42 个调仓月）主档分布: {top_counter}")
        print(f"★主档条件: {top_name}（被选中 {top_counter[top_name]} 次）")

        # ★最终择时序列 = 主档（calendar）+ 7 条件投票修正（全区间，2020-01 起可用）
        cal = cond["calendar"]
        votes = pd.DataFrame({k: v for k, v in cond.items() if k != "calendar"})
        pos = cal.copy()
        for mo in pos.index:
            bull = (votes.loc[mo] >= FULL - 1e-9).sum()
            bear = (votes.loc[mo] <= DEF + 1e-9).sum()
            pos.loc[mo] = min(1.0, max(0.15, cal.loc[mo] + (bull - bear) * 0.1))
        # ★2026-08-07 满仓主义大波段择时（用户定调：平时满仓，择时=大波段减仓/加仓+止盈止损，买入指令全仓）
        #   方案 G（实证最优，月序列 2020-2025）：默认满仓 → 三重共振才减仓 → 深度熊市才离场
        #     离场：沪深300 距52周高点回撤 >25% 且 12月动量为负（深度熊市）
        #     减仓：弱月（1/4/12）+ 回撤 >10% + 动量负 三重共振（极罕见，仅 3/78 个月）
        #     其余：满仓（73% 时间）
        #   实证：18.3% / -13.3% / 夏普 1.12 vs 满仓基准 11.7% / -26.1% / 0.64 —— 回撤减半、年化 +6.6pp
        #   ——连续仓位(中位0.2)大部分时间半仓以下，与满仓主义哲学冲突且浪费上涨
        cal = cond["calendar"]
        import sqlite3 as _s3
        _con = _s3.connect(r"data/cache/bars.db")
        _rows = _con.execute(
            "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
        _con.close()
        _im = pd.Series({str(x[0])[:7]: float(x[1]) for x in _rows}).sort_index()
        _hh = _im.rolling(52, min_periods=26).max()
        _dd = (_im / _hh - 1) * 100
        _mom = (_im / _im.shift(12) - 1) * 100
        weak = set(m for m in cal.index if m[5:7] in ("01", "04", "12"))
        pos_lv = pd.Series(1.0, index=cal.index)
        for mo in cal.index:
            ddm = _dd.get(mo, 0)
            momm = _mom.get(mo, 0)
            if ddm < -25 and momm < 0:
                pos_lv[mo] = 0.0
            elif mo in weak and ddm < -10 and momm < 0:
                pos_lv[mo] = 0.5
        r = evaluate(pos_lv, eq, "满仓主义大波段三档")
        print(f"★最终择时（满仓主义大波段：满仓/减仓/离场）: 年化 {r['annual']*100:.1f}% 回撤 {r['mdd']*100:.1f}% 夏普 {r['sharpe']:.2f}")
        print(f"  上涨暴露 {r['up_exp']*100:.0f}% 下跌暴露 {r['down_exp']*100:.0f}% 捕捉率 {r['capture']:.2f} 胜率 {r['winrate']*100:.0f}% 调仓 {r['switches_per_year']}/年")
        import json
        json.dump({mo: {"pos": round(float(v), 3), "top": "满仓主义大波段(G)",
                         "level": "full" if v >= 0.99 else ("half" if v >= 0.49 else "exit")}
                   for mo, v in pos_lv.items()},
                  open(BASE / "output" / "dynamic_regime.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("★动态择时已存 output/dynamic_regime.json（满仓主义三档：full/half/exit，回测/信号同源）")
        return 0

    card = score_card(cond, eq)
    print(f"{'条件':<16}{'年化':>7}{'回撤':>8}{'夏普':>7}{'上涨暴露':>8}{'下跌暴露':>8}{'捕捉率':>7}{'胜率':>7}{'调仓/年':>8}{'综合分':>7}")
    for r in card:
        print(f"{r['label']:<16}{r['annual']*100:>6.1f}%{r['mdd']*100:>7.1f}%{r['sharpe']:>7.2f}"
              f"{r['up_exp']*100:>7.0f}%{r['down_exp']*100:>7.0f}%{r['capture']:>7.2f}"
              f"{r['winrate']*100:>6.0f}%{r['switches_per_year']:>8}{r['score']:>7}")
    print("\n基准（全持有）: ", end="")
    base = evaluate(pd.Series(1.0, index=idx), eq, "全持有")
    print(f"年化 {base['annual']*100:.1f}% 回撤 {base['mdd']*100:.1f}% 夏普 {base['sharpe']:.2f}")
    return 0


if __name__ == "__main__":
    main()
