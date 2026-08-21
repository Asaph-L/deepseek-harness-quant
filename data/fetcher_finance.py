# -*- coding: utf-8 -*-
"""财报批量下载器（M2 收尾②，基本面因子数据源）

数据源：AkShare stock_financial_abstract_ths（同花顺财务摘要，免费）
入库：SQLite 新表 finance_report（本地缓存唯一读取原则）
自算：finance_calc.py 累计→单季差分、单季同比（C 因子核心，反向验证）

表结构 finance_report：
  code TEXT, period TEXT(报告期 YYYY-MM-DD), 净利润 REAL(亿), 营收 REAL(亿),
  单季净利 REAL, 单季同比 REAL, 单季营收 REAL, 单季营收同比 REAL,
  每股收益 REAL, ROE REAL, 来源 TEXT, PRIMARY KEY(code, period)

用法：
  python data/fetcher_finance.py --limit 50      # 小样本验证（只处理前 50 只）
  python data/fetcher_finance.py                 # 全量（约 40 分钟，幂等 PK code+period）
  python data/fetcher_finance.py --start 100     # 断点续传（从第 100 只开始）
  python data/fetcher_finance.py --status        # 查看进度
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import sqlite3
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from data.finance_calc import parse_num
from data.cache import CACHE_DIR

FIN_DB = str(CACHE_DIR / "finance.db")
LOG_FILE = BASE / "logs" / "finance_load.log"
PROGRESS_FILE = BASE / "logs" / "finance_progress.txt"
FAILED_FILE = BASE / "logs" / "finance_failed_codes.json"
UPSERT_SQL = """INSERT INTO finance_report
    (code,period,net_profit,revenue,sq_net_profit,sq_net_yoy,sq_revenue,sq_rev_yoy,eps,roe,source)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(code,period) DO UPDATE SET
      net_profit=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.net_profit
                      ELSE COALESCE(excluded.net_profit,finance_report.net_profit) END,
      revenue=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.revenue
                   ELSE COALESCE(excluded.revenue,finance_report.revenue) END,
      sq_net_profit=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.sq_net_profit
                         ELSE COALESCE(excluded.sq_net_profit,finance_report.sq_net_profit) END,
      sq_net_yoy=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.sq_net_yoy
                      ELSE COALESCE(excluded.sq_net_yoy,finance_report.sq_net_yoy) END,
      sq_revenue=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.sq_revenue
                      ELSE COALESCE(excluded.sq_revenue,finance_report.sq_revenue) END,
      sq_rev_yoy=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.sq_rev_yoy
                      ELSE COALESCE(excluded.sq_rev_yoy,finance_report.sq_rev_yoy) END,
      eps=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.eps
               ELSE COALESCE(excluded.eps,finance_report.eps) END,
      roe=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.roe
               ELSE COALESCE(excluded.roe,finance_report.roe) END,
      source=CASE WHEN finance_report.source LIKE '%tushare_v2' THEN finance_report.source
                  ELSE excluded.source END"""


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_db():
    con = sqlite3.connect(FIN_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS finance_report (
        code TEXT, period TEXT, net_profit REAL, revenue REAL,
        sq_net_profit REAL, sq_net_yoy REAL, sq_revenue REAL, sq_rev_yoy REAL,
        eps REAL, roe REAL, source TEXT,
        PRIMARY KEY (code, period))""")
    con.commit()
    return con


def fetch_one(code6: str) -> pd.DataFrame:
    """拉取单只股票的财务摘要（按报告期），返回原始 DataFrame"""
    import akshare as ak
    df = ak.stock_financial_abstract_ths(symbol=code6, indicator="按报告期")
    return df


def parse_finance(code6: str, df: pd.DataFrame) -> list:
    """原始财务摘要 → 入库行（含自算单季值）
    返回 [(code, period, ...), ...]"""
    if df is None or df.empty:
        return []

    # 定位列
    def col(df, *names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_period = col(df, "报告期", "报告日期", "日期")
    c_net = col(df, "净利润", "归母净利润")
    c_rev = col(df, "营业总收入", "营业收入", "主营收入")
    c_eps = col(df, "每股收益", "基本每股收益")
    c_roe = col(df, "净资产收益率", "ROE", "加权净资产收益率")
    if c_period is None or c_net is None:
        return []

    def money_to_yi(raw):
        """按原始单位标记换算为亿元；无单位数字按接口基础单位“元”处理。"""
        if raw is None or raw is False or isinstance(raw, bool):
            return None
        if isinstance(raw, str):
            s = raw.strip().replace(",", "")
            if not s or s.lower() in ("false", "nan", "none", "-"):
                return None
            try:
                if s.endswith("亿"):
                    return float(s[:-1])
                if s.endswith("万"):
                    return float(s[:-1]) / 10000.0
                if s.endswith("元"):
                    return float(s[:-1]) / 1e8
                return float(s) / 1e8
            except ValueError:
                return None
        try:
            return float(raw) / 1e8
        except (TypeError, ValueError):
            return None

    def safe_yoy(current, previous):
        if current is None or previous is None or abs(previous) < 0.01:
            return None
        return max(-10.0, min(10.0, (current - previous) / abs(previous)))

    rows = []
    cum_net = {}
    cum_rev = {}
    eps_map = {}
    roe_map = {}
    for _, r in df.iterrows():
        period = str(r[c_period])[:10]
        if not period or len(period) != 10:
            continue
        net = money_to_yi(r.get(c_net))
        rev = money_to_yi(r.get(c_rev)) if c_rev else None
        eps = parse_num(r.get(c_eps)) if c_eps else None
        roe = parse_num(r.get(c_roe)) if c_roe else None
        if net is None:
            continue
        cum_net[period] = net
        if rev is not None:
            cum_rev[period] = rev
        eps_map[period] = eps
        roe_map[period] = roe

    # 自算单季值（复用 finance_calc 逻辑）
    def single_quarter(cum_map):
        sq = {}
        for (y, m), v in sorted(cum_map.items()):
            if m == 3:
                # ★Q1 累计 = 单季本身（不扣上年年报！之前修过的坑）
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

    def to_ym(period):
        try:
            return (int(period[:4]), int(period[5:7]))
        except Exception:
            return None

    net_ym = {to_ym(p): v for p, v in cum_net.items() if to_ym(p)}
    rev_ym = {to_ym(p): v for p, v in cum_rev.items() if to_ym(p)}
    sq_net = single_quarter(net_ym)
    sq_rev = single_quarter(rev_ym)

    for period, net in sorted(cum_net.items()):
        ym = to_ym(period)
        if ym is None:
            continue
        sqn = sq_net.get(ym)
        # 单季同比：今年该季度 / 去年同季 - 1
        prev_ym = (ym[0] - 1, ym[1])
        prev_sqn = sq_net.get(prev_ym)
        sqn_yoy = safe_yoy(sqn, prev_sqn)

        sqr = sq_rev.get(ym)
        prev_sqr = sq_rev.get(prev_ym)
        sqr_yoy = safe_yoy(sqr, prev_sqr)

        rows.append((
            code6, period, net, cum_rev.get(period),
            sqn, sqn_yoy, sqr, sqr_yoy,
            eps_map.get(period), roe_map.get(period), "akshare_ths"
        ))
    return rows


def load_codes(limit=None):
    """从行情缓存取股票代码列表"""
    import sqlite3 as s3
    con = s3.connect(str(CACHE_DIR / "bars.db"))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    codes = [c.split(".")[0] for c in codes]  # 6位代码
    if limit:
        codes = codes[:limit]
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只")
    ap.add_argument("--start", type=int, default=0, help="从第 N 只开始")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry-failed", action="store_true", help="只补拉上次失败的代码（读 logs/finance_failed_codes.json）")
    args = ap.parse_args()

    if args.status:
        if PROGRESS_FILE.exists():
            print(PROGRESS_FILE.read_text(encoding="utf-8"))
        else:
            print("尚无财报下载进度")
        return 0

    # ★#394 失败代码落盘 + --retry-failed 定向补拉（免费接口限流常致大量失败，补拉避开全量重跑）
    if args.retry_failed:
        if not FAILED_FILE.exists():
            log("无失败清单文件，无需补拉")
            return 0
        import json as _json
        codes = _json.loads(FAILED_FILE.read_text(encoding="utf-8"))
        if not codes:
            log("失败清单为空，无需补拉")
            return 0
        log(f"定向补拉：{len(codes)} 只上次失败的代码")
    else:
        codes = load_codes(args.limit)
    total = len(codes)
    if args.start > 0:
        codes = codes[args.start:]
    log(f"财报批量下载: 共 {total} 只，本次处理 {len(codes)} 只（start={args.start}）")

    con = init_db()
    t0 = time.time()
    done, fail, empty = 0, [], 0

    for i, code in enumerate(codes):
        # ★#393 限流防护：0.30s 间隔把有效速率压到 ~2只/s（AkShare 同花顺免费接口限流——
        #   08-07 2.3只/s 成功、08-13 4.5只/s 触发 2992 失败；0.30s+网络~0.22s ≈ 1.9只/s 安全）
        time.sleep(0.30)
        try:
            df = fetch_one(code)
            rows = parse_finance(code, df)
            if not rows:
                empty += 1
                continue
            con.executemany(UPSERT_SQL, rows)
            con.commit()
            done += 1
        except Exception as e:
            # ★#393 限流重试一次（限流通常是瞬时的，等 2s 重试可挽回大部分）
            time.sleep(2)
            try:
                df = fetch_one(code)
                rows = parse_finance(code, df)
                if rows:
                    con.executemany(UPSERT_SQL, rows)
                    con.commit()
                    done += 1
                    continue
            except Exception:
                pass
            fail.append((code, str(e)[:60]))
        if (i + 1) % 20 == 0:
            el = time.time() - t0
            rate = (i + 1) / el
            eta = (len(codes) - i - 1) / rate if rate > 0 else 0
            log(f"[进度] {i+1}/{len(codes)} 成功{done} 空{empty} 失败{len(fail)} "
                f"速率{rate:.2f}只/s 剩余约{eta/60:.0f}分钟")
        if (i + 1) % 100 == 0:
            PROGRESS_FILE.write_text(
                f"更新 {datetime.now():%H:%M:%S} | 位置 {i+1}/{len(codes)} | 成功 {done} | 失败 {len(fail)}\n",
                encoding="utf-8")

    el = time.time() - t0
    n_rows = con.execute("SELECT COUNT(*) FROM finance_report").fetchone()[0]
    n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM finance_report").fetchone()[0]
    con.close()
    log(f"== 财报下载完成 == 处理 {len(codes)} 只，成功 {done}，空 {empty}，失败 {len(fail)}，"
        f"耗时 {el/60:.1f} 分钟 | 库内 {n_codes} 只 {n_rows} 行")
    if fail:
        log(f"失败清单（前20）: {fail[:20]}")
        # ★#394 失败代码落盘（供 --retry-failed 定向补拉）
        import json as _json
        FAILED_FILE.write_text(
            _json.dumps([c for c, _ in fail], ensure_ascii=False),
            encoding="utf-8")
    elif FAILED_FILE.exists():
        # 本次全部成功 → 清空失败清单
        FAILED_FILE.write_text("[]", encoding="utf-8")
    PROGRESS_FILE.write_text(
        f"最终 {datetime.now():%H:%M:%S} | 库内 {n_codes} 只 {n_rows} 行 | 本次成功 {done} 失败 {len(fail)}\n",
        encoding="utf-8")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
