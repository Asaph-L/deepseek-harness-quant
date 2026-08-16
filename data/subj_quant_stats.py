# -*- coding: utf-8 -*-
"""data/subj_quant_stats.py — 标签胜率统计管道（主系统自建 · 2026-08-14）

用户：「因子正在忙别的大业务，你先自己做」——接管原分工给因子池的 tag_stats 胜率统计。

原理（规格 2.4）：每个主观标签 = 一个可回测的历史事件，统计"事件日后 horizon 日跑赢大盘的胜率"。
  标签 → 历史事件识别 → 未来 horizon 日超额收益 → 胜率/n/超额 → tag_stats 入库 → 徽章亮起。

可回测的标签（数据已具备）：
  业绩类：中报预增/年报预增（单季净利同比>50%）、扭亏（单季净利转正）、超预期（同比>100%）
  事件类：破净（流通市值 < 净资产，PB<1 近似）
不可回测（无公告数据，标记待积累）：治理类（增持/回购/激励/分拆）、中标/重组/高送转

基准：HS300（近似"跑赢大盘"，非全市场等权——首次版本诚实标注）
用法：python data/subj_quant_stats.py
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
CACHE = Path(r"data/cache")
DAILY_PIVOT = BASE / "output" / "daily_close.parquet"
HS300_PARQUET = BASE / "output" / "hs300_monthly.parquet"
HORIZON = 20          # 未来 20 交易日
MIN_N = 30            # 徽章最低样本
SUBJ_DB = BASE / "data" / "cache" / "subj_quant.db"


def _to_bar_code(c):
    """6 位代码 → bars.db 格式（加交易所后缀）"""
    c = str(c).strip()[:6]
    if c[:1] in ("6", "9"):
        return c + ".SH"
    if c[:1] in ("4", "8"):
        return c + ".BJ"
    return c + ".SZ"


# 证监会门类 → 板块（19 个一级板块）
SECTOR_MAP = {
    "A": "农林牧渔", "B": "采矿业", "C": "制造业", "D": "公用事业", "E": "建筑业",
    "F": "商贸零售", "G": "交通运输", "H": "住宿餐饮", "I": "信息技术", "J": "金融",
    "K": "房地产", "L": "商务服务", "M": "科研服务", "N": "环保水利", "O": "居民服务",
    "P": "教育", "Q": "卫生", "R": "文化传媒", "S": "综合",
}


def _load_sector():
    """code(6位) → 板块门类（stock_basic.db 证监会行业）"""
    con = sqlite3.connect(r"data/cache/stock_basic.db")
    rows = con.execute("SELECT code, industry FROM stock_basic WHERE industry!=''").fetchall()
    con.close()
    return {str(r[0])[:6]: SECTOR_MAP.get(str(r[1])[:1], "其他") for r in rows}


def _load_daily_close():
    """全市场日线收盘透视（qfq），缓存 parquet 加速"""
    if DAILY_PIVOT.exists():
        return pd.read_parquet(DAILY_PIVOT)
    con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
    df = pd.read_sql("SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    DAILY_PIVOT.parent.mkdir(parents=True, exist_ok=True)
    p.to_parquet(DAILY_PIVOT)
    return p


def _load_hs300():
    """HS300 日线收盘（benchmark，baostock 缓存）"""
    if HS300_PARQUET.exists():
        d = pd.read_parquet(HS300_PARQUET)["close"]
        d.index = pd.to_datetime(d.index)
        return d.sort_index()
    con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
    rows = con.execute("SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
    con.close()
    return pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows}).sort_index()


def _fwd_excess(daily, hs300, events, sector_map=None, horizon=HORIZON):
    """事件日 → forward horizon 日 跑赢 HS300 的超额收益，返回 (板块, 超额) 列表"""
    dates = daily.index
    date_pos = {d: i for i, d in enumerate(dates)}
    hs_pos = {d: i for i, d in enumerate(hs300.index)}
    rets = []
    cols = set(daily.columns)
    for code, day in events:
        if code not in cols or day not in date_pos or day not in hs_pos:
            continue
        i = date_pos[day]
        j = i + horizon
        if j >= len(dates):
            continue
        col = daily[code]
        if not (col.iloc[i] > 0 and col.iloc[j] > 0):
            continue
        s_ret = col.iloc[j] / col.iloc[i] - 1
        hi, hj = hs_pos[day], hs_pos[day] + horizon
        if hj >= len(hs300):
            continue
        h_ret = hs300.iloc[hj] / hs300.iloc[hi] - 1
        sector = sector_map.get(str(code)[:6], "其他") if sector_map else "全部"
        rets.append((sector, s_ret - h_ret))
    return rets


def _events_finance(daily):
    """业绩类事件（用财报公告日 ann_date + 单季净利同比）"""
    con = sqlite3.connect(f"file:{CACHE / 'finance.db'}?mode=ro&immutable=1", uri=True)
    fr = pd.read_sql("SELECT code, period, sq_net_profit, sq_net_yoy FROM finance_report", con)
    con.close()
    con = sqlite3.connect(f"file:{CACHE / 'finance_ts.db'}?mode=ro&immutable=1", uri=True)
    fs = pd.read_sql("SELECT code, end_date, ann_date FROM financials_ts", con)
    con.close()

    fr["period"] = fr["period"].astype(str).str.replace("-", "").str[:8]
    fr["code"] = fr["code"].astype(str).str[:6]
    fs["code"] = fs["code"].astype(str).str[:6]
    fs["end_date"] = fs["end_date"].astype(str).str[:10].str.replace("-", "").str[:8]
    fs["ann_date"] = fs["ann_date"].astype(str).str[:10]

    ann = fs.drop_duplicates(subset=["code", "end_date"])[["code", "end_date", "ann_date"]]
    m = fr.merge(ann, left_on=["code", "period"], right_on=["code", "end_date"], how="inner")
    m["ann_date"] = pd.to_datetime(m["ann_date"], errors="coerce")
    m = m[m["ann_date"].notna() & (m["ann_date"] >= daily.index[0])]
    m["month"] = m["period"].str[4:6].astype(int)
    m["sq_net_yoy"] = pd.to_numeric(m["sq_net_yoy"], errors="coerce")
    m["sq_net_profit"] = pd.to_numeric(m["sq_net_profit"], errors="coerce")
    m["bar_code"] = m["code"].apply(_to_bar_code)

    # 单季净利同比（前一期的同季，用于扭亏判断：上一季度净利）
    m = m.sort_values(["code", "period"])
    m["prev_sq_profit"] = m.groupby("code")["sq_net_profit"].shift(1)

    ev = {}
    for k, mo in (("中报预增", 6), ("年报预增", 12)):
        mask = (m["month"] == mo) & (m["sq_net_yoy"] > 0.5)
        ev[k] = list(zip(m[mask]["bar_code"], m[mask]["ann_date"]))
    mask = (m["sq_net_profit"] > 0) & (m["prev_sq_profit"] < 0)
    ev["扭亏"] = list(zip(m[mask]["bar_code"], m[mask]["ann_date"]))
    mask = m["sq_net_yoy"] > 1.0
    ev["超预期"] = list(zip(m[mask]["bar_code"], m[mask]["ann_date"]))
    return ev


def _events_break_net(daily):
    """破净：流通市值 < 净资产（PB<1 近似，月度事件）"""
    con = sqlite3.connect(f"file:{CACHE / 'hist_mv.db'}?mode=ro&immutable=1", uri=True)
    mv = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", con)
    con.close()
    con = sqlite3.connect(f"file:{CACHE / 'finance_ts.db'}?mode=ro&immutable=1", uri=True)
    fs = pd.read_sql("SELECT code, end_date, total_hldr_eqy_exc_min_int FROM financials_ts", con)
    con.close()

    mv["month"] = mv["month"].astype(str).str.replace("-", "").str[:6]
    mv["code"] = mv["code"].astype(str).str[:6]
    fs["code"] = fs["code"].astype(str).str[:6]
    fs["end_date"] = fs["end_date"].astype(str).str[:10].str.replace("-", "").str[:8]
    # 净资产按 code+end_date 取最新（月度事件用最近一期净资产）
    fs = fs[fs["total_hldr_eqy_exc_min_int"].notna()]
    fs = fs.sort_values("end_date").drop_duplicates(subset="code", keep="last")

    m = mv.merge(fs, on="code", how="inner")
    m["circ_mv"] = pd.to_numeric(m["circ_mv"], errors="coerce")
    m["nav"] = pd.to_numeric(m["total_hldr_eqy_exc_min_int"], errors="coerce")
    m = m[(m["circ_mv"] > 0) & (m["nav"] > 0)]
    m["pb"] = m["circ_mv"] / m["nav"]
    m = m[m["pb"] < 1.0]
    # 月度 → 事件日（当月最后一个交易日）
    m["ev_day"] = pd.to_datetime(m["month"] + "01") + pd.offsets.MonthEnd(0)
    m = m[m["ev_day"] >= daily.index[0]]
    m["bar_code"] = m["code"].apply(_to_bar_code)
    return list(zip(m["bar_code"], m["ev_day"]))


def _aggregate(tag, rets):
    """(板块, 超额) 列表 → 整体胜率 + 分板块胜率"""
    if not rets:
        return None, []
    df = pd.DataFrame(rets, columns=["sector", "ret"])
    df = df[np.isfinite(df["ret"])]
    if len(df) == 0:
        return None, []
    arr = df["ret"].to_numpy()
    overall = {"tag": tag, "n": len(arr), "winrate": float((arr > 0).mean()),
               "excess": float(arr.mean()), "p_value": None,
               "sample_ok": int(len(arr) >= MIN_N)}
    sectors = []
    for sec, g in df.groupby("sector"):
        if len(g) >= MIN_N:
            a = g["ret"].to_numpy()
            sectors.append({"tag": tag, "sector": sec, "n": len(a),
                            "winrate": float((a > 0).mean()), "excess": float(a.mean())})
    sectors.sort(key=lambda x: -x["winrate"])
    return overall, sectors


def main():
    print("加载日线透视…")
    daily = _load_daily_close()
    hs300 = _load_hs300()
    sector_map = _load_sector()
    print(f"  日线 {len(daily)} 天 × {daily.shape[1]} 只 · 板块 {len(set(sector_map.values()))} 个")

    print("识别业绩类事件…")
    ev_fin = _events_finance(daily)
    print("识别破净事件…")
    ev_bn = _events_break_net(daily)
    events = {**ev_fin, "破净": ev_bn}

    rows = []
    sector_rows = []
    for tag, ev in events.items():
        rets = _fwd_excess(daily, hs300, ev, sector_map)
        overall, sectors = _aggregate(tag, rets)
        if overall:
            rows.append(overall)
            sector_rows.extend(sectors)
            ok = "✓" if overall["sample_ok"] else "待积累"
            print(f"  {tag:<8} n={overall['n']:<5} 胜率={overall['winrate']*100:>5.1f}%  超额={overall['excess']*100:>+6.1f}%  {ok}")
            if sectors:
                best = sectors[0]
                worst = sectors[-1]
                print(f"           板块: 最强 {best['sector']}({best['winrate']*100:.0f}%) / 最弱 {worst['sector']}({worst['winrate']*100:.0f}%)")

    if not rows:
        print("无有效事件")
        return
    # 写入 tag_stats（整体）+ tag_stats_sector（分板块）
    con = sqlite3.connect(str(SUBJ_DB))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.execute("""CREATE TABLE IF NOT EXISTS tag_stats_sector (
        tag TEXT, sector TEXT, n INT, winrate REAL, excess REAL, PRIMARY KEY(tag, sector))""")
    for r in rows:
        con.execute("INSERT OR REPLACE INTO tag_stats VALUES (?,?,?,?,?,?)",
                    (r["tag"], r["n"], r["winrate"], r["excess"], r["p_value"], now))
    for s in sector_rows:
        con.execute("INSERT OR REPLACE INTO tag_stats_sector VALUES (?,?,?,?,?)",
                    (s["tag"], s["sector"], s["n"], s["winrate"], s["excess"]))
    con.commit()
    con.close()
    print(f"\n已写入 {len(rows)} 个标签胜率 + {len(sector_rows)} 个板块细分 → {SUBJ_DB}")


if __name__ == "__main__":
    main()
