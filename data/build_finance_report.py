# -*- coding: utf-8 -*-
"""data/build_finance_report.py — 用 Tushare 构建 finance.db 的 finance_report 表（2026-08-17 新增）

背景：strategy/pool_layers.py / ranking_v2.py / factors/opportunities/scan.py 依赖
      finance.db 的 finance_report 表（ROE/单季同比/营收），原由 fetcher_finance.py
      用 akshare 逐只拉（全市场 2-3 小时）。本脚本改用 Tushare income + fina_indicator，
      并行 8 线程，全市场 ~5 分钟，字段口径与 fetcher_finance 完全一致：
      (code6, period YYYY-MM-DD, net_profit亿, revenue亿, sq_net_profit, sq_net_yoy,
       sq_revenue, sq_rev_yoy, eps, roe, source)

用法：
  python data/build_finance_report.py              # 全市场
  python data/build_finance_report.py --limit 50   # 只建前 50 只（测试）
"""
import argparse
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _pro, _call
from data.cache import CACHE_DIR

FIN_DB = CACHE_DIR / "finance.db"
START = "20180101"
SOURCE = "tushare_v2"
RATIO_NORMALIZATION_VERSION = "tushare-ratios-v1"
MONEY_NORMALIZATION_VERSION = "legacy-money-yoy-v1"
CODE_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def init_db():
    con = sqlite3.connect(str(FIN_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS finance_report (
        code TEXT, period TEXT, net_profit REAL, revenue REAL,
        sq_net_profit REAL, sq_net_yoy REAL, sq_revenue REAL, sq_rev_yoy REAL,
        eps REAL, roe REAL, source TEXT,
        PRIMARY KEY (code, period))""")
    con.commit()
    return con


def to_ym(period):
    try:
        return (int(period[:4]), int(period[5:7]))
    except Exception:
        return None


def _finite_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _yuan_to_yi(value):
    """Tushare income 金额固定为元；finance_report 统一存亿元。"""
    value = _finite_float(value)
    return value / 1e8 if value is not None else None


def _roe_to_ratio(value):
    """Tushare ROE 字段为百分数；消费端统一使用小数。"""
    value = _finite_float(value)
    return value / 100 if value is not None else None


def _annualized_roe(row):
    """优先官方 roe_yearly；缺失时才按报告月年化累计 roe。"""
    yearly = _roe_to_ratio(row.get("roe_yearly"))
    if yearly is not None:
        return yearly
    cumulative = _roe_to_ratio(row.get("roe"))
    end = str(row.get("end_date") or "")
    if cumulative is None or len(end) < 6:
        return cumulative
    multiplier = {"03": 4.0, "06": 2.0, "09": 4.0 / 3.0, "12": 1.0}.get(end[4:6], 1.0)
    return cumulative * multiplier


def _safe_yoy(current, previous):
    """同比统一存小数；跳过近零分母，并将极端值限制在 ±1000%。"""
    current = _finite_float(current)
    previous = _finite_float(previous)
    if current is None or previous is None or abs(previous) < 0.01:  # 0.01 亿元 = 100 万元
        return None
    return max(-10.0, min(10.0, (current - previous) / abs(previous)))


def single_quarter(cum_map):
    """累计 → 单季（Q1=自身；其余 = 本期累计 - 上一季累计）"""
    sq = {}
    for (y, m), v in sorted(cum_map.items()):
        if m == 3:
            sq[(y, m)] = v
        elif m == 6:
            base = cum_map.get((y, 3))
            sq[(y, m)] = (v - base) if base is not None else None
        elif m == 9:
            base = cum_map.get((y, 6))
            sq[(y, m)] = (v - base) if base is not None else None
        elif m == 12:
            base = cum_map.get((y, 9))
            sq[(y, m)] = (v - base) if base is not None else None
    return sq


def fetch_one(ts_code: str):
    """单只 → finance_report 行列表；失败返回 []"""
    try:
        code6 = ts_code.split(".", 1)[0]
        pro = _pro()
        inc = _call(pro.income, ts_code=ts_code,
                    start_date=START,
                    fields="ts_code,end_date,total_revenue,n_income,basic_eps,report_type")
        if inc is None or inc.empty:
            return []
        inc = inc[inc["report_type"] == "1"]
        fi = _call(pro.fina_indicator, ts_code=ts_code,
                   start_date=START, fields="ts_code,end_date,roe_yearly,roe")
        roe_map = {}
        if fi is not None and len(fi):
            for _, r in fi.iterrows():
                roe_map[str(r["end_date"])] = _annualized_roe(r)

        cum_net, cum_rev, eps_map = {}, {}, {}
        for _, r in inc.iterrows():
            end = str(r["end_date"])
            p = f"{end[:4]}-{end[4:6]}-{end[6:]}"
            net = _yuan_to_yi(r.get("n_income"))
            rev = _yuan_to_yi(r.get("total_revenue"))
            if net is not None:
                cum_net[p] = net
            if rev is not None:
                cum_rev[p] = rev
            eps_map[p] = r.get("basic_eps")

        net_ym = {to_ym(p): v for p, v in cum_net.items() if to_ym(p)}
        rev_ym = {to_ym(p): v for p, v in cum_rev.items() if to_ym(p)}
        sq_net = single_quarter(net_ym)
        sq_rev = single_quarter(rev_ym)

        rows = []
        for period, net in sorted(cum_net.items()):
            ym = to_ym(period)
            if ym is None:
                continue
            sqn = sq_net.get(ym)
            prev_sqn = sq_net.get((ym[0] - 1, ym[1]))
            sqn_yoy = _safe_yoy(sqn, prev_sqn)
            sqr = sq_rev.get(ym)
            prev_sqr = sq_rev.get((ym[0] - 1, ym[1]))
            sqr_yoy = _safe_yoy(sqr, prev_sqr)
            end8 = period.replace("-", "")
            rows.append((code6, period, net, cum_rev.get(period),
                         sqn, sqn_yoy, sqr, sqr_yoy,
                         eps_map.get(period), roe_map.get(end8), SOURCE))
        return rows
    except Exception:
        return []


def load_codes(limit=None, explicit=None):
    if explicit:
        codes = [c.strip().upper() for c in explicit.split(",") if c.strip()]
        invalid = [c for c in codes if not CODE_RE.fullmatch(c)]
        if invalid:
            raise ValueError(f"非法 ts_code: {', '.join(invalid)}")
        return list(dict.fromkeys(codes))[:limit or None]
    bars_db = CACHE_DIR / "bars.db"
    con = sqlite3.connect(f"file:{bars_db}?mode=ro&immutable=1", uri=True)
    # 只取规范 Tushare 股票代码；排除指数等残留，同时覆盖北交所。
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar "
        "WHERE code LIKE '%.SH' OR code LIKE '%.SZ' OR code LIKE '%.BJ'").fetchall()]
    con.close()
    codes = sorted(set(codes))
    if limit:
        codes = codes[:limit]
    return codes


def _recompute_quarters(con):
    """按修复后的累计值重算单季值与同比，统一负基期的同比符号。"""
    rows = con.execute(
        "SELECT code, period, net_profit, revenue FROM finance_report ORDER BY code, period"
    ).fetchall()
    grouped = defaultdict(list)
    for code, period, net, revenue in rows:
        grouped[code].append((period, _finite_float(net), _finite_float(revenue)))

    updates = []
    for code, records in grouped.items():
        net_map = {to_ym(p): v for p, v, _ in records if to_ym(p) and v is not None}
        rev_map = {to_ym(p): v for p, _, v in records if to_ym(p) and v is not None}
        sq_net = single_quarter(net_map)
        sq_rev = single_quarter(rev_map)
        for period, _, _ in records:
            ym = to_ym(period)
            if ym is None:
                continue
            net = sq_net.get(ym)
            revenue = sq_rev.get(ym)
            updates.append((
                net,
                _safe_yoy(net, sq_net.get((ym[0] - 1, ym[1]))),
                revenue,
                _safe_yoy(revenue, sq_rev.get((ym[0] - 1, ym[1]))),
                code,
                period,
            ))
    con.executemany(
        "UPDATE finance_report SET sq_net_profit=?,sq_net_yoy=?,sq_revenue=?,sq_rev_yoy=? "
        "WHERE code=? AND period=?",
        updates,
    )
    return len(updates)


def repair_existing(con):
    """离线迁移旧 Tushare 库；每个版本有独立标记，可重复安全调用。"""
    con.execute("CREATE TABLE IF NOT EXISTS finance_meta (key TEXT PRIMARY KEY, value TEXT)")
    changed = {"roe": 0, "net_profit": 0, "revenue": 0, "quarters": 0}

    ratio_done = con.execute(
        "SELECT 1 FROM finance_meta WHERE key=?", (RATIO_NORMALIZATION_VERSION,)
    ).fetchone()
    if not ratio_done:
        n, raw_like = con.execute(
            "SELECT COUNT(roe), COALESCE(SUM(ABS(roe)>1),0) FROM finance_report "
            "WHERE source='tushare' AND roe IS NOT NULL"
        ).fetchone()
        # 旧库的 Tushare ROE 是百分数；新写入行带版本标记，永不进入此迁移。
        if n and raw_like / n > 0.5:
            cur = con.execute(
                "UPDATE finance_report SET roe=roe/100 "
                "WHERE source='tushare' AND roe IS NOT NULL"
            )
            changed["roe"] = cur.rowcount
        con.execute(
            "INSERT INTO finance_meta(key,value) VALUES (?,?)",
            (RATIO_NORMALIZATION_VERSION, datetime.now().isoformat(timespec="seconds")),
        )

    money_done = con.execute(
        "SELECT 1 FROM finance_meta WHERE key=?", (MONEY_NORMALIZATION_VERSION,)
    ).fetchone()
    if not money_done:
        # 两个旧写入器都曾用数值阈值猜单位。5000亿元高于 A 股正常累计
        # 净利润上界，50000亿元高于正常累计营收上界；次级条件处理
        # 1000～5000 且与已规范营收量纲明显不相容的净利润行。
        cur = con.execute(
            "UPDATE finance_report SET net_profit=net_profit/100000000.0 "
            "WHERE source IN ('tushare','akshare_ths') AND net_profit IS NOT NULL AND "
            "(ABS(net_profit)>5000 OR (ABS(net_profit)>1000 AND revenue IS NOT NULL "
            "AND revenue!=0 AND ABS(net_profit/revenue)>100))"
        )
        changed["net_profit"] = cur.rowcount
        cur = con.execute(
            "UPDATE finance_report SET revenue=revenue/100000000.0 "
            "WHERE source IN ('tushare','akshare_ths') AND revenue IS NOT NULL "
            "AND ABS(revenue)>50000"
        )
        changed["revenue"] = cur.rowcount
        changed["quarters"] = _recompute_quarters(con)
        con.execute(
            "INSERT INTO finance_meta(key,value) VALUES (?,?)",
            (MONEY_NORMALIZATION_VERSION, datetime.now().isoformat(timespec="seconds")),
        )

    con.commit()
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes", default=None, help="逗号分隔 ts_code；例如 600519.SH,920001.BJ")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only-missing", action="store_true",
                    help="仅拉库中从未出现的股票；默认会刷新全部目标以获取新季度")
    ap.add_argument("--resume-refresh", action="store_true",
                    help="续传本轮 v2 刷新，跳过已有 tushare_v2/mixed_tushare_v2 行的股票")
    ap.add_argument("--repair-existing", action="store_true",
                    help="一次性修复旧库的 ROE/同比口径，不访问网络")
    args = ap.parse_args()

    if args.repair_existing:
        con = init_db()
        changed = repair_existing(con)
        con.close()
        log(f"旧库口径修复完成：{changed}（重复执行将安全跳过）")
        return 0

    codes = load_codes(args.limit, args.codes)
    log(f"目标 {len(codes)} 只 | workers={args.workers}")

    con = init_db()
    repair_existing(con)
    done = {r[0] for r in con.execute("SELECT DISTINCT code FROM finance_report").fetchall()}
    if args.only_missing:
        todo = [c for c in codes if c.split(".", 1)[0] not in done]
        mode = "补缺"
    elif args.resume_refresh:
        refreshed = {r[0] for r in con.execute(
            "SELECT DISTINCT code FROM finance_report "
            "WHERE source IN ('tushare_v2','mixed_tushare_v2')"
        ).fetchall()}
        todo = [c for c in codes if c.split(".", 1)[0] not in refreshed]
        mode = "续传刷新"
    else:
        todo = codes
        mode = "刷新"
    log(f"已有 {len(done)} 只，本次{mode} {len(todo)} 只")

    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            rows = fut.result()
            c = futs[fut]
            if rows:
                con.executemany(
                    "INSERT INTO finance_report "
                    "(code,period,net_profit,revenue,sq_net_profit,sq_net_yoy,sq_revenue,"
                    "sq_rev_yoy,eps,roe,source) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code,period) DO UPDATE SET "
                    "net_profit=COALESCE(excluded.net_profit,finance_report.net_profit),"
                    "revenue=COALESCE(excluded.revenue,finance_report.revenue),"
                    "sq_net_profit=COALESCE(excluded.sq_net_profit,finance_report.sq_net_profit),"
                    "sq_net_yoy=COALESCE(excluded.sq_net_yoy,finance_report.sq_net_yoy),"
                    "sq_revenue=COALESCE(excluded.sq_revenue,finance_report.sq_revenue),"
                    "sq_rev_yoy=COALESCE(excluded.sq_rev_yoy,finance_report.sq_rev_yoy),"
                    "eps=COALESCE(excluded.eps,finance_report.eps),"
                    "roe=CASE WHEN excluded.roe IS NOT NULL THEN excluded.roe "
                    "WHEN finance_report.source IN ('tushare_v2','mixed_tushare_v2') "
                    "THEN finance_report.roe ELSE NULL END,"
                    "source=CASE WHEN "
                    "excluded.roe IS NULL OR "
                    "(excluded.eps IS NULL AND finance_report.eps IS NOT NULL) "
                    "THEN 'mixed_tushare_v2' ELSE excluded.source END",
                    rows,
                )
                con.commit()
                ok += 1
            else:
                fail += 1
            if i % 500 == 0 or i == len(todo):
                el = time.time() - t0
                log(f"[{i}/{len(todo)}] 成功 {ok} / 失败 {fail} | 均速 {i/el:.0f} 只/秒 | "
                    f"剩余 {el/i*(len(todo)-i)/60:.0f} 分钟")
    n = con.execute("SELECT COUNT(*) FROM finance_report").fetchone()[0]
    n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM finance_report").fetchone()[0]
    con.close()
    log(f"完成: 成功 {ok} / 失败 {fail} | finance_report {n} 行 / {n_codes} 只 | {FIN_DB}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
