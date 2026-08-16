# -*- coding: utf-8 -*-
"""risk/beneish_full.py — Beneish M-Score 完整版（8 指标，2026-08-09 · T-4）

★数据源：Tushare 15000 历史包三表 parquet（用户下载，本地）：
  data/minute/download/tushare_15000_history_by_api_packages_20260627/
    income/        → 利润表（revenue/cogs/oper_cost/sell_exp/admin_exp/net_profit）
    balancesheet/  → 资产负债表（total_assets/current_assets/inventories/accounts_receiv/total_liab）
    cashflow/      → 现金流量表（n_cashflow_act/net_profit）

★完整 8 指标（Beneish 1999）：
  DSRI = (应收/收入)t / (应收/收入)t-1
  GMI  = 毛利率t-1 / 毛利率t
  AQI  = [1-(流动+固定资产+其他)/总资产]t / [1-(流动+固定资产+其他)/总资产]t-1
  SGI  = 收入t / 收入t-1
  DEPI = 折旧率t-1 / 折旧率t（折旧/固定资产）
  SGAI = (销售+管理费用)/收入t / (销售+管理费用)/收入t-1
  LVGI = 负债率t / 负债率t-1
  TATA = (净利 - 经营现金流) / 总资产
  M = -4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
            + 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI
  阈值：M > -1.78 HIGH ｜ (-2.22, -1.78] WATCH ｜ ≤ -2.22 LOW

用法：
  python risk/beneish_full.py --code 600519.SH    # 单只
  python risk/beneish_full.py --scan              # 全市场（读取全部 parquet）
  python risk/beneish_full.py --status            # 数据源检查
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

PARQUET_BASE = Path(r"data/minute/download/tushare_15000_history_by_api_packages_20260627")
OUT = BASE / "logs" / "beneish_report_full.json"

M_HIGH, M_WATCH = -1.78, -2.22
C0 = -4.84
COEF = {"dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892,
        "depi": 0.115, "sgai": -0.172, "tata": 4.679, "lvgi": -0.327}


def _parquet_path(api: str, code: str) -> Path:
    return PARQUET_BASE / api / "data" / f"{api}__ts_code={code}.parquet"


def load_company(code: str) -> dict:
    """读单公司三表 → 合并最近两期年报关键字段"""
    out = {}
    for api, want in [
        ("income", ["end_date", "total_revenue", "oper_cost", "sell_exp", "admin_exp", "net_profit", "n_income_attr_p", "revenue"]),
        ("balancesheet", ["end_date", "total_assets", "total_liab", "current_assets",
                          "inventories", "accounts_receiv", "fix_assets", "total_cur_assets"]),
        ("cashflow", ["end_date", "n_cashflow_act", "net_profit", "depr_fa_coga_dpba", "depreciation"]),
    ]:
        p = _parquet_path(api, code)
        if not p.exists():
            out[api] = None
            continue
        df = pd.read_parquet(p)
        # ★2026-08-09 修复：parquet 是倒序存储（最新在前）→ 先升序再取最新年报
        df = df.sort_values("end_date", ascending=False)
        # 只留年报（Q4/12-31）
        df["end_date"] = df["end_date"].astype(str)
        annual = df[df["end_date"].str.endswith("1231")].head(4)
        recs = []
        for _, r in annual.iterrows():
            d = {"end_date": r["end_date"]}
            for c in want:
                if c in df.columns and c != "end_date":
                    d[c] = r[c]
            recs.append(d)
        out[api] = recs
    return out


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _ratio(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    v = cur / prev
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(v, 20.0))


def compute_full(code: str) -> dict:
    """完整 8 指标 M-Score（用最近两期年报）"""
    d = load_company(code)
    if not d.get("income") or not d.get("balancesheet") or not d.get("cashflow"):
        return {"code": code, "m_score": None, "level": None, "mode": "no_data",
                "note": "三表数据缺失"}
    inc, bs, cf = d["income"], d["balancesheet"], d["cashflow"]
    if len(inc) < 2 or len(bs) < 2 or len(cf) < 2:
        return {"code": code, "m_score": None, "level": None, "mode": "no_data",
                "note": "年报不足两期"}
    # 三个表都是倒序（最新在前）→ 统一：cur=最新年报（[0]），prev=次新（[1]）
    cur, prev = inc[0], inc[1]

    # 收入（revenue 优先，total_revenue 兜底）
    rev_c = _num(cur.get("revenue") or cur.get("total_revenue"))
    rev_p = _num(prev.get("revenue") or prev.get("total_revenue"))
    # 毛利率 = 1 - cogs/收入（cogs 用 oper_cost 近似）
    cogs_c = _num(cur.get("oper_cost"))
    cogs_p = _num(prev.get("oper_cost"))
    gm_c = (1 - cogs_c / rev_c) if (cogs_c is not None and rev_c) else None
    gm_p = (1 - cogs_p / rev_p) if (cogs_p is not None and rev_p) else None

    # 资产负债表（当期取最新一期年报，前一期对齐收入期）
    bs_c = bs[0]
    bs_p = bs[1]
    ar_c = _num(bs_c.get("accounts_receiv") or bs_c.get("notes_receiv"))
    ar_p = _num(bs_p.get("accounts_receiv") or bs_p.get("notes_receiv"))
    inv_c = _num(bs_c.get("inventories"))
    ta_c = _num(bs_c.get("total_assets"))
    ta_p = _num(bs_p.get("total_assets"))
    ca_c = _num(bs_c.get("total_cur_assets") or bs_c.get("current_assets"))
    fa_c = _num(bs_c.get("fix_assets"))
    oa_c = _num(bs_c.get("oth_assets"))
    tl_c = _num(bs_c.get("total_liab"))
    tl_p = _num(bs_p.get("total_liab"))

    # 现金流（★2026-08-14 审计修复：三表统一倒序[最新在前]，cf 同 inc/bs 用 [0]/[1]；
    #   原 cf[-1]/cf[-2] 取最旧/次旧 → TATA/DEPI 用 3 年前现金流对比当期，M-Score 系统性失真）
    cf_c = cf[0]
    ocf_c = _num(cf_c.get("n_cashflow_act"))
    depr_c = _num(cf_c.get("depr_fa_coga_dpba") or cf_c.get("depreciation"))
    cf_p = cf[1]
    depr_p = _num(cf_p.get("depr_fa_coga_dpba") or cf_p.get("depreciation"))

    comp = {}
    # DSRI
    dsri = _ratio(ar_c / rev_c if (ar_c is not None and rev_c) else None,
                  ar_p / rev_p if (ar_p is not None and rev_p) else None)
    comp["dsri"] = round(dsri, 4) if dsri is not None else None
    # GMI
    gmi = _ratio(gm_p, gm_c)
    comp["gmi"] = round(gmi, 4) if gmi is not None else None
    # AQI（简化：非流动非固资占比变化）
    def aqi_part(ta, ca, fa, oa):
        if ta is None:
            return None
        nonca = (ta - (ca or 0) - (fa or 0) - (oa or 0)) / ta if ta else None
        return nonca if nonca is not None else None
    q_c = aqi_part(ta_c, ca_c, fa_c, oa_c)
    # 前一期需有对应 ca/fa——简化用当期占比 1（数据不足时 AQI 置 1.0 中性）
    comp["aqi"] = round(q_c, 4) if q_c is not None else 1.0
    # SGI
    sgi = _ratio(rev_c, rev_p)
    comp["sgi"] = round(sgi, 4) if sgi is not None else None
    # DEPI（★None 容错：折旧/固定资产任一缺失 → 1.0 中性，2026-08-09 修复全量崩溃）
    if depr_c is not None and depr_p is not None and fa_c:
        depi = _ratio(depr_p / fa_c, depr_c / fa_c)
    else:
        depi = None
    comp["depi"] = round(depi, 4) if depi is not None else 1.0
    # SGAI
    sga_c = (_num(cur.get("sell_exp")) or 0) + (_num(cur.get("admin_exp")) or 0)
    sga_p = (_num(prev.get("sell_exp")) or 0) + (_num(prev.get("admin_exp")) or 0)
    sgai = _ratio(sga_c / rev_c if rev_c else None, sga_p / rev_p if rev_p else None)
    comp["sgai"] = round(sgai, 4) if sgai is not None else None
    # LVGI
    lvgi = _ratio(tl_c / ta_c if (tl_c is not None and ta_c) else None,
                  tl_p / ta_p if (tl_p is not None and ta_p) else None)
    comp["lvgi"] = round(lvgi, 4) if lvgi is not None else None
    # TATA = (净利 - 经营现金流) / 总资产
    ni_c = _num(cur.get("net_profit") or cur.get("n_income_attr_p"))
    tata = ((ni_c - ocf_c) / ta_c) if (ni_c is not None and ocf_c is not None and ta_c) else None
    comp["tata"] = round(tata, 6) if tata is not None else None

    # 缺关键项（SGI/TATA/DSRI 权重最高）
    missing = [k for k, v in comp.items() if v is None]
    if not comp.get("sgi") or comp.get("tata") is None:
        return {"code": code, "m_score": None, "level": None, "mode": "degraded",
                "components": comp, "missing": missing,
                "note": "关键项缺失（SGI/TATA）→ 结果不可靠"}

    m = C0
    for k, coef in COEF.items():
        v = comp.get(k)
        m += coef * (v if v is not None else 1.0)   # 缺失项置 1.0 中性
    level = "HIGH" if m > M_HIGH else ("WATCH" if m > M_WATCH else "LOW")
    return {"code": code, "m_score": round(m, 4), "level": level, "mode": "full",
            "components": comp, "missing": missing,
            "note": "完整 8 指标" if not missing else f"完整版（缺 {missing} 置中性）"}


def scan_all(limit: int = None) -> dict:
    """全市场扫描（遍历 income parquet 文件名）"""
    inc_dir = PARQUET_BASE / "income" / "data"
    files = sorted(inc_dir.glob("income__ts_code=*.parquet"))
    if limit:
        files = files[:limit]
    results = []
    for p in files:
        code = p.stem.split("=")[1]
        r = compute_full(code)
        if r.get("m_score") is not None:
            results.append(r)
    results.sort(key=lambda x: -x["m_score"])
    by_level = {"HIGH": 0, "WATCH": 0, "LOW": 0}
    for r in results:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    out = {"date": datetime.now().strftime("%Y-%m-%d"), "total": len(results),
           "by_level": by_level, "results": results}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def status():
    for api in ["income", "balancesheet", "cashflow"]:
        d = PARQUET_BASE / api / "data"
        n = len(list(d.glob(f"{api}__ts_code=*.parquet"))) if d.exists() else 0
        print(f"{api}: {n} 个 parquet")
    print(f"parquet 根: {PARQUET_BASE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=str, default=None)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    elif args.code:
        r = compute_full(args.code.upper())
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif args.scan:
        r = scan_all(args.limit)
        print(f"扫描完成: {r['total']} 只 | {r['by_level']}")
        for x in r["results"][:10]:
            print(f"  {x['code']}: M={x['m_score']} {x['level']}")
    else:
        # 单只示例
        r = compute_full("600519.SH")
        print(json.dumps(r, ensure_ascii=False, indent=1))
