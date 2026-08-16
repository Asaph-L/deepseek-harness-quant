# -*- coding: utf-8 -*-
"""
factors/fscore.py — Piotroski F-Score（A 股减配版，2026-08-07）

数据：finance_quality.db（baostock profit/balance/cashflow 三接口，fetch_quality.py 补拉）
9 项评分（原版 Piotroski 2000；★标注为数据可得近似项）：
  P1 ROA > 0                    → roeAvg > 0（★ROE 近似 ROA）
  P2 经营现金流 > 0              → cfoToOR > 0 或 cfoToNP 非空（★近似）
  P3 ROA 改善                    → roeAvg 同比上升（当期 vs 上年同期）
  P4 应计利润 < 0                → cfoToNP > 1（★现金利润比近似，质量好）
  P5 杠杆下降                    → liabilityToAsset 同比下降
  P6 流动比率改善                → currentRatio 同比上升
  P7 无新股发行                  → 需 totalShare（暂缺 → 计 null 不参与）
  P8 毛利率改善                  → gpMargin 同比上升
  P9 资产周转率改善              → 需总资产（暂缺 → 计 null 不参与）

学术证据：Piotroski 2000（年化超额 +7.5%）；SJEMR 2022 中国实证独立有效；
清华 2022：A 股主要起降风险作用，叠加 BM/筹码才进攻（→ F-Score 作质量门槛而非进攻因子）。

用法：
  from factors.fscore import fscore, fscore_batch
  s = fscore("600519.SH")            # 单只
  df = fscore_batch(["600519.SH", ...])
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

QD_DB = Path(r"data/cache/finance_quality.db")


def _load(code):
    """code 的全部质量历史 → {statDate: dict}"""
    con = sqlite3.connect(str(QD_DB))
    rows = con.execute(
        "SELECT period, roe_avg, gp_margin, np_margin, current_ratio, "
        "liability_to_asset, cfo_to_np, cfo_to_or, pub_date FROM quality "
        "WHERE code=? ORDER BY period", (code,)).fetchall()
    con.close()
    out = {}
    for period, roe, gp, np_, cr, lta, cfo_np, cfo_or, pub in rows:
        out[period] = {
            "roe": roe, "gp": gp, "np": np_, "cr": cr, "lta": lta,
            "cfo_np": cfo_np, "cfo_or": cfo_or, "pub": pub,
        }
    return out


def fscore(code: str) -> dict:
    """单只 F-Score（9 项，缺失项计 null）"""
    hist = _load(code)
    periods = sorted(hist.keys())
    if not periods:
        return {"code": code, "score": None, "n_available": 0, "items": {}, "period": None}
    cur_p = periods[-1]
    cur = hist[cur_p]
    # 上年同期（同月，年份-1）
    prev_p = None
    y, m = cur_p[:4], cur_p[5:7]
    target = f"{int(y) - 1}-{m}"
    for p in periods:
        if p.startswith(target):
            prev_p = p
            break
    prev = hist.get(prev_p, {}) if prev_p else {}

    def chg(k):
        """同比变化：cur[k] - prev[k]；数据缺失返回 None"""
        a, b = cur.get(k), prev.get(k)
        if a is None or b is None:
            return None
        return a - b

    items = {
        "P1_roa_pos": 1 if (cur.get("roe") or 0) > 0 else 0,          # ROE>0 近似
        "P2_cfo_pos": 1 if (cur.get("cfo_or") or 0) > 0 or (cur.get("cfo_np") is not None and (cur.get("cfo_np") or 0) != 0) else 0,
        "P3_roa_imp": (1 if chg("roe") and chg("roe") > 0 else 0) if chg("roe") is not None else None,
        "P4_accrual": (1 if (cur.get("cfo_np") or 0) > 1 else 0) if cur.get("cfo_np") is not None else None,
        "P5_lever_dn": (1 if chg("lta") and chg("lta") < 0 else 0) if chg("lta") is not None else None,
        "P6_liq_up": (1 if chg("cr") and chg("cr") > 0 else 0) if chg("cr") is not None else None,
        "P7_no_issue": None,                                          # 数据缺口
        "P8_gp_imp": (1 if chg("gp") and chg("gp") > 0 else 0) if chg("gp") is not None else None,
        "P9_turn_imp": None,                                          # 数据缺口
    }
    avail = {k: v for k, v in items.items() if v is not None}
    score = sum(avail.values())
    return {"code": code, "score": score, "n_available": len(avail),
            "items": items, "period": cur_p,
            "roe": cur.get("roe"), "gp": cur.get("gp"),
            "lta": cur.get("lta"), "cr": cur.get("cr"),
            "cfo_np": cur.get("cfo_np"), "cfo_or": cur.get("cfo_or")}


def fscore_batch(codes) -> dict:
    return {c: fscore(c) for c in codes}


if __name__ == "__main__":
    codes = sys.argv[1:] or ["600519.SH", "000001.SZ", "000333.SZ"]
    for c in codes:
        r = fscore(c)
        print(f"{c}: F-Score {r['score']}/{r['n_available']} 期 {r['period']} "
              f"ROE {r.get('roe')} 毛利率 {r.get('gp')} 杠杆 {r.get('lta')}")
        print(f"  明细: {r['items']}")
