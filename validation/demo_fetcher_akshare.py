# -*- coding: utf-8 -*-
"""M1 数据管道 · AkShare 覆盖源 DEMO 验证（fetcher_akshare.py）

验证内容：
1. 新浪日线备源：600519.SH 单只拉取 → 缓存 → 秒回（与 baostock 交叉校验）
2. 同花顺财务摘要 → finance_calc 反向校验（自算单季同比 vs 外部累计同比）
3. 东财业绩预告 stock_yjyg_em 跑通
4. 东财机构调研 stock_jgdy_tj_em 跑通

运行：cd deepseek-harness-quant && .venv/Scripts/python.exe validation/demo_fetcher_akshare.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.cache import DailyCache
from data import fetcher_akshare as fa
from data import fetcher_baostock as fb
from data.finance_calc import build_factor_table, check_consistency, parse_num

CODE = "600519.SH"
START, END = "2024-01-01", "2024-03-31"   # 短区间，交叉校验用
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))


# ---------------- 1. 新浪日线备源 + 缓存 ----------------
print("=" * 70)
print("1) 新浪日线备源 fetch_daily + ensure_daily 缓存")
t0 = time.time()
df = fa.fetch_daily(CODE, START, END, adjust="qfq")
t_net = time.time() - t0
check("新浪日线拉取", df is not None and len(df) > 0,
      f"{len(df)} 行，用时 {t_net:.1f}s")
if df is not None and len(df) > 0:
    last = df.iloc[-1]
    check("列完整性", {"date", "open", "high", "low", "close", "preclose",
                         "volume", "amount", "turn", "pct_chg", "is_st"} <= set(df.columns))
    check("价格自洽", bool((df["high"] >= df["low"]).all()) and bool((df["close"] > 0).all()))
    check("pct_chg 自算自洽", abs(last["pct_chg"] - (last["close"] / last["preclose"] - 1)) < 1e-9)

# 缓存写入 + 秒回
cache = DailyCache()
t0 = time.time()
n = cache.put_daily(CODE, df, adjust="qfq", source="akshare")
t_put = time.time() - t0
check("缓存写入", n == len(df), f"{n} 行 upsert，用时 {t_put*1000:.0f}ms")

t0 = time.time()
df_c = fa.ensure_daily(CODE, START, END, adjust="qfq", cache=cache)
t_cached = time.time() - t0
check("缓存秒回(ensure_daily 二次零网络)", len(df_c) == len(df) and t_cached < 1.0,
      f"{len(df_c)} 行，用时 {t_cached*1000:.0f}ms")

# ---------------- 2. 与 baostock 交叉校验（多源双写校验雏形）----------------
print("=" * 70)
print("2) 交叉校验：baostock(主) vs akshare-新浪(备) 同区间收盘价")
df_bs = fb.ensure_daily(CODE, START, END, adjust="qfq", cache=cache)
if df_bs is not None and len(df_bs) > 0:
    m = df_bs[["date", "close"]].merge(df_c[["date", "close"]], on="date",
                                       suffixes=("_bs", "_ak"))
    if len(m) > 0:
        m["diff_pct"] = (m["close_bs"] - m["close_ak"]).abs() / m["close_bs"]
        max_diff = m["diff_pct"].max()
        mean_diff = m["diff_pct"].mean()
        ok = max_diff < 0.02  # 前复权口径差异容忍 2%
        check("双源收盘价交叉校验", ok,
              f"共 {len(m)} 个共同交易日，平均偏差 {mean_diff*100:.3f}%，最大偏差 {max_diff*100:.3f}%")
    else:
        check("双源交叉校验", False, "无共同交易日，无法校验")
else:
    check("双源交叉校验", False, "baostock 侧无数据（网络异常？）")

# ---------------- 3. 同花顺财务摘要 + finance_calc 反向校验 ----------------
print("=" * 70)
print("3) 同花顺财务摘要 → finance_calc 反向校验")
try:
    t0 = time.time()
    df_fin = fa.fetch_financial_abstract(CODE)
    check("财务摘要拉取", df_fin is not None and len(df_fin) > 0,
          f"{len(df_fin)} 期，用时 {time.time()-t0:.1f}s")
    if df_fin is not None and len(df_fin) > 0:
        rows, _ = build_factor_table(df_fin)
        check("自算因子表构建", len(rows) > 0, f"{len(rows)} 个报告期")
        notes = check_consistency(rows)
        for n in notes[-2:]:
            print("    " + n)
        # C 因子口径：最近单季同比是否可达
        latest = [r for r in rows if r["sq_yoy"] is not None]
        check("C 因子口径(自算单季同比)可达", len(latest) >= 4,
              f"最近可用单季同比 {len(latest)} 期，最新 {latest[-1]['period']} sq_yoy={latest[-1]['sq_yoy']*100:+.1f}%")
except Exception as e:
    check("财务摘要", False, f"{type(e).__name__}: {str(e)[:120]}")

# ---------------- 4. 业绩预告 / 机构调研 ----------------
print("=" * 70)
print("4) 东财业绩预告 / 机构调研")
try:
    t0 = time.time()
    df_yjyg = fa.fetch_yjyg("20251231")
    check("业绩预告拉取", df_yjyg is not None and len(df_yjyg) > 0,
          f"{len(df_yjyg)} 条，用时 {time.time()-t0:.1f}s")
    if df_yjyg is not None and len(df_yjyg) > 0:
        # 字段结构有效即可（某股某期是否发预告是数据事实，非管道能力）
        has_code = "股票代码" in df_yjyg.columns and df_yjyg["股票代码"].notna().sum() > 0
        has_amt = "业绩变动幅度" in df_yjyg.columns
        sample = df_yjyg.iloc[0]["股票简称"] if "股票简称" in df_yjyg.columns else "?"
        check("预告字段结构有效", has_code and has_amt,
              f"{len(df_yjyg)} 条，样例 {sample}")
except Exception as e:
    check("业绩预告", False, f"{type(e).__name__}: {str(e)[:120]}")

try:
    t0 = time.time()
    df_jgdy = fa.fetch_jgdy("20251231")
    check("机构调研拉取", df_jgdy is not None and len(df_jgdy) > 0,
          f"{len(df_jgdy)} 条，用时 {time.time()-t0:.1f}s")
    if df_jgdy is not None and len(df_jgdy) > 0:
        has_code = "代码" in df_jgdy.columns and df_jgdy["代码"].notna().sum() > 0
        has_cnt = "接待机构数量" in df_jgdy.columns
        sample = df_jgdy.iloc[0]["名称"] if "名称" in df_jgdy.columns else "?"
        check("调研字段结构有效", has_code and has_cnt,
              f"{len(df_jgdy)} 条，样例 {sample}")
except Exception as e:
    check("机构调研", False, f"{type(e).__name__}: {str(e)[:120]}")

# ---------------- 汇总 ----------------
print("=" * 70)
fails = [n for n, ok in RESULTS if not ok]
print(f"汇总：{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" +
      (f"，失败项: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
