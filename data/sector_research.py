# -*- coding: utf-8 -*-
"""data/sector_research.py — 板块研究台（相对独立模块 · 2026-08-14 v2 重构）

用户需求（v2 重定向）：「我主观框定一个板块，机器帮我择时 + 选板块内强因子」。
替代 v1 的"事件标签+贝叶斯"。

能力：
  1. list_sectors()：证监会 83 大类列表
  2. sector_timing(sector)：板块等权指数 60日动量 + MA20/60 → 可进/观望/回避
  3. sector_strong_factors(sector)：七大机会类型在该板块内的历史胜率排名
     （未来接入：散户因子，因子池正在研究）

强因子口径 = 主系统 7 大机会类型（reversal/value/breakout/revalue/event/quality_gap/pv_consensus）
  → 在选定板块内的历史 20 日跑赢 HS300 胜率。

数据：daily_close.parquet（缓存）+ stock_basic.db（行业）+ finance.db + hist_mv.db
"""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from data.cache import CACHE_DIR

CACHE = CACHE_DIR
DAILY_PIVOT = BASE / "output" / "daily_close.parquet"
HS300_PARQUET = BASE / "output" / "hs300_monthly.parquet"
HORIZON = 20
MIN_N = 30

# 七大机会类型（与 factors/opportunities/registry.py 一致）
OTYPES = {
    "value": "低估值",
    "reversal": "反转",
    "breakout": "突破",
    "revalue": "价值重估",
    "quality_gap": "质量折价",
    "pv_consensus": "量价共识",
    "event": "事件驱动",
}

# 七大机会类型全市场 17 年回测 6月胜率/均收益（from registry evidence）
OTYPE_WINRATE = {
    "value": {"name": "低估值", "winrate": 62.5, "excess": 11.6},
    "quality_gap": {"name": "质量折价", "winrate": 70.4, "excess": 14.6},
    "revalue": {"name": "价值重估", "winrate": 53.6, "excess": 8.7},
    "reversal": {"name": "反转", "winrate": 39.0, "excess": -1.8, "neg": True},
    "breakout": {"name": "突破", "winrate": 45.5, "excess": 5.4},
    "pv_consensus": {"name": "量价共识", "winrate": 53.3, "excess": 4.3},
    "event": {"name": "事件驱动", "winrate": 51.5, "excess": 8.3},
}

SECTOR_MAP = {
    "A": "农林牧渔", "B": "采矿业", "C": "制造业", "D": "公用事业", "E": "建筑业",
    "F": "商贸零售", "G": "交通运输", "H": "住宿餐饮", "I": "信息技术", "J": "金融",
    "K": "房地产", "L": "商务服务", "M": "科研服务", "N": "环保水利", "O": "居民服务",
    "P": "教育", "Q": "卫生", "R": "文化传媒", "S": "综合",
}


def _to_bar_code(c):
    c = str(c).strip()[:6]
    if c[:1] in ("4", "8") or c.startswith("92"):
        return c + ".BJ"
    if c[:1] in ("6", "9"):
        return c + ".SH"
    return c + ".SZ"


def _load_sector_detail():
    """code(带后缀) → (板块, 板块)。★2026-08-22 修复：stock_basic.industry 为纯中文
    行业名（如"银行"/"全国地产"），无"代码+名称"前缀结构；原 ind[:3]/ind[3:] 切分
    会把"IT设备"错切成 code="IT设"/name="备"。改为 code=name=完整行业名（110 类）。"""
    con = sqlite3.connect(str(CACHE / "stock_basic.db"))
    rows = con.execute("SELECT code, industry FROM stock_basic WHERE industry!=''").fetchall()
    con.close()
    m = {}
    for code, ind in rows:
        c = str(code).strip()
        if len(c) >= 6 and ind:
            m[c] = (ind, ind)
    return m


def list_sectors():
    """83 大类列表 [{code, name, n}]"""
    m = _load_sector_detail()
    from collections import Counter
    cnt = Counter(v[0] for v in m.values())
    name = {v[0]: v[1] for v in m.values()}
    out = [{"code": k, "name": name.get(k, k), "n": cnt[k]}
           for k in sorted(cnt)]
    return out


def _daily():
    """日线收盘透视（qfq），缓存 parquet；缺失时从 bars.db 生成（★2026-08-22 修复：
    原直接 read_parquet 无 fallback，daily_close.parquet 缺失 → 板块研究台全线 500）"""
    if not DAILY_PIVOT.exists():
        con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
        df = pd.read_sql("SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
        con.close()
        p = df.pivot_table(index="date", columns="code", values="close").sort_index()
        p.index = pd.to_datetime(p.index)
        DAILY_PIVOT.parent.mkdir(parents=True, exist_ok=True)
        p.to_parquet(DAILY_PIVOT)
    return pd.read_parquet(DAILY_PIVOT)


def _sector_index(daily, sector):
    """板块等权净值（板块内股票的日均收益 cumprod）"""
    m = _load_sector_detail()
    codes = [c for c, (sc, _) in m.items() if sc == sector and c in daily.columns]
    if len(codes) < 5:
        return None, len(codes)
    sub = daily[codes].ffill().dropna(how="all")
    ret = sub.pct_change().mean(axis=1, skipna=True)
    nav = (1 + ret.fillna(0)).cumprod()
    return nav, len(codes)


def sector_timing(sector):
    """板块择时：60日动量 + MA20/MA60 → {state, mom60, ma_state, n}"""
    daily = _daily()
    nav, n = _sector_index(daily, sector)
    if nav is None:
        return {"ok": False, "n": n, "note": "板块成分不足"}
    close = nav
    mom60 = close.iloc[-1] / close.iloc[-61] - 1 if len(close) > 61 else np.nan
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma_state = "多头" if ma20 > ma60 else "空头"
    if mom60 > 0 and ma_state == "多头":
        state, label, color = "in", "可进", "green"
    elif mom60 < 0 and ma_state == "空头":
        state, label, color = "out", "回避", "red"
    else:
        state, label, color = "wait", "观望", "yellow"
    return {"ok": True, "n": n, "state": state, "label": label, "color": color,
            "mom60": round(float(mom60) * 100, 2), "ma_state": ma_state,
            "asof": str(daily.index[-1])[:10]}


def _finance():
    con = sqlite3.connect(f"file:{CACHE / 'finance.db'}?mode=ro&immutable=1", uri=True)
    fr = pd.read_sql("SELECT code, period, sq_net_profit, sq_net_yoy, roe FROM finance_report", con)
    con.close()
    fr["code"] = fr["code"].astype(str).str[:6]
    fr["period"] = fr["period"].astype(str).str.replace("-", "").str[:8]
    fr["end_date"] = pd.to_datetime(fr["period"], format="%Y%m%d", errors="coerce")
    fr["sq_net_yoy"] = pd.to_numeric(fr["sq_net_yoy"], errors="coerce")
    fr["sq_net_profit"] = pd.to_numeric(fr["sq_net_profit"], errors="coerce")
    fr["roe"] = pd.to_numeric(fr["roe"], errors="coerce")
    con = sqlite3.connect(f"file:{CACHE / 'finance_ts.db'}?mode=ro&immutable=1", uri=True)
    ann = pd.read_sql("SELECT code,end_date,ann_date FROM financials_ts", con)
    con.close()
    ann["code"] = ann["code"].astype(str).str[:6]
    ann["period"] = ann["end_date"].astype(str).str.replace("-", "").str[:8]
    ann["ann_date"] = pd.to_datetime(ann["ann_date"], errors="coerce")
    ann = ann.sort_values("ann_date").drop_duplicates(["code", "period"], keep="last")
    fr = fr.merge(ann[["code", "period", "ann_date"]], on=["code", "period"], how="left")
    return fr


def _asof_by_code(left, right, left_on, right_on):
    """逐股票向后 as-of；确保每个月只看到当时已公告的财务数据。"""
    pieces = []
    right_groups = {code: g.sort_values(right_on) for code, g in right.groupby("code")}
    for code, group in left.groupby("code"):
        rg = right_groups.get(code)
        if rg is None or rg.empty:
            continue
        if "end_date" in rg.columns:
            rg = rg.copy()
            rg["end_date"] = pd.to_datetime(rg["end_date"], errors="coerce")
            rg = rg[rg["end_date"].notna()].sort_values([right_on, "end_date"])
            # 晚披露的旧期修订不能盖过已经披露的新季度；只保留报告期前沿。
            rg = rg[rg["end_date"].eq(rg["end_date"].cummax())]
            if rg.empty:
                continue
        lg = group.sort_values(left_on)
        merged = pd.merge_asof(
            lg, rg.drop(columns=["code"]), left_on=left_on, right_on=right_on,
            direction="backward", allow_exact_matches=True,
        )
        merged["code"] = code
        pieces.append(merged)
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def _break_net():
    con = sqlite3.connect(f"file:{CACHE / 'hist_mv.db'}?mode=ro&immutable=1", uri=True)
    mv = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", con)
    con.close()
    con = sqlite3.connect(f"file:{CACHE / 'finance_ts.db'}?mode=ro&immutable=1", uri=True)
    fs = pd.read_sql(
        "SELECT code,end_date,ann_date,total_hldr_eqy_exc_min_int FROM financials_ts", con
    )
    con.close()
    mv["month"] = mv["month"].astype(str).str.replace("-", "").str[:6]
    mv["code"] = mv["code"].astype(str).str[:6]
    fs["code"] = fs["code"].astype(str).str[:6]
    mv["day"] = pd.to_datetime(mv["month"] + "01") + pd.offsets.MonthEnd(0)
    fs["ann_date"] = pd.to_datetime(fs["ann_date"], errors="coerce")
    fs["end_date"] = pd.to_datetime(fs["end_date"], errors="coerce")
    fs = fs[fs["ann_date"].notna() & fs["total_hldr_eqy_exc_min_int"].notna()]
    m = _asof_by_code(
        mv, fs[["code", "end_date", "ann_date", "total_hldr_eqy_exc_min_int"]],
        "day", "ann_date"
    )
    if m.empty:
        return pd.DataFrame(columns=["code", "day", "pb"])
    m["circ_mv"] = pd.to_numeric(m["circ_mv"], errors="coerce")
    m["nav"] = pd.to_numeric(m["total_hldr_eqy_exc_min_int"], errors="coerce")
    m = m[(m["circ_mv"] > 0) & (m["nav"] > 0)]
    m["pb"] = m["circ_mv"] * 1e8 / m["nav"]   # circ_mv 单位=亿元 → 换算成元再除以净资产
    return m[["code", "day", "pb"]]


def _monthly_factors(daily):
    """月度价格因子面板：60日回撤、20日动量、距52周高点"""
    m = daily.resample("ME").last()
    mom20 = m.pct_change(20)
    hh250 = m.rolling(250, min_periods=60).max()
    near_high = m / hh250 - 1
    dd60 = m / m.rolling(60, min_periods=20).max() - 1
    return m, mom20, near_high, dd60


def _fwd_winrate(daily, hs300, events, sector_map, horizon=HORIZON):
    """事件 → forward horizon 日 跑赢 HS300；返回 {板块: (n, winrate, excess)}
    ★2026-08-15 T+1 口径：事件月末收盘才确定 → 次一交易日（i+1）起算持有 horizon 日，
    不含事件日收盘→次日的隔夜跳空（原 close_i 起算含跳空，轻度前视）。"""
    dates = daily.index
    dpos = {d: i for i, d in enumerate(dates)}
    hpos = {d: i for i, d in enumerate(hs300.index)}
    cols = set(daily.columns)
    buckets = {}
    for code, day in events:
        if code not in cols or day not in dpos or day not in hpos:
            continue
        i = dpos[day]
        if i + 1 + horizon >= len(dates) or hpos[day] + 1 + horizon >= len(hs300):
            continue
        c = daily[code]
        if not (c.iloc[i + 1] > 0 and c.iloc[i + 1 + horizon] > 0):
            continue
        sr = c.iloc[i + 1 + horizon] / c.iloc[i + 1] - 1
        hr = hs300.iloc[hpos[day] + 1 + horizon] / hs300.iloc[hpos[day] + 1] - 1
        sec = sector_map.get(str(code)[:6], "其他")
        buckets.setdefault(sec, []).append(sr - hr)
    return buckets


def sector_strong_factors(sector):
    """七大机会类型在该板块内的历史胜率排名"""
    daily = _daily()
    # ★2026-08-22 修复：HS300 缓存缺失时回退 bars.db（SH.000300），避免 read_parquet 抛错
    if HS300_PARQUET.exists():
        hs300 = pd.read_parquet(HS300_PARQUET)["close"].sort_index()
    else:
        con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
        rows = con.execute(
            "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date"
        ).fetchall()
        con.close()
        hs300 = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows}).sort_index()
    hs300.index = pd.to_datetime(hs300.index)
    fr = _finance()
    bn = _break_net()
    m, mom20, near_high, dd60 = _monthly_factors(daily)
    sector_map = {str(c)[:6]: sc for c, (sc, _) in _load_sector_detail().items()}

    # 各机会类型的简化触发事件（月度，code→bar_code, day）
    def _stack(df, name):
        s = df.stack().reset_index()
        s.columns = ["day", "code", name]
        s["code"] = s["code"].astype(str).str[:6]
        return s

    def mk_events(df):
        return [(c, d) for c, d in zip(df["code"].apply(_to_bar_code), df["day"])]

    dd = _stack(dd60, "dd")
    mom = _stack(mom20, "mom")
    nh = _stack(near_high, "nh")
    vol = _stack(m.pct_change().rolling(20).std(), "vol")

    events = {}
    # 低估值：PB < 1
    events["value"] = mk_events(bn[bn["pb"] < 1.0])
    # 质量折价：ROE>15% 且 60日回撤<-25%
    fr_known = fr[fr["ann_date"].notna()].sort_values("ann_date")
    q = _asof_by_code(
        dd, fr_known[["code", "end_date", "ann_date", "roe"]], "day", "ann_date"
    )
    events["quality_gap"] = mk_events(q[(q["roe"] > 0.15) & (q["dd"] < -0.25)])
    # 价值重估：每次单季净利同比 > 50% 的实际公告日。
    fr2 = fr[(fr["sq_net_yoy"] > 0.5) & fr["ann_date"].notna()].copy()
    fr2 = fr2.copy()
    fr2["day"] = fr2["ann_date"]
    events["revalue"] = mk_events(fr2)
    # 反转：60日回撤<-25% 且 20日动量>0
    r = dd.merge(mom, on=["day", "code"], how="inner")
    events["reversal"] = mk_events(r[(r["dd"] < -0.25) & (r["mom"] > 0)])
    # 突破：距52周高点 < 5%
    events["breakout"] = mk_events(nh[nh["nh"] > -0.05])
    # 量价共识：低波（20日波动率低分位，简化）
    vq = vol[vol["vol"] < vol["vol"].quantile(0.3)]
    events["pv_consensus"] = mk_events(vq)
    # 事件驱动（精确化）：近20日涨停 且 所属行业20日动量>0（板块突破确认，事件/政策→涨停+趋势）
    up = daily.pct_change()
    ind_of = pd.Series({c: sector_map.get(str(c)[:6], "Z") for c in daily.columns})
    ind_ret = up.T.groupby(ind_of).median().T              # 行业日收益（date × 行业）
    ind_mom = ind_ret.rolling(20).sum().resample("ME").last()  # 行业20日动量（月末）
    ind_mom_long = ind_mom.reset_index().melt(id_vars="date", var_name="ind", value_name="ind_mom")
    ind_mom_long = ind_mom_long.rename(columns={"date": "day"})
    code_ind = pd.DataFrame({"code": [str(c)[:6] for c in ind_of.index], "ind": list(ind_of.values)})
    lim_m = (up > 0.095).rolling(20, min_periods=1).sum().resample("ME").last()
    ev = _stack(lim_m, "limup")
    ev2 = ev.merge(code_ind, on="code", how="left").merge(ind_mom_long, on=["day", "ind"], how="left")
    events["event"] = mk_events(ev2[(ev2["limup"] > 0) & (ev2["ind_mom"] > 0)])

    out = []
    for otype, ev in events.items():
        if not ev:
            out.append({"otype": otype, "name": OTYPES[otype], "n": 0,
                        "winrate": None, "excess": None, "ok": False, "note": "数据待接入"})
            continue
        buckets = _fwd_winrate(daily, hs300, ev, sector_map)
        if sector not in buckets or len(buckets[sector]) < MIN_N:
            n = len(buckets.get(sector, []))
            out.append({"otype": otype, "name": OTYPES[otype], "n": n,
                        "winrate": None, "excess": None, "ok": False,
                        "note": f"样本不足({n})"})
            continue
        a = np.array(buckets[sector])
        out.append({"otype": otype, "name": OTYPES[otype], "n": len(a),
                    "winrate": round(float((a > 0).mean()) * 100, 1),
                    "excess": round(float(a.mean()) * 100, 1), "ok": True, "note": ""})
    out.sort(key=lambda x: -(x["winrate"] if x["winrate"] is not None else -1))
    return out


def stock_search(q, limit=20):
    """按代码/名称搜索股票"""
    q = str(q or "").strip()
    if not q:
        return []
    con = sqlite3.connect(str(CACHE / "stock_basic.db"))
    rows = con.execute(
        "SELECT code, name FROM stock_basic WHERE code LIKE ? OR name LIKE ? LIMIT ?",
        (f"%{q}%", f"%{q}%", limit)).fetchall()
    con.close()
    return [{"code": r[0], "name": r[1]} for r in rows]


def stock_factors(code):
    """个股诊断：主观选个股 → 机器判断命中哪些机会类型 + 各因子历史命中率"""
    code = _to_bar_code(code)
    daily = _daily()
    if code not in daily.columns:
        return {"ok": False, "error": f"{code} 不在日线池"}
    c = daily[code].dropna()
    if len(c) < 260:
        return {"ok": False, "error": "历史不足 260 日"}
    close = float(c.iloc[-1])
    dd60 = close / float(c.iloc[-60:].max()) - 1
    mom20 = close / float(c.iloc[-21]) - 1
    nh250 = close / float(c.iloc[-250:].max()) - 1
    vol20 = float(c.pct_change().iloc[-20:].std() * np.sqrt(252))

    fr = _finance()
    fr_l = fr[fr["code"] == code[:6]].sort_values("period")
    roe = float(fr_l["roe"].dropna().iloc[-1]) if len(fr_l) and fr_l["roe"].notna().any() else None
    sq_nyoy = float(fr_l["sq_net_yoy"].dropna().iloc[-1]) if len(fr_l) and fr_l["sq_net_yoy"].notna().any() else None
    bn = _break_net()
    bnb = bn[bn["code"] == code[:6]]
    pb = float(bnb["pb"].iloc[-1]) if len(bnb) else None

    sec_map = _load_sector_detail()
    sc, sn = sec_map.get(code, ("", ""))
    con = sqlite3.connect(str(CACHE / "stock_basic.db"))
    row = con.execute("SELECT name FROM stock_basic WHERE code=?", (code,)).fetchone()
    con.close()
    name = row[0] if row else ""

    hits = []
    def hit(ot, met, note=""):
        w = OTYPE_WINRATE[ot]
        hits.append({"otype": ot, "name": w["name"], "met": met, "winrate": w["winrate"],
                     "excess": w["excess"], "note": note, "neg": w.get("neg", False)})
    hit("value", pb is not None and pb < 1.0, f"PB {pb:.2f}" if pb is not None else "")
    hit("quality_gap", roe is not None and roe > 0.15 and dd60 < -0.25,
        f"ROE {roe*100:.0f}% · 回撤 {dd60*100:.0f}%" if roe is not None else "")
    hit("revalue", sq_nyoy is not None and sq_nyoy > 0.5,
        f"单季净利同比 {sq_nyoy*100:.0f}%" if sq_nyoy is not None else "")
    hit("reversal", dd60 < -0.25 and mom20 > 0, f"回撤 {dd60*100:.0f}% · 20日动量 {mom20*100:.0f}%")
    hit("breakout", nh250 > -0.05, f"距52周高点 {nh250*100:.0f}%")
    hit("pv_consensus", vol20 < 0.3, f"20日波动率 {vol20*100:.0f}%")
    _up20 = c.pct_change().iloc[-20:]
    _n_limup = int((_up20 > 0.095).sum())
    hit("event", _n_limup > 0, f"近20日涨停 {_n_limup} 次" if _n_limup else "近20日无涨停")
    hits.sort(key=lambda x: (not x["met"], -x["winrate"]))

    return {"ok": True, "code": code, "name": name, "sector": (sc or sn or "").strip(),
            "price": round(close, 2),
            "factors": {"dd60": round(dd60*100, 1), "mom20": round(mom20*100, 1),
                        "nh250": round(nh250*100, 1), "vol20": round(vol20*100, 1),
                        "pb": round(pb, 2) if pb is not None else None,
                        "roe": round(roe*100, 1) if roe is not None else None,
                        "sq_nyoy": round(sq_nyoy*100, 1) if sq_nyoy is not None else None},
            "hits": hits}


def sector_pitch(sector, topn=20, strong_factors=None):
    """Pitch 层：用板块强因子筛选板块内股票 → 候选买入清单
    逻辑同主 pitch：命中强因子 → 按因子命中率加权评分 → 排序取 TopN"""
    daily = _daily()
    sec_map = _load_sector_detail()
    codes = [c for c, (sc, _) in sec_map.items() if sc == sector and c in daily.columns]
    if len(codes) < 5:
        return {"ok": False, "n": len(codes), "note": "板块成分不足"}
    sub = daily[codes].dropna(axis=1, how="all").iloc[-260:]
    if len(sub) < 60:
        return {"ok": False, "n": len(codes), "note": "历史不足"}
    close = sub.iloc[-1]
    dd60 = close / sub.iloc[-60:].max() - 1
    mom20 = close / sub.iloc[-21] - 1
    nh250 = close / sub.iloc[-250:].max() - 1
    vol20 = sub.pct_change().iloc[-20:].std() * np.sqrt(252)

    fr = _finance().sort_values("period").drop_duplicates(subset="code", keep="last").set_index("code")
    bn = _break_net().sort_values("day").drop_duplicates(subset="code", keep="last").set_index("code")

    # 散户涌入排雷（因子池研究 2026-08-15：户数大增=散户追涨 → 剔除）
    # ★2026-08-22 降级：gdhs_full.db（股东户数，外包数据）缺失 → 空表，跳过排雷不报错
    try:
        con = sqlite3.connect(str(CACHE / "gdhs_full.db"))
        gd = pd.read_sql("SELECT code, chg_pct, ann_date FROM gdhs", con)
        con.close()
        gd["code"] = gd["code"].astype(str).str[:6]
        gd = gd.sort_values("ann_date").drop_duplicates(subset="code", keep="last").set_index("code")
    except Exception:
        gd = pd.DataFrame(columns=["code", "chg_pct", "ann_date"]).set_index("code")

    # 板块强因子（复用已算的，避免重复计算）
    strong = strong_factors if strong_factors is not None else sector_strong_factors(sector)
    strong_ok = [f for f in strong if f.get("ok") and f["winrate"] is not None][:3]
    strong_names = {f["otype"] for f in strong_ok}
    if not strong_names:
        return {"ok": True, "n_total": len(codes), "n_pitch": 0, "strong_factors": [], "pitch": []}

    con = sqlite3.connect(str(CACHE / "stock_basic.db"))
    names = dict(con.execute("SELECT code, name FROM stock_basic").fetchall())
    con.close()

    def _f(v):
        return float(v) if pd.notna(v) else None

    rows = []
    n_excluded = 0
    for code in sub.columns:
        c6 = code[:6]
        # 散户涌入排雷：户数大增(>10%) 剔除
        if c6 in gd.index and pd.notna(gd.loc[c6, "chg_pct"]) and float(gd.loc[c6, "chg_pct"]) > 10:
            n_excluded += 1
            continue
        roe = _f(fr.loc[c6, "roe"]) if c6 in fr.index else None
        sq = _f(fr.loc[c6, "sq_net_yoy"]) if c6 in fr.index else None
        pb = _f(bn.loc[c6, "pb"]) if c6 in bn.index else None
        hits = []
        if pb is not None and pb < 1.0:
            hits.append("value")
        if roe is not None and roe > 0.15 and dd60[code] < -0.25:
            hits.append("quality_gap")
        if sq is not None and sq > 0.5:
            hits.append("revalue")
        if dd60[code] < -0.25 and mom20[code] > 0:
            hits.append("reversal")
        if nh250[code] > -0.05:
            hits.append("breakout")
        if vol20[code] < 0.3:
            hits.append("pv_consensus")
        strong_hits = [h for h in hits if h in strong_names]
        if strong_hits:
            score = sum(OTYPE_WINRATE[h]["winrate"] for h in strong_hits)
            rows.append({"code": code, "name": names.get(code, ""),
                         "price": round(float(close[code]), 2),
                         "hits": [OTYPE_WINRATE[h]["name"] for h in strong_hits],
                         "hits_otype": strong_hits, "score": round(score, 1),
                         "n_hits": len(strong_hits)})
    rows.sort(key=lambda x: (-x["n_hits"], -x["score"]))
    return {"ok": True, "n_total": len(codes), "n_pitch": len(rows),
            "n_excluded": n_excluded,
            "strong_factors": [{"otype": f["otype"], "name": f["name"], "winrate": f["winrate"]}
                               for f in strong_ok],
            "pitch": rows[:topn]}


def sector_retail(sector, topn=20):
    """散户因子（因子池 2026-08-15 研究落地）：
    ① 股东户数：散户涌入（chg_pct>10%）= 排雷；主力吸筹（chg_pct<-10%）= 正向
    ② 换手率：20日平均换手率（低换手=防守，反过度交易）"""
    daily = _daily()
    sec_map = _load_sector_detail()
    codes = [c for c, (sc, _) in sec_map.items() if sc == sector and c in daily.columns]
    if len(codes) < 5:
        return {"ok": False, "n": len(codes), "note": "板块成分不足"}

    con = sqlite3.connect(str(CACHE / "stock_basic.db"))
    names = dict(con.execute("SELECT code, name FROM stock_basic").fetchall())
    con.close()

    # 质量过滤（ROE）——"低换手好股"必须基本面合格，剔除 ST/亏损
    fr = _finance().sort_values("period").drop_duplicates(subset="code", keep="last").set_index("code")

    # 股东户数（每 code 最新一期）
    # ★2026-08-22 降级：gdhs_full.db（外包数据）缺失 → 空表，跳过散户因子不报错
    try:
        con = sqlite3.connect(str(CACHE / "gdhs_full.db"))
        gd = pd.read_sql("SELECT code, chg_pct, ann_date FROM gdhs", con)
        con.close()
        gd["code"] = gd["code"].astype(str).str[:6]
        gd = gd.sort_values("ann_date").drop_duplicates(subset="code", keep="last").set_index("code")
    except Exception:
        gd = pd.DataFrame(columns=["code", "chg_pct", "ann_date"]).set_index("code")

    # 换手率：最近20日成交额均值 / 流通市值（★只取 tushare 源，amount 单位一致=千元；baostock 源 amount 单位=元会污染）
    dates = daily.index[-20:]
    dmin, dmax = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")
    codes_s = ",".join("'%s'" % c for c in codes)
    con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
    amt = pd.read_sql(f"SELECT code, AVG(amount) AS amt FROM daily_bar WHERE adjust='qfq' AND source='tushare' AND code IN ({codes_s}) AND date>='{dmin}' AND date<='{dmax}' GROUP BY code", con)
    con.close()
    amt["code"] = amt["code"].astype(str).str[:6]
    con = sqlite3.connect(f"file:{CACHE / 'hist_mv.db'}?mode=ro&immutable=1", uri=True)
    mv = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", con)
    con.close()
    mv["code"] = mv["code"].astype(str).str[:6]
    mv = mv.sort_values("month").drop_duplicates(subset="code", keep="last").set_index("code")
    turn = amt.set_index("code").join(mv["circ_mv"], how="inner")
    turn["turnover"] = turn["amt"] * 1000 / (turn["circ_mv"] * 1e8) * 100  # 日换手率%

    inflow, outflow = [], []
    for code in codes:
        c6 = code[:6]
        chg = float(gd.loc[c6, "chg_pct"]) if c6 in gd.index and pd.notna(gd.loc[c6, "chg_pct"]) else None
        name = names.get(code, "")
        tv = float(turn.loc[c6, "turnover"]) if c6 in turn.index and pd.notna(turn.loc[c6, "turnover"]) else None
        if chg is not None and chg > 10:
            inflow.append({"code": code, "name": name, "chg": round(chg, 1), "turnover": round(tv, 2) if tv is not None else None})
        elif chg is not None and chg < -10:
            outflow.append({"code": code, "name": name, "chg": round(chg, 1), "turnover": round(tv, 2) if tv is not None else None})
    inflow.sort(key=lambda x: -x["chg"])
    outflow.sort(key=lambda x: x["chg"])

    # 低换手（防守）Top：全板块按换手率升序，★剔除 ST + 亏损（ROE<=0）——"低换手好股"而非流动性枯竭的垃圾股
    lowturn = []
    n_filtered = 0
    for code in codes:
        c6 = code[:6]
        name = names.get(code, "")
        if name.startswith("ST") or name.startswith("*ST") or name.startswith("退"):
            n_filtered += 1
            continue
        roe = float(fr.loc[c6, "roe"]) if c6 in fr.index and pd.notna(fr.loc[c6, "roe"]) else None
        if roe is None or roe <= 0:
            n_filtered += 1
            continue
        tv = float(turn.loc[c6, "turnover"]) if c6 in turn.index and pd.notna(turn.loc[c6, "turnover"]) else None
        if tv is not None:
            lowturn.append({"code": code, "name": name, "turnover": round(tv, 2), "roe": round(roe * 100, 1)})
    lowturn.sort(key=lambda x: x["turnover"])

    return {"ok": True, "n_total": len(codes), "n_inflow": len(inflow), "n_outflow": len(outflow),
            "n_filtered": n_filtered,
            "inflow": inflow[:topn], "outflow": outflow[:topn], "lowturn": lowturn[:topn]}


def sector_crowd(sector):
    """热度板块因子（因子池最新研究 2026-08-15）：行业成交额占全市场比重的 60 日滚动分位
    ★结论：冷门板块（低拥挤）= 流动性陷阱；散户错误正确因子 = 个股低换手（turn_low），非板块热度"""
    daily = _daily()
    sec_map = _load_sector_detail()
    codes = [c for c, (sc, _) in sec_map.items() if sc == sector and c in daily.columns]
    if len(codes) < 5:
        return {"ok": False, "note": "板块成分不足"}
    dates = daily.index[-60:]
    dmin, dmax = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")
    all_codes = ",".join("'%s'" % c for c in daily.columns)
    sec_codes = ",".join("'%s'" % c for c in codes)
    con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
    tot = pd.read_sql(f"SELECT date, SUM(amount) AS amt FROM daily_bar WHERE adjust='qfq' AND source='tushare' AND code IN ({all_codes}) AND date>='{dmin}' AND date<='{dmax}' GROUP BY date", con)
    sec = pd.read_sql(f"SELECT date, SUM(amount) AS amt FROM daily_bar WHERE adjust='qfq' AND source='tushare' AND code IN ({sec_codes}) AND date>='{dmin}' AND date<='{dmax}' GROUP BY date", con)
    con.close()
    if len(tot) == 0 or len(sec) == 0:
        return {"ok": False, "note": "成交额数据缺失"}
    share = sec.set_index("date")["amt"] / tot.set_index("date")["amt"]
    cur = float(share.iloc[-1])
    avg = float(share.mean())
    # 热度分档（占全市场成交额比重）
    if cur > 0.05:
        heat = "热门"
    elif cur > 0.02:
        heat = "中性"
    else:
        heat = "冷门"
    return {"ok": True, "share_pct": round(cur * 100, 2), "avg_share_pct": round(avg * 100, 2),
            "heat": heat, "n": len(codes),
            "note": "★热度板块因子已证伪（2026-08-15 主系统 T+1 复算）：冷门板块=流动性陷阱；热门×低换手也不加分（复算 +41.96% ≈ 纯低换手 +42.18%）。散户错误唯一实证=个股低换手（turn_low）"}


if __name__ == "__main__":
    secs = list_sectors()
    print(f"板块数: {len(secs)}")
    for s in secs[:5]:
        print(f"  {s['code']} {s['name']} {s['n']} 只")
    t = sector_timing("C39")
    print("C39 择时:", t)
    print("C39 强因子:")
    for f in sector_strong_factors("C39"):
        print(f"  {f['name']:<8} n={f['n']:<6} 胜率={f['winrate']}% 超额={f['excess']}% {f['note']}")
