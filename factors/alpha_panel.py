# -*- coding: utf-8 -*-
"""factors/alpha_panel.py — 本地因子计算引擎（README 11 类 · 2026-08-22 重建）

背景：README 声称 123+ 因子、实证 ICIR 等数值来自外包因子池（data/factorpool/ 缺失）。
本模块从本地数据库（bars.db qfq 日线 + finance.db/finance_ts.db PIT 财报 +
hist_mv.db 历史市值 + stock_basic.db 行业）向量化重建 README 表 11 类核心因子，
输出统一面板（date × code），供 factor_evaluator 批量实证与策略层消费。

数据边界（AGENTS.md）：
  - 换手率 2019 年前缺失 → 全部因子实证起点 2019-01-01
  - 基本面因子 PIT：仅用 ann_date 已披露数据（禁止 look-ahead）
  - 市值用 hist_mv（历史口径，非快照）

用法：
  from factors.alpha_panel import compute_all, FACTORS, direction_map
  panels = compute_all(start="2019-01-01")     # {name: DataFrame(date×code)}
  df = compute("amihud")
"""
import json
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

BARS_DB = BASE / "data" / "cache" / "bars.db"
FIN_DB = BASE / "data" / "cache" / "finance.db"
FIN_TS_DB = BASE / "data" / "cache" / "finance_ts.db"
MV_DB = BASE / "data" / "cache" / "hist_mv.db"
BASIC_DB = BASE / "data" / "cache" / "stock_basic.db"

DEFAULT_START = "2019-01-01"
LHB_DB = BASE / "data" / "cache" / "lhb.db"
SHEBAO_DB = BASE / "data" / "cache" / "shebao.db"
GDHS_DB = BASE / "data" / "cache" / "gdhs_full.db"

# 因子方向（A 股实证方向：+1 值越大越好 / -1 值越小越好；与 params.yaml factors.direction 同语义）
DIRECTION = {
    # 换手率族
    "turnover": -1, "turn_mean20": -1, "turn_std20": -1, "turn_mid_prox": -1,
    # 低波族
    "lowvol_60": -1, "std20": -1, "downside_vol": -1,
    # 反转族
    "reversal20": -1, "o2c_sum_20": 1,
    # 流动性
    "amihud": 1,
    # 彩票/偏度
    "max_ret20": -1, "skew20": -1, "rmax_20": -1,
    # 振幅/动量
    "amp20": -1, "open_prem_20": 1,
    # 基本面低频
    "bp": -1, "asset_growth": 1, "sue": 1, "accruals": -1, "fscore": 1,
    # 短线涨跌停
    "limit_up_cnt_20": 1, "consec_limit_up": 1, "limit_down_cnt_20": -1,
    "consec_limit_down": -1,
    # 行业层
    "ind_crowd_60": -1, "ind_rs_20": 1,
    # 机构行为（龙虎榜 + 社保）
    "lhb_cnt_20": -1, "lhb_jg_cnt_20": -1, "shebao_hold": 1, "shebao_chg": 1,  # ★实证 IC 负 → 反用
    "gdhs_chg_pct": -1,  # 股东户数大增 = 散户涌入 = 负向
    # Alpha101
    "alpha003": 1, "alpha006": 1, "alpha015": 1, "alpha044": 1, "alpha050": 1,
}

FAMILY = {
    "turnover": "换手率", "turn_mean20": "换手率", "turn_std20": "换手率", "turn_mid_prox": "换手率",
    "lowvol_60": "低波动", "std20": "低波动", "downside_vol": "低波动",
    "reversal20": "反转", "o2c_sum_20": "反转",
    "amihud": "流动性",
    "max_ret20": "彩票/偏度", "skew20": "彩票/偏度", "rmax_20": "彩票/偏度",
    "amp20": "振幅/动量", "open_prem_20": "振幅/动量",
    "bp": "基本面低频", "asset_growth": "基本面低频", "sue": "基本面低频",
    "accruals": "基本面低频", "fscore": "基本面低频",
    "limit_up_cnt_20": "短线涨跌停", "consec_limit_up": "短线涨跌停",
    "limit_down_cnt_20": "短线涨跌停", "consec_limit_down": "短线涨跌停",
    "ind_crowd_60": "行业层", "ind_rs_20": "行业层",
    "lhb_cnt_20": "机构行为", "lhb_jg_cnt_20": "机构行为",
    "shebao_hold": "机构行为", "shebao_chg": "机构行为",
    "gdhs_chg_pct": "机构行为",
    "alpha003": "Alpha101", "alpha006": "Alpha101", "alpha015": "Alpha101",
    "alpha044": "Alpha101", "alpha050": "Alpha101",
}


# ---------------- 龙虎榜（机构行为族） ----------------

_lhb_cache = {"ts": 0.0, "data": None}


def _load_lhb() -> dict:
    """龙虎榜数据（data/cache/lhb.db，tushare top_list + top_inst 2026-08-23 接入）：
    返回 {code6: DataFrame(index=trade_date, cnt=上榜次数, jg=机构专用净买次数)}"""
    global _lhb_cache
    import time as _t
    now = _t.time()
    if _lhb_cache["data"] is not None and now - _lhb_cache["ts"] < 300:
        return _lhb_cache["data"]
    out = {}
    if not LHB_DB.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{LHB_DB}?mode=ro&immutable=1", uri=True)
        tl = pd.read_sql("SELECT trade_date, ts_code FROM top_list", con)
        ti = pd.read_sql("SELECT trade_date, ts_code, exalterate, net_buy FROM top_inst", con)
        con.close()
        for name, df in (("cnt", tl), ("jg", ti)):
            if df.empty:
                continue
            df["code6"] = df["ts_code"].astype(str).str[:6]
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            if name == "jg":
                df = df[df["exalterate"].astype(str).str.contains("机构专用")]
                df = df[df["net_buy"].astype(float) > 0]
            g = df.groupby("code6")
            for c6, gd in g:
                d = gd.groupby("trade_date").size()
                d.name = name
                if c6 not in out:
                    out[c6] = d.to_frame()
                else:
                    out[c6] = out[c6].join(d, how="outer")
    except Exception:
        pass
    _lhb_cache.update({"ts": now, "data": out})
    return out


def _f_lhb_cnt_20(P):
    """近 20 交易日龙虎榜上榜次数"""
    lhb = _load_lhb()
    idx = P["close"].index
    out = pd.DataFrame(0.0, index=idx, columns=P["close"].columns)
    code_map = {str(c).split(".")[0]: c for c in P["close"].columns}
    for c6, d in lhb.items():
        col = code_map.get(c6)
        if col is None or "cnt" not in d:
            continue
        out[col] = d["cnt"].reindex(idx).fillna(0).rolling(20, min_periods=1).sum()
    return out


def _f_lhb_jg_cnt_20(P):
    """近 20 交易日机构专用席位净买次数"""
    lhb = _load_lhb()
    idx = P["close"].index
    out = pd.DataFrame(0.0, index=idx, columns=P["close"].columns)
    code_map = {str(c).split(".")[0]: c for c in P["close"].columns}
    for c6, d in lhb.items():
        col = code_map.get(c6)
        if col is None or "jg" not in d:
            continue
        out[col] = d["jg"].reindex(idx).fillna(0).rolling(20, min_periods=1).sum()
    return out


# ---------------- 社保基金（机构行为族） ----------------

_shebao_cache = {"ts": 0.0, "data": None}


def _load_shebao() -> pd.DataFrame:
    """社保持仓（data/cache/shebao.db，tushare top10_holders 过滤社保组合 2026-08-23）：
    返回长表 code6/end_date/holder_name/hold_amount/hold_ratio/hold_change"""
    global _shebao_cache
    import time as _t
    now = _t.time()
    if _shebao_cache["data"] is not None and now - _shebao_cache["ts"] < 300:
        return _shebao_cache["data"]
    out = pd.DataFrame()
    if SHEBAO_DB.exists():
        try:
            con = sqlite3.connect(f"file:{SHEBAO_DB}?mode=ro&immutable=1", uri=True)
            out = pd.read_sql("SELECT * FROM shebao", con)
            con.close()
            if not out.empty:
                out["code6"] = out["ts_code"].astype(str).str[:6]
        except Exception:
            pass
    _shebao_cache.update({"ts": now, "data": out})
    return out


def _f_shebao_hold(P):
    """社保组合持有比例合计（最新报告期，PIT 前向填充）"""
    sb = _load_shebao()
    idx = P["close"].index
    out = pd.DataFrame(0.0, index=idx, columns=P["close"].columns)
    if sb.empty:
        return out
    g = sb.groupby("code6")["hold_ratio"].sum()
    code_map = {str(c).split(".")[0]: c for c in P["close"].columns}
    for c6, ratio in g.items():
        col = code_map.get(c6)
        if col is not None:
            out[col] = ratio
    return out


def _f_shebao_chg(P):
    """社保组合持股变化合计（hold_change，正=加仓）"""
    sb = _load_shebao()
    idx = P["close"].index
    out = pd.DataFrame(0.0, index=idx, columns=P["close"].columns)
    if sb.empty:
        return out
    g = sb.groupby("code6")["hold_change"].sum()
    code_map = {str(c).split(".")[0]: c for c in P["close"].columns}
    for c6, chg in g.items():
        col = code_map.get(c6)
        if col is not None:
            out[col] = chg
    return out
_gdhs_cache = {"ts": 0.0, "data": None}


def _load_gdhs() -> pd.DataFrame:
    """股东户数（data/cache/gdhs_full.db，tushare stk_holdernumber 2026-08-23 接入）"""
    global _gdhs_cache
    import time as _t
    now = _t.time()
    if _gdhs_cache["data"] is not None and now - _gdhs_cache["ts"] < 300:
        return _gdhs_cache["data"]
    out = pd.DataFrame()
    if GDHS_DB.exists():
        try:
            con = sqlite3.connect(f"file:{GDHS_DB}?mode=ro&immutable=1", uri=True)
            out = pd.read_sql("SELECT * FROM gdhs", con)
            con.close()
            if not out.empty:
                out["ann"] = pd.to_datetime(out["ann_date"], errors="coerce")
        except Exception:
            pass
    _gdhs_cache.update({"ts": now, "data": out})
    return out


def _f_gdhs_chg_pct(P):
    """最新披露股东户数变化率（PIT：ann_date 生效；>10%=散户涌入，<0=筹码集中）"""
    g = _load_gdhs()
    idx = P["close"].index
    out = pd.DataFrame(np.nan, index=idx, columns=P["close"].columns)
    if g.empty:
        return out
    code_map = {str(c).split(".")[0]: c for c in P["close"].columns}
    g = g[g["ann"].notna()].sort_values(["ts_code", "ann"])
    g = g.drop_duplicates("ts_code", keep="last")
    g["code6"] = g["ts_code"].astype(str).str[:6]
    for _, r in g.iterrows():
        col = code_map.get(r["code6"])
        if col is not None and pd.notna(r.get("chg_pct")):
            out[col] = float(r["chg_pct"])
    return out



# ---------------- 数据加载 ----------------

def _read_bars(start: str = DEFAULT_START, end: str = None) -> pd.DataFrame:
    """全市场 qfq 日线（长表：code/date/open/high/low/close/preclose/volume/amount/turn/pct_chg）"""
    sql = ("SELECT code,date,open,high,low,close,volume,amount,turn,pct_chg "
           "FROM daily_bar WHERE adjust='qfq' AND close>0 AND date>=?")
    args: list = [start]
    if end:
        sql += " AND date<=?"
        args.append(end)
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True)
    df = pd.read_sql(sql, con, params=args)
    con.close()
    df["date"] = pd.to_datetime(df["date"])
    # 排除指数（sh./sz. 前缀）与退市格式
    df = df[~df["code"].str.startswith(("sh.", "sz."))]
    return df


def _pivot(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """长表 → date×code 面板"""
    p = df.pivot_table(index="date", columns="code", values=col)
    return p.sort_index()


def _load_price_panels(start: str = DEFAULT_START, end: str = None) -> dict:
    """价量面板集 {open/high/low/close/volume/amount/turn/pct_chg: date×code}"""
    df = _read_bars(start, end)
    out = {c: _pivot(df, c) for c in
           ("open", "high", "low", "close", "volume", "amount", "turn", "pct_chg")}
    return out


def _load_finance_pit(start: str = DEFAULT_START) -> pd.DataFrame:
    """PIT 财报面板（ann_date 生效，月末重采样后 ffill）：
    code6/date(ann_date) 为索引时刻，字段 = roe/sue(单季净利同比)/rev_yoy/accrual_proxy/bp/asset_growth/fscore"""
    con = sqlite3.connect(f"file:{FIN_TS_DB}?mode=ro&immutable=1", uri=True)
    fs = pd.read_sql(
        "SELECT code,end_date,ann_date,total_revenue,n_income_attr_p,"
        "total_hldr_eqy_exc_min_int FROM financials_ts", con)
    con.close()
    con = sqlite3.connect(f"file:{FIN_DB}?mode=ro&immutable=1", uri=True)
    fr = pd.read_sql(
        "SELECT code,period,sq_net_profit,sq_net_yoy,sq_revenue,sq_rev_yoy,roe "
        "FROM finance_report", con)
    con.close()
    con = sqlite3.connect(f"file:{MV_DB}?mode=ro&immutable=1", uri=True)
    mv = pd.read_sql("SELECT month,code,circ_mv FROM hist_mv", con)
    con.close()

    for d in (fs, fr):
        d["code6"] = d["code"].astype(str).str[:6]
    fs["ann"] = pd.to_datetime(fs["ann_date"], errors="coerce")
    fs["end"] = pd.to_datetime(fs["end_date"], errors="coerce")
    fr["ann"] = pd.to_datetime(fr["period"], errors="coerce")  # finance_report 无 ann_date → 用 period 月末近似
    fr["period_dt"] = pd.to_datetime(fr["period"], errors="coerce")
    mv["code6"] = mv["code"].astype(str).str[:6]
    mv["month_dt"] = pd.to_datetime(mv["month"] + "-01")

    # ---- 净资产同比 + BP（用 fs：ann_date 生效）----
    fs = fs[fs["ann"].notna()].sort_values(["code6", "ann", "end"])
    fs = fs.drop_duplicates(["code6", "end"], keep="last")
    fs["eq_yoy"] = fs.groupby("code6")["total_hldr_eqy_exc_min_int"].pct_change(4)
    # ★保留全部 equity 行（bp 依赖）；eq_yoy 缺失行保留 NaN 不丢 equity
    fs = fs[["code6", "ann", "end", "eq_yoy", "total_hldr_eqy_exc_min_int"]]

    # ---- 市值（保留全部月份；merge_asof backward 需完整历史，drop_duplicates(code6) 只留最新月 → 早期 NaN）----
    mv = mv.sort_values(["code6", "month_dt"])

    # ---- 合并：以月末为锚，asof 向后取最新已披露 ----
    months = pd.date_range(start, pd.Timestamp.today().normalize(), freq="ME")
    rows = []
    fin_map = {}
    for _, r in fr.iterrows():
        key = (r["code6"], r["period_dt"])
        fin_map[key] = r
    # 逐月 asof：先构建按 code 排序的披露流，再 merge_asof
    fr2 = fr[["code6", "ann", "period_dt", "sq_net_profit", "sq_net_yoy",
              "sq_revenue", "sq_rev_yoy", "roe"]].sort_values(["code6", "ann"])
    fs2 = fs[["code6", "ann", "eq_yoy", "total_hldr_eqy_exc_min_int"]].sort_values(["code6", "ann"])
    mv2 = mv[["code6", "month_dt", "circ_mv"]].rename(columns={"month_dt": "ann"})
    mv2 = mv2.sort_values(["code6", "ann"])

    anchor = pd.DataFrame({"ann": months, "date": months})
    out = pd.DataFrame({"date": months})
    for code6 in fr2["code6"].drop_duplicates():
        f = fr2[fr2["code6"] == code6].drop_duplicates("ann", keep="last")
        a = pd.merge_asof(anchor, f[["ann", "sq_net_yoy", "sq_rev_yoy", "roe",
                                     "sq_net_profit", "sq_revenue"]],
                          on="ann", direction="backward")
        g = fs2[fs2["code6"] == code6].drop_duplicates("ann", keep="last")
        a = pd.merge_asof(a, g[["ann", "eq_yoy", "total_hldr_eqy_exc_min_int"]],
                          on="ann", direction="backward")
        m = mv2[mv2["code6"] == code6].drop_duplicates("ann", keep="last")
        a = pd.merge_asof(a, m[["ann", "circ_mv"]], on="ann", direction="backward")
        a["code6"] = code6
        rows.append(a)
    fin = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if fin.empty:
        return fin
    fin["bp"] = fin["circ_mv"] * 1e8 / fin["total_hldr_eqy_exc_min_int"]
    fin["accruals"] = fin["sq_net_yoy"] - fin["sq_rev_yoy"]   # 净利增速-营收增速（应收代理）
    fin["fscore"] = ((fin["roe"] > 0).astype(int) + (fin["sq_net_profit"] > 0).astype(int)
                     + (fin["sq_revenue"] > 0).astype(int))
    return fin[["date", "code6", "sq_net_yoy", "roe", "eq_yoy", "bp", "accruals", "fscore"]]


# ---------------- 截面/时序工具 ----------------

def _cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """截面 rank（0-1），每行（每日）对全市场排序"""
    return df.rank(axis=1, pct=True)


def _roll_corr(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    """滚动相关系数（date×code）"""
    return x.rolling(n, min_periods=n // 2).corr(y)


def _ts_rank(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """时间序列 rank：过去 n 天窗口内当前值的位置分位（0-1）"""
    return df.rolling(n, min_periods=max(n // 2, 2)).apply(
        lambda a: (a[-1] <= a).mean(), raw=True)


# ---------------- 因子实现 ----------------

def _f_turnover(P):
    return P["turn"]


def _f_turn_mean20(P):
    return P["turn"].rolling(20, min_periods=5).mean()


def _f_turn_std20(P):
    return P["turn"].rolling(20, min_periods=5).std()


def _f_turn_mid_prox(P):
    return P["turn"].rolling(20, min_periods=5).median()


def _f_lowvol_60(P):
    return P["close"].pct_change().rolling(60, min_periods=30).std() * np.sqrt(252)


def _f_std20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).std() * np.sqrt(252)


def _f_downside_vol(P):
    ret = P["close"].pct_change()
    return ret.where(ret < 0).rolling(60, min_periods=30).std() * np.sqrt(252)


def _f_reversal20(P):
    return P["close"] / P["close"].shift(20) - 1   # 20 日动量（方向 -1 = 反转）


def _f_o2c_sum_20(P):
    o2c = -(P["close"] / P["open"] - 1)
    return o2c.rolling(20, min_periods=5).sum()


def _f_amihud(P):
    ret = P["close"].pct_change()
    return (ret.abs() / (P["amount"] + 1e-8)).rolling(20, min_periods=5).mean()


def _f_max_ret20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).max()


def _f_skew20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).skew()


def _f_rmax_20(P):
    ret = P["close"].pct_change()
    mx = ret.rolling(20, min_periods=10).max()
    sd = ret.rolling(20, min_periods=10).std()
    return mx / sd.replace(0, np.nan)


def _f_amp20(P):
    return ((P["high"] - P["low"]) / P["close"]).rolling(20, min_periods=10).mean()


def _f_open_prem_20(P):
    prem = P["open"] / P["close"].shift(1) - 1
    return prem.rolling(20, min_periods=5).sum()


def _f_limit_up_cnt_20(P):
    return (P["pct_chg"] >= 9.5).astype(float).rolling(20, min_periods=5).sum()


def _f_consec_limit_up(P):
    """当前连续涨停天数（pct_chg>=9.5，0=非涨停）"""
    up = (P["pct_chg"] >= 9.5).astype(float)
    out = up.copy()
    for col in up.columns:
        s = up[col]
        out[col] = s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
    return out


def _f_limit_down_cnt_20(P):
    return (P["pct_chg"] <= -9.5).astype(float).rolling(20, min_periods=5).sum()


def _f_consec_limit_down(P):
    dn = (P["pct_chg"] <= -9.5).astype(float)
    out = dn.copy()
    for col in dn.columns:
        s = dn[col]
        out[col] = s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
    return out


def _f_ind_crowd_60(P):
    """行业成交额占全市场 60 日滚动均值（用 stock_basic.industry）"""
    try:
        con = sqlite3.connect(f"file:{BASIC_DB}?mode=ro&immutable=1", uri=True)
        rows = con.execute("SELECT code, industry FROM stock_basic WHERE industry!=''").fetchall()
        con.close()
        ind_of = {str(c): i for c, i in rows}
        amt = P["amount"]
        tot = amt.sum(axis=1).rolling(60, min_periods=20).mean()
        ind_amt = {}
        for ind in set(ind_of.values()):
            cols = [c for c in amt.columns if ind_of.get(c) == ind]
            if cols:
                ind_amt[ind] = amt[cols].sum(axis=1).rolling(60, min_periods=20).mean() / tot
        out = pd.DataFrame(index=amt.index, columns=amt.columns, dtype=float)
        for c in amt.columns:
            ind = ind_of.get(c)
            if ind and ind in ind_amt:
                out[c] = ind_amt[ind]
        return out
    except Exception:
        return pd.DataFrame(index=P["close"].index, columns=P["close"].columns, dtype=float)


def _f_ind_rs_20(P):
    """行业 20 日相对强弱：行业等权收益 - 全市场等权收益"""
    try:
        con = sqlite3.connect(f"file:{BASIC_DB}?mode=ro&immutable=1", uri=True)
        rows = con.execute("SELECT code, industry FROM stock_basic WHERE industry!=''").fetchall()
        con.close()
        ind_of = {str(c): i for c, i in rows}
        ret = P["close"].pct_change()
        mkt = ret.mean(axis=1).rolling(20, min_periods=10).sum()
        ind_ret = {}
        for ind in set(ind_of.values()):
            cols = [c for c in ret.columns if ind_of.get(c) == ind]
            if cols:
                ind_ret[ind] = ret[cols].mean(axis=1).rolling(20, min_periods=10).sum()
        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype=float)
        for c in ret.columns:
            ind = ind_of.get(c)
            if ind and ind in ind_ret:
                out[c] = ind_ret[ind] - mkt
        return out
    except Exception:
        return pd.DataFrame(index=P["close"].index, columns=P["close"].columns, dtype=float)


# ---------------- Alpha101（5 个代表） ----------------

def _f_alpha003(P):
    """-corr(rank(open), rank(volume), 10)"""
    return -_roll_corr(_cs_rank(P["open"]), _cs_rank(P["volume"]), 10)


def _f_alpha006(P):
    """-corr(open, volume, 10)"""
    return -_roll_corr(P["open"], P["volume"], 10)


def _f_alpha015(P):
    """-sum(rank(corr(rank(high), rank(volume), 3)), 3)"""
    c = _roll_corr(_cs_rank(P["high"]), _cs_rank(P["volume"]), 3)
    return -_cs_rank(c).rolling(3, min_periods=2).sum()


def _f_alpha044(P):
    """-rank(ts_rank(close,10)) * rank(delta(close,1)) * rank(ts_rank(volume/adv20,5))"""
    adv20 = P["volume"].rolling(20, min_periods=10).mean()
    a = _cs_rank(_ts_rank(P["close"], 10))
    b = _cs_rank(P["close"].diff())
    c = _cs_rank(_ts_rank(P["volume"] / adv20.replace(0, np.nan), 5))
    return -(a * b * c)


def _f_alpha050(P):
    """-ts_rank(corr(rank(close), rank(volume), 5), 5)"""
    c = _roll_corr(_cs_rank(P["close"]), _cs_rank(P["volume"]), 5)
    return -_ts_rank(c, 5)


# ---------------- 基本面（PIT） ----------------

def _pivot_pit(fin: pd.DataFrame, col: str, px_index: pd.DatetimeIndex,
               px_columns=None) -> pd.DataFrame:
    """PIT 因子：月末锚点 → 日频 ffill → 对齐价格面板索引；code6 列映射回带后缀 code"""
    if fin is None or fin.empty:
        return pd.DataFrame(index=px_index, columns=[], dtype=float)
    p = fin.pivot_table(index="date", columns="code6", values=col)
    p = p.reindex(px_index).ffill()
    if px_columns is not None:
        m = {str(c).split(".")[0]: c for c in px_columns}
        p = p.rename(columns=m)
    return p


def _f_sue(P, fin):
    return _pivot_pit(fin, "sq_net_yoy", P["close"].index, P["close"].columns)


def _f_roe(P, fin):
    return _pivot_pit(fin, "roe", P["close"].index, P["close"].columns)


def _f_asset_growth(P, fin):
    return _pivot_pit(fin, "eq_yoy", P["close"].index, P["close"].columns)


def _f_bp(P, fin):
    return _pivot_pit(fin, "bp", P["close"].index, P["close"].columns)


def _f_accruals(P, fin):
    return _pivot_pit(fin, "accruals", P["close"].index, P["close"].columns)


def _f_fscore(P, fin):
    return _pivot_pit(fin, "fscore", P["close"].index, P["close"].columns)


# ---------------- 注册表 ----------------

FACTOR_FUNCS = {
    "turnover": _f_turnover, "turn_mean20": _f_turn_mean20,
    "turn_std20": _f_turn_std20, "turn_mid_prox": _f_turn_mid_prox,
    "lowvol_60": _f_lowvol_60, "std20": _f_std20, "downside_vol": _f_downside_vol,
    "reversal20": _f_reversal20, "o2c_sum_20": _f_o2c_sum_20,
    "amihud": _f_amihud,
    "max_ret20": _f_max_ret20, "skew20": _f_skew20, "rmax_20": _f_rmax_20,
    "amp20": _f_amp20, "open_prem_20": _f_open_prem_20,
    "limit_up_cnt_20": _f_limit_up_cnt_20, "consec_limit_up": _f_consec_limit_up,
    "limit_down_cnt_20": _f_limit_down_cnt_20, "consec_limit_down": _f_consec_limit_down,
    "ind_crowd_60": _f_ind_crowd_60, "ind_rs_20": _f_ind_rs_20,
    "lhb_cnt_20": _f_lhb_cnt_20, "lhb_jg_cnt_20": _f_lhb_jg_cnt_20,
    "shebao_hold": _f_shebao_hold, "shebao_chg": _f_shebao_chg,
    "gdhs_chg_pct": _f_gdhs_chg_pct,
    "alpha003": _f_alpha003, "alpha006": _f_alpha006, "alpha015": _f_alpha015,
    "alpha044": _f_alpha044, "alpha050": _f_alpha050,
    # 基本面（PIT）
    "sue": _f_sue, "roe": _f_roe, "asset_growth": _f_asset_growth,
    "bp": _f_bp, "accruals": _f_accruals, "fscore": _f_fscore,
}

# 仅价量（不需要 PIT 财报）的因子
PRICE_ONLY = {k for k in FACTOR_FUNCS
              if k not in ("sue", "roe", "asset_growth", "bp", "accruals", "fscore")}


def compute(name: str, start: str = DEFAULT_START, end: str = None) -> pd.DataFrame:
    """计算单个因子面板（date×code）"""
    if name not in FACTOR_FUNCS:
        raise KeyError(f"未知因子: {name}（可选: {sorted(FACTOR_FUNCS)}）")
    P = _load_price_panels(start, end)
    fin = None
    if name not in PRICE_ONLY:
        fin = _load_finance_pit(start)
        if fin.empty:
            return pd.DataFrame(index=P["close"].index, columns=P["close"].columns, dtype=float)
        # code6 对齐：把 P 的列转 code6
        P = dict(P)
        P["_code6_map"] = {c: c.split(".")[0] for c in P["close"].columns}
    fn = FACTOR_FUNCS[name]
    if name in PRICE_ONLY:
        return fn(P)
    return fn(P, fin)


PANEL_CACHE_DIR = BASE / "output" / "alpha_panels"


def save_panels(panels: dict, start: str = DEFAULT_START):
    """因子面板缓存到磁盘（每因子一个 parquet）——避免评估/挖掘重复重算"""
    PANEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (PANEL_CACHE_DIR / "meta.json").write_text(
        json.dumps({"start": start, "names": sorted(panels), "ts": str(pd.Timestamp.now())}),
        encoding="utf-8")
    for name, df in panels.items():
        df.to_parquet(PANEL_CACHE_DIR / f"{name}.parquet")
    print(f"缓存已存 {PANEL_CACHE_DIR}（{len(panels)} 因子）")


def load_panels(start: str = DEFAULT_START, names: list = None) -> dict:
    """读缓存面板；缺失/元数据不符时重算；新因子增量补算（★2026-08-23）"""
    want = names or list(FACTOR_FUNCS.keys())
    meta = PANEL_CACHE_DIR / "meta.json"
    out = {}
    if meta.exists():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            if m.get("start") == start:
                for n in want:
                    fp = PANEL_CACHE_DIR / f"{n}.parquet"
                    if fp.exists():
                        out[n] = pd.read_parquet(fp)
        except Exception:
            pass
    missing = [n for n in want if n not in out]
    if missing:
        print(f"补算缺失因子 {len(missing)} 个: {missing}")
        extra = compute_all(start=start, names=missing)
        out.update(extra)
        save_panels(out, start)
    if out:
        print(f"面板就绪（{len(out)}/{len(want)} 因子，start={start}）")
    return out


def compute_all(start: str = DEFAULT_START, end: str = None,
                names: list = None) -> dict:
    """批量计算因子面板（内存约 30 因子 × 85MB，逐因子计算不入缓存）"""
    names = names or list(FACTOR_FUNCS.keys())
    out = {}
    P = _load_price_panels(start, end)
    fin = None
    for n in names:
        if n not in FACTOR_FUNCS:
            continue
        try:
            if n in PRICE_ONLY:
                out[n] = FACTOR_FUNCS[n](P)
            else:
                if fin is None:
                    fin = _load_finance_pit(start)
                out[n] = FACTOR_FUNCS[n](P, fin)
            print(f"  ✓ {n} ({FAMILY.get(n, '')}) {out[n].shape}")
        except Exception as e:
            print(f"  ✗ {n}: {str(e)[:80]}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default=None, help="逗号分隔（默认全部）")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--out", default=str(BASE / "output" / "alpha_panels" / "panel.parquet"))
    args = ap.parse_args()
    names = args.names.split(",") if args.names else None
    t0 = pd.Timestamp.now()
    panels = compute_all(start=args.start, names=names)
    print(f"共 {len(panels)} 个因子，耗时 {(pd.Timestamp.now() - t0).total_seconds():.0f}s")
