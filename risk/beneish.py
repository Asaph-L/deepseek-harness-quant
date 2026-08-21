# -*- coding: utf-8 -*-
"""risk/beneish.py — Beneish M-Score 财务造假检测（外包 AI · 2026-08-08）

★定位：风控层 R6 红旗（权重最高）。识别财务操纵（利润操纵/应计异常/营收膨胀）。

学术标准（M. Daniel Beneish, 1999）8 指标：
  DSRI 应收账款指数 = 应收/收入(t) ÷ 应收/收入(t-1)
  GMI  毛利率指数   = 毛利率(t-1) ÷ 毛利率(t)          （>1 = 毛利率恶化 → 操纵动机↑）
  AQI  资产质量指数 = 非流动资产剔除后资产占比变化
  SGI  销售增长指数 = 收入(t) ÷ 收入(t-1)               （>1.2 高增长压力↑）
  DEPI 折旧指数     = 折旧率(t-1) ÷ 折旧率(t)
  SGAI 销售管理费用指数 = (销售+管理费用)/收入(t) ÷ (销售+管理费用)/收入(t-1)
  LVGI 杠杆指数     = 负债率(t) ÷ 负债率(t-1)           （>1 杠杆上升 → 盈余管理动机↑）
  TATA 总应计/总资产 = (净利 - 经营现金流)/总资产        （越高应计利润越多 → 操纵嫌疑↑）

  M = -4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
            + 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI
  阈值：M > -1.78 大概率操纵；-2.22 < M ≤ -1.78 中嫌疑；M ≤ -2.22 正常

★数据降级路径（当前 finance.db 只有利润表摘要，quality 表两期）：
  - 完整 8 指标需资产负债表明细（应收/存货/折旧/总资产/SG&A）→ 数据未入库
  - 降级 M-Score 用可算项近似：
      GMI  ← quality.gp_margin 两期比
      SGI  ← finance.sq_rev_yoy（单季收入同比，sgi = 1 + yoy）
      LVGI ← quality.liability_to_asset 两期比
      TATA ← cfo_to_np 代理：tata_proxy = max(0, 1 - cfo_to_np)（净利无现金支撑部分占比）
      DSRI/AQI/DEPI/SGAI 数据不足 → 置 0（中性），明确标注 mode='degraded'
  - 完整公式函数留好接口（beneish_full()），资产负债表入库后直接切换

输出：logs/beneish_report.json（批量）+ 单只模式；接入 risk/stock_risk.py 作为 R6

用法：
  python risk/beneish.py --code 000001.SZ      # 单只
  python risk/beneish.py --scan                 # 批量（最新两期 quality 对齐）
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

QD_DB = r"data/cache/finance_quality.db"
FIN_DB = r"data/cache/finance.db"
OUT = BASE / "logs" / "beneish_report.json"

# 阈值（需求指定）
M_HIGH = -1.78      # M > -1.78 → 高操纵嫌疑
M_WATCH = -2.22     # -2.22 < M ≤ -1.78 → 中嫌疑
# 常数项与系数（Beneish 1999 标准）
C0 = -4.84
COEF = {"dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892,
        "depi": 0.115, "sgai": -0.172, "tata": 4.679, "lvgi": -0.327}


def _conn(db):
    return sqlite3.connect(db)


def _latest_two_periods() -> list:
    """quality 表最新两个覆盖期（最新期覆盖≥1000 才作为 cur；否则用覆盖最全两期）"""
    con = _conn(QD_DB)
    rows = con.execute(
        "SELECT period, COUNT(*) FROM quality WHERE period < '2026-07-01' "
        "GROUP BY period ORDER BY period DESC").fetchall()
    con.close()
    if not rows:
        return []
    if rows[0][1] >= 1000:
        return [rows[0][0], rows[1][0] if len(rows) > 1 else None]
    # 最新期覆盖不足 → 回退到覆盖最全的两期
    rows.sort(key=lambda x: -x[1])
    return [rows[0][0], rows[1][0] if len(rows) > 1 else None]


def _load_quality(period: str) -> dict:
    """period → {code: {gp, liab, cfo, roe}}（脏数据防御：liab>1.5 或 |gp|>1.0 跳过）"""
    if not period:
        return {}
    con = _conn(QD_DB)
    rows = con.execute(
        "SELECT code, gp_margin, liability_to_asset, cfo_to_np, roe_avg FROM quality WHERE period=?",
        (period,)).fetchall()
    con.close()
    d = {}
    for code, gp, liab, cfo, roe in rows:
        if (liab is not None and liab > 1.5) or (gp is not None and abs(gp) > 1.0):
            continue  # 百分数残留脏数据
        d[code] = {"gp": gp, "liab": liab, "cfo": cfo, "roe": roe}
    return d


def _load_fin_sgi() -> dict:
    """finance.db 最新期 sq_rev_yoy → {code6: sgi}；无同比则用 sq_revenue 相邻期比"""
    con = _conn(FIN_DB)
    period = con.execute("SELECT MAX(period) FROM finance_report").fetchone()[0]
    rows = con.execute(
        "SELECT code, sq_rev_yoy, sq_revenue FROM finance_report WHERE period=?",
        (period,)).fetchall()
    con.close()
    sgi = {}
    for code, yoy, rev in rows:
        code6 = str(code)[:6]
        v = None
        if yoy is not None and yoy == yoy and yoy > -0.99:
            v = 1.0 + float(yoy) / 100.0 if abs(yoy) > 1 else 1.0 + float(yoy)  # 兼容百分数/小数
        if v is not None:
            sgi[code6] = v
    return sgi


def _ratio(cur, prev, lo=0.0, hi=10.0):
    """安全比值 + winsorize 截断（防极端值爆炸；标准 Beneish 也做 1%-99% 缩尾）"""
    if cur is None or prev is None or prev == 0:
        return None
    v = cur / prev
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return max(lo, min(v, hi))


def _tata_proxy(cfo_to_np, roe):
    """TATA 代理（★量纲校准 2026-08-08）：
    真实 TATA = (净利-经营现金流)/总资产 ≈ (1 - cfo_to_np) × ROE
    （ROE = 净利/净资产，总资产 ≈ 净资产×(1+负债率)，用 ROE 做规模因子近似）
    cfo_to_np ≥ 1（现金覆盖净利）→ 应计≈0；cfo 越负应计越高
    """
    if cfo_to_np is None:
        return None
    if cfo_to_np >= 1.0:
        return 0.0
    scale = max(0.0, min(roe or 0.0, 0.5))          # ROE 截断 0~50%
    t = (1.0 - cfo_to_np) * scale
    return round(max(0.0, min(t, 1.0)), 4)


def beneish_degraded(gp_cur, gp_prev, liab_cur, liab_prev, cfo_cur, sgi, roe_cur=None) -> dict:
    """降级 M-Score（现有数据可算项）→ {m_score, level, components} 或 None（关键项缺失）"""
    comp = {}
    gmi = _ratio(gp_prev, gp_cur, 0.0, 10.0) if gp_cur is not None else None  # 毛利率(t-1)/毛利率(t)
    comp["gmi"] = round(gmi, 4) if gmi is not None else None
    lvgi = _ratio(liab_cur, liab_prev, 0.0, 10.0) if liab_cur is not None else None  # 负债率(t)/负债率(t-1)
    comp["lvgi"] = round(lvgi, 4) if lvgi is not None else None
    comp["sgi"] = round(sgi, 4) if sgi is not None else None
    comp["tata"] = _tata_proxy(cfo_cur, roe_cur)
    # 数据不足标注（DSRI/AQI/DEPI/SGAI 当前不可得）
    for k in ("dsri", "aqi", "depi", "sgai"):
        comp[k] = None

    # 至少要有 tata 才能算（TATA 系数最高，缺它结果无意义）
    if comp["tata"] is None:
        return None
    m = C0
    for k, coef in COEF.items():
        v = comp[k]
        if v is not None:
            m += coef * v
    level = "HIGH" if m > M_HIGH else ("WATCH" if m > M_WATCH else "LOW")
    missing = [k for k in comp if comp[k] is None]
    return {
        "m_score": round(m, 4),
        "level": level,
        "mode": "degraded",
        "components": comp,
        "missing": missing,
        "note": "降级 M-Score：GMI/SGI/LVGI/TATA 近似，DSRI/AQI/DEPI/SGAI 数据不足置中性"
                if missing else "降级 M-Score（全部可算项）",
    }


def beneish_full(components: dict) -> dict:
    """完整公式（8 指标齐全时用；资产负债表入库后启用）→ {m_score, level}"""
    m = C0
    for k, coef in COEF.items():
        v = components.get(k)
        if v is None:
            return None
        m += coef * v
    level = "HIGH" if m > M_HIGH else ("WATCH" if m > M_WATCH else "LOW")
    return {"m_score": round(m, 4), "level": level, "mode": "full", "components": components}


def check_one(code: str) -> dict:
    """单只 → {code, period, m_score, level, mode, components, note}；数据不足返回 level=None"""
    code = code.upper()
    periods = _latest_two_periods()
    if len(periods) < 2:
        return {"code": code, "period": None, "m_score": None, "level": None,
                "mode": "degraded", "components": {}, "note": "quality 表不足两期"}
    cur_p, prev_p = periods[0], periods[1]
    q_cur = _load_quality(cur_p)
    q_prev = _load_quality(prev_p)
    row = q_cur.get(code)
    if not row:
        return {"code": code, "period": cur_p, "m_score": None, "level": None,
                "mode": "degraded", "components": {}, "note": "最新期无 quality 数据"}
    prev = q_prev.get(code, {})
    sgi_map = _load_fin_sgi()
    r = beneish_degraded(row["gp"], prev.get("gp"), row["liab"], prev.get("liab"),
                         row["cfo"], sgi_map.get(code[:6]), row.get("roe"))
    if not r:
        return {"code": code, "period": cur_p, "m_score": None, "level": None,
                "mode": "degraded", "components": {}, "note": "关键项缺失（cfo_to_np 无）"}
    r["code"], r["period"] = code, cur_p
    return r


def compute_map(period: str = None) -> dict:
    """批量 → {code: {m_score, level, ...}}（scan_all 一次性取用，性能友好）"""
    periods = _latest_two_periods()
    if len(periods) < 2:
        return {}
    cur_p, prev_p = periods[0], periods[1]
    q_cur = _load_quality(cur_p)
    q_prev = _load_quality(prev_p)
    sgi_map = _load_fin_sgi()
    out = {}
    for code, row in q_cur.items():
        prev = q_prev.get(code, {})
        r = beneish_degraded(row["gp"], prev.get("gp"), row["liab"], prev.get("liab"),
                             row["cfo"], sgi_map.get(code[:6]), row.get("roe"))
        if r:
            r["code"], r["period"] = code, cur_p
            out[code] = r
    return out


def scan_all() -> dict:
    """批量 → 写 logs/beneish_report.json"""
    m = compute_map()
    by_level = {"HIGH": 0, "WATCH": 0, "LOW": 0}
    for r in m.values():
        if r["level"] in by_level:
            by_level[r["level"]] += 1
    out = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "mode": "degraded",
        "stats": {"total": len(m), **by_level},
        "thresholds": {"M_HIGH": M_HIGH, "M_WATCH": M_WATCH,
                       "note": "M > -1.78 高嫌疑；-2.22 < M ≤ -1.78 中嫌疑"},
        "results": sorted(m.values(), key=lambda x: -(x["m_score"] if x["m_score"] is not None else -999)),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Beneish M-Score（降级版）")
    ap.add_argument("--code", type=str, default=None)
    ap.add_argument("--scan", action="store_true")
    args = ap.parse_args()
    if args.code:
        r = check_one(args.code)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif args.scan:
        r = scan_all()
        print(f"Beneish 批量扫描: {r['stats']}")
        print("\nHIGH 嫌疑（M > -1.78）Top 10:")
        for x in [x for x in r["results"] if x["level"] == "HIGH"][:10]:
            print(f"  {x['code']} M={x['m_score']:.4f} {x['note'][:30]}")
        print(f"\n已存 {OUT}")
    else:
        ap.print_help()
