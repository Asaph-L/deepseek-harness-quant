# -*- coding: utf-8 -*-
"""★2026-08-13 #293 市场知识库构建器（总指挥需求：因子实测 + 市场重要信息 → 供知识库 AI 主观分析）

扩展 unified.db 新增 4 表（每日快照，date 主键 upsert）：
  market_daily        每日市场快照（择时分/四维/风格适配/拥挤度/涨跌宽度/池子状态）——AI 主观分析核心
  market_style_daily  市场风格反推（外包 market_style_*.json 按日）
  factor_health_snap  因子健康快照（外包 health_*.csv 按日）
  timing_series       择时历史序列（date, score, level）

AI 导出：report/market_kb_dump.json（近 30 天快照 + 因子实测 + 风格 + 健康——给知识库 AI 做主观分析）

用法：python data/build_market_kb.py [--dump]
自动链：dev_auto 8.70（每日 18:30 后重建）
"""
import json
import sqlite3
import glob
import os
import re
import sys
import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
LOGS = BASE / "logs"
REPORT = BASE / "report"
OUTPUT = BASE / "output"
UNIFIED = Path(r"data/cache/unified.db")
EXT_HS = Path(r"data/factorpool/output/health")

TABLES = {
    "market_daily": """CREATE TABLE IF NOT EXISTS market_daily (
        date TEXT PRIMARY KEY, ts TEXT,
        timing_score REAL, timing_level TEXT,
        dim_policy REAL, dim_macro REAL, dim_emotion REAL, dim_width REAL,
        regime_fit TEXT, style_state TEXT,
        crowd_pctile REAL, crowd_n INTEGER,
        n_up INTEGER, n_down INTEGER, n_flat INTEGER, turnover_亿 REAL,
        n_opp INTEGER, n_pitch INTEGER, n_tech INTEGER,
        reason TEXT)""",
    "market_style_daily": """CREATE TABLE IF NOT EXISTS market_style_daily (
        date TEXT PRIMARY KEY, ts TEXT, top_style TEXT, raw TEXT)""",
    "factor_health_snap": """CREATE TABLE IF NOT EXISTS factor_health_snap (
        date TEXT, factor TEXT, status TEXT, icir60 REAL, icir120 REAL,
        PRIMARY KEY(date, factor))""",
    "timing_series": """CREATE TABLE IF NOT EXISTS timing_series (
        date TEXT PRIMARY KEY, score REAL, level TEXT, ts TEXT)""",
    # ★#294 Pitch v3 证据引擎权重（架构包容性：改优先级只改这张表/配置，不重做接口）
    #   backtest_w=回测证据权重（ICIR/胜率）/ live_w=实测证据权重（factor_pitch_perf T+1/T+5）
    #   min_live_samples=实测样本门槛——低于此数回测主导，达到后实测加权提升
    "evidence_weights": """CREATE TABLE IF NOT EXISTS evidence_weights (
        key TEXT PRIMARY KEY, value REAL, note TEXT, updated TEXT)""",
}


def _con():
    con = sqlite3.connect(str(UNIFIED), timeout=10)
    return con


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _latest(files, key=os.path.getmtime):
    files = list(files)
    if not files:
        return None
    return max(files, key=key)


def load_timing():
    """择时系统 → market_daily 核心字段"""
    p = OUTPUT / "timing_system.json"
    d = _read_json(p)
    if not d:
        return None
    dims = d.get("dims") or {}
    row = {
        "date": d.get("date"),
        "timing_score": d.get("score"),
        "timing_level": d.get("level"),
        "dim_policy": (dims.get("政策") or {}).get("score"),
        "dim_macro": (dims.get("宏观") or {}).get("score"),
        "dim_emotion": (dims.get("情绪") or {}).get("score"),
        "dim_width": (dims.get("宽度") or {}).get("score"),
        "regime_fit": json.dumps(d.get("regime_fit"), ensure_ascii=False)[:200] if d.get("regime_fit") else None,
        "style_state": json.dumps(d.get("style_state"), ensure_ascii=False)[:200] if d.get("style_state") else None,
        "reason": d.get("reason"),
    }
    return row


def load_crowding():
    """报告目录 factor_crowding_*.json → (pctile, n)"""
    fs = sorted(glob.glob(str(REPORT / "factor_crowding_*.json")), key=os.path.getmtime)
    d = _read_json(_latest(fs))
    if not d:
        return None, None
    # 结构：{factors: {因子: {pct_rank, action}}}——市场分位=各因子 pct_rank 平均，拥挤数=action 非 normal
    if isinstance(d.get("factors"), dict):
        ranks = []
        crowd_n = 0
        for _f, _v in d["factors"].items():
            if isinstance(_v, dict):
                if _v.get("pct_rank") is not None:
                    ranks.append(float(_v["pct_rank"]))
                if _v.get("action") not in (None, "normal"):
                    crowd_n += 1
        pct = round(sum(ranks) / len(ranks), 1) if ranks else None
        return pct, crowd_n
    pct = d.get("pctile_252", d.get("pctile", d.get("mkt_pctile")))
    n = d.get("n", d.get("count", len(d.get("crowded", [])) if isinstance(d.get("crowded"), list) else None))
    return pct, n


def load_market_width():
    """bars.db 算当日涨跌家数 + 成交额（immutable 读，快）"""
    try:
        from data.cache import DailyCache
        date = DailyCache().latest_trade_date()
        con = sqlite3.connect("file:data/cache/bars.db?mode=ro&immutable=1", uri=True, timeout=5)
        rows = con.execute(
            "SELECT SUM(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END), SUM(CASE WHEN pct_chg<0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN pct_chg=0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN source IN ('tushare','tushare_backup') THEN amount*1000 ELSE amount END) FROM daily_bar "
            "WHERE adjust='qfq' AND date=? AND code NOT LIKE 'SH.%' AND code NOT LIKE 'sh.%'", (date,)).fetchone()
        con.close()
        n_up, n_down, n_flat, amt_yuan = rows if rows else (None, None, None, None)
        # ★2026-08-15 单位归一：tushare 千元×1000→元，baostock/akshare 元；排除 SH.000300 指数行
        #   → 亿元 = amt_yuan / 1e8
        return date, n_up, n_down, n_flat, (round(amt_yuan / 1e8, 0) if amt_yuan else None)
    except Exception:
        return None, None, None, None, None


def load_pools():
    """池子快照：最新 opp_pool / pitch_v2 / tech_pitch 的 n"""
    def _n(files):
        f = _latest(files)
        if not f:
            return None
        d = _read_json(f)
        if not isinstance(d, dict):
            return None
        for k in ("n", "count"):
            if isinstance(d.get(k), int):
                return d[k]
        for k in ("items", "opps", "opportunities", "pitch", "tech", "entries"):
            v = d.get(k)
            if isinstance(v, list):
                return len(v)
        return None
    n_opp = _n(glob.glob(str(LOGS / "opp_pool_*.json")))
    n_pitch = _n(glob.glob(str(LOGS / "pitch_v2*.json")))
    n_tech = _n(glob.glob(str(LOGS / "tech_pitch*.json")))
    return n_opp, n_pitch, n_tech


def load_market_style_rows():
    """外包 health/market_style_*.json → 按日期行"""
    out = []
    for p in sorted(glob.glob(str(EXT_HS / "market_style_*.json"))):
        d = _read_json(p)
        if not d:
            continue
        date = str(d.get("date") or re.search(r"market_style_(\d{4}-\d{2}-\d{2})", os.path.basename(p)).group(1))
        top = None
        style = d.get("style") or d.get("top") or d.get("market_style")
        ts_ = d.get("top_styles")
        if isinstance(ts_, list) and ts_:
            top = str(ts_[0])
        elif isinstance(style, dict):
            top = str(style.get("top") or style.get("name") or list(style.keys())[:1])
        elif isinstance(style, str):
            top = style
        out.append({"date": date, "ts": d.get("ts", ""), "top_style": top, "raw": json.dumps(d, ensure_ascii=False)})
    return out


def load_health_rows():
    """外包 health_*.csv → 按日期因子健康（文件名含日期；同日多版取行数最多）"""
    by_date = {}
    for p in glob.glob(str(EXT_HS / "health_*.csv")):
        m = re.search(r"health_(\d{4}-\d{2}-\d{2})", os.path.basename(p))
        if not m:
            continue
        date = m.group(1)
        try:
            with open(p, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if date not in by_date or len(rows) > len(by_date[date][1]):
                by_date[date] = (p, rows)
        except Exception:
            continue
    out = []
    for date, (p, rows) in by_date.items():
        for r in rows:
            out.append({
                "date": date,
                "factor": str(r.get("factor") or r.get("code") or ""),
                "status": str(r.get("status") or "")[:20],
                "icir60": _num(r.get("icir60")),
                "icir120": _num(r.get("icir120")),
            })
    return out


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def load_timing_series():
    """timing_history（跨日序列）→ timing_series 行"""
    fs = sorted(glob.glob(str(LOGS / "timing_history*.json")), key=os.path.getmtime)
    d = _read_json(_latest(fs))
    out = []
    if d:
        ser = d.get("score_series") or d.get("series") or []
        for s in ser:
            if isinstance(s, dict):
                out.append({"date": s.get("date"), "score": s.get("score"), "level": s.get("level"), "ts": s.get("ts", "")})
            elif isinstance(s, (int, float)):
                out.append({"date": None, "score": s, "level": None, "ts": ""})
    return out


def build(con):
    for name, ddl in TABLES.items():
        con.execute(ddl)

    # ★#294 evidence_weights 默认权重（Pitch v3：实测数据未积累 → 回测主导；未来调权改此表）
    _now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _defaults = [
        ("backtest_w", 1.0, "回测证据权重（ICIR120/胜率标准化）——实测样本不足时主导"),
        ("live_w", 0.0, "实测证据权重（factor_pitch_perf T+1/T+5 实盘业绩）——数据积累后调高"),
        ("min_live_samples", 20, "实测样本门槛——单因子 pitch 次数低于此数回测主导"),
        ("live_strong_boost", 1.2, "实测强因子加成（实测 T+5 显著为正时 score×此系数）"),
        ("ai_pool_w", 1.0, "AI 主观池展示权重（页面显示不参与引擎排序，预留）"),
    ]
    for k, v, note in _defaults:
        con.execute("INSERT OR IGNORE INTO evidence_weights (key, value, note, updated) VALUES (?,?,?,?)",
                    (k, v, note, _now))

    # 1) market_daily（最新一天）
    t = load_timing()
    if t and t.get("date"):
        pct, n_crowd = load_crowding()
        wd, n_up, n_down, n_flat, amt = load_market_width()
        n_opp, n_pitch, n_tech = load_pools()
        date = t["date"]
        if wd and wd != date:
            n_up = n_down = n_flat = amt = None  # 宽度数据日与择时日不一致则留空（诚实）
        con.execute("""INSERT OR REPLACE INTO market_daily
            (date, ts, timing_score, timing_level, dim_policy, dim_macro, dim_emotion, dim_width,
             regime_fit, style_state, crowd_pctile, crowd_n, n_up, n_down, n_flat, turnover_亿,
             n_opp, n_pitch, n_tech, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date, t.get("ts", ""), t["timing_score"], t["timing_level"],
             t["dim_policy"], t["dim_macro"], t["dim_emotion"], t["dim_width"],
             t["regime_fit"], t["style_state"], pct, n_crowd,
             n_up, n_down, n_flat, amt, n_opp, n_pitch, n_tech, t["reason"]))

    # 2) market_style_daily
    for r in load_market_style_rows():
        con.execute("INSERT OR REPLACE INTO market_style_daily (date, ts, top_style, raw) VALUES (?,?,?,?)",
                    (r["date"], r["ts"], r["top_style"], r["raw"]))

    # 3) factor_health_snap
    con.execute("DELETE FROM factor_health_snap")
    for r in load_health_rows():
        con.execute("INSERT OR REPLACE INTO factor_health_snap (date, factor, status, icir60, icir120) VALUES (?,?,?,?,?)",
                    (r["date"], r["factor"], r["status"], r["icir60"], r["icir120"]))

    # 4) timing_series（历史有日期条目 + 当前择时兜底）
    for r in load_timing_series():
        if r.get("date"):
            con.execute("INSERT OR REPLACE INTO timing_series (date, score, level, ts) VALUES (?,?,?,?)",
                        (r["date"], r["score"], r["level"], r["ts"]))
    if t and t.get("date"):
        con.execute("INSERT OR REPLACE INTO timing_series (date, score, level, ts) VALUES (?,?,?,?)",
                    (t["date"], t["timing_score"], t["timing_level"], t.get("ts", "")))

    con.commit()


def dump_ai(con):
    """AI 导出：report/market_kb_dump.json（近 30 天快照 + 因子实测 + 风格 + 健康）"""
    md = con.execute("SELECT * FROM market_daily ORDER BY date DESC LIMIT 30").fetchall()
    cols = [c[1] for c in con.execute("PRAGMA table_info(market_daily)").fetchall()]
    md_rows = [dict(zip(cols, r)) for r in md]
    fp = con.execute("SELECT * FROM factor_agg ORDER BY n_pitch DESC LIMIT 30").fetchall()
    fcols = [c[1] for c in con.execute("PRAGMA table_info(factor_agg)").fetchall()]
    fp_rows = [dict(zip(fcols, r)) for r in fp]
    ms = con.execute("SELECT * FROM market_style_daily ORDER BY date DESC LIMIT 10").fetchall()
    mscols = [c[1] for c in con.execute("PRAGMA table_info(market_style_daily)").fetchall()]
    ms_rows = [dict(zip(mscols, r)) for r in ms]
    fh = con.execute("SELECT date, COUNT(*) n, SUM(CASE WHEN status LIKE '%有效%' THEN 1 ELSE 0 END) eff FROM factor_health_snap GROUP BY date ORDER BY date DESC LIMIT 7").fetchall()
    dump = {
        "generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "市场知识库 AI 视图——因子实测 + 市场快照 + 风格 + 健康。用于知识库 AI 主观分析。",
        "market_daily_recent": md_rows,
        "factor_perf_top": fp_rows,
        "market_style_recent": ms_rows,
        "factor_health_summary": [{"date": r[0], "n": r[1], "effective": r[2]} for r in fh],
    }
    out = REPORT / "market_kb_dump.json"
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, len(md_rows), len(fp_rows)


def main():
    con = _con()
    build(con)
    n_md = con.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0]
    n_ms = con.execute("SELECT COUNT(*) FROM market_style_daily").fetchone()[0]
    n_fh = con.execute("SELECT COUNT(*) FROM factor_health_snap").fetchone()[0]
    n_ts = con.execute("SELECT COUNT(*) FROM timing_series").fetchone()[0]
    con.close()
    print(f"✅ 市场知识库更新: market_daily {n_md} 天 | style {n_ms} | health_snap {n_fh} | timing_series {n_ts}")
    if "--dump" in sys.argv or len(sys.argv) == 1:
        con = _con()
        out, n30, nfp = dump_ai(con)
        con.close()
        print(f"✅ AI 导出: {out}（近 {n30} 天快照 + {nfp} 因子实测）")


if __name__ == "__main__":
    main()
