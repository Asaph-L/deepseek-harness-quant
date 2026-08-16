# -*- coding: utf-8 -*-
"""Baostock 日线主源 DEMO：单只拉取 → 缓存写入 → 缓存读取 验证（M1 里程碑验收）

验证点：
1. 代码格式转换（600519.SH → sh.600519）
2. 单只股票 2010-01 至今前复权日线拉取（行数/覆盖范围合理性）
3. 本地 SQLite 缓存写入 + 唯一读取（二次读取零网络，秒回）
4. 数据质量抽查：复权价非负、high≥low、涨跌幅与 (close/preclose-1) 自洽
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.cache import DailyCache
from data.fetcher_baostock import ensure_daily, fetch_daily, to_bs_code

CHECK_OK = True


def check(cond, msg):
    global CHECK_OK
    tag = "PASS" if cond else "FAIL"
    if not cond:
        CHECK_OK = False
    print(f"  [{tag}] {msg}")


def main():
    code = "600519.SH"  # 贵州茅台：1990s 上市，2010 至今无缺失
    start, end = "2010-01-01", None  # end=None → 今天

    # 0) 代码格式转换
    print("== 代码格式转换 ==")
    check(to_bs_code("600519.SH") == "sh.600519", "600519.SH → sh.600519")
    check(to_bs_code("000001.SZ") == "sz.000001", "000001.SZ → sz.000001")
    try:
        to_bs_code("830799.BJ")
        check(False, "BJ 应报错（Baostock 不覆盖北交所）")
    except ValueError:
        check(True, "BJ 正确报错（Baostock 不覆盖北交所）")

    # 1) 首次拉取 + 写缓存
    cache = DailyCache()
    t0 = time.time()
    print("\n== 首次拉取（网络）==")
    df = ensure_daily(code, start, end, adjust="qfq", cache=cache)
    secs = time.time() - t0
    print(f"  行数 {len(df)}，耗时 {secs:.1f}s，覆盖 {df['date'].min()} ~ {df['date'].max()}")
    check(len(df) > 3000, f"2010 至今应 >3000 交易日（实际 {len(df)}）")
    check(df["date"].min() <= "2010-01-08", "起点不晚于 2010-01-08")

    # 2) 数据质量抽查
    print("\n== 数据质量抽查 ==")
    check((df["close"] > 0).all(), "close 全为正")
    check((df["high"] >= df["low"]).all(), "high >= low")
    check((df["low"] > 0).all(), "low 全为正")
    pct_self = (df["close"] / df["preclose"] - 1.0) * 100.0
    diff = (df["pct_chg"] - pct_self).abs().dropna()
    check((diff < 0.05).all(), "pct_chg 与 (close/preclose-1) 自洽（前复权口径）")
    check(df["is_st"].isin([0, 1]).all(), "is_st ∈ {0,1}")
    print(f"  最新 3 日: " + " | ".join(
        f"{r.date} 收{r.close:.2f} 涨{r.pct_chg:+.2f}%" for r in df.tail(3).itertuples()))

    # 3) 缓存二次读取（零网络）
    t0 = time.time()
    df2 = cache.get_daily(code, start="2024-01-01", end="2024-12-31", adjust="qfq")
    secs2 = time.time() - t0
    print("\n== 缓存读取 ==")
    meta = cache.get_meta(code, "qfq")
    print(f"  meta: {meta}")
    check(df2 is not None and len(df2) > 230, f"2024 年全年 ~244 交易日（实际 {len(df2) if df2 is not None else 0}）")
    check(secs2 < 0.5, f"缓存读取秒回（{secs2*1000:.0f}ms，无网络）")
    check(cache.covers(code, "2020-01-01", "2026-01-01", "qfq"), "covers() 判定 2020~2026 已全覆盖")

    # 4) 全覆盖时 ensure_daily 不触发网络（仍秒回）
    t0 = time.time()
    df3 = ensure_daily(code, "2015-01-01", "2020-12-31", adjust="qfq", cache=cache)
    secs3 = time.time() - t0
    check(secs3 < 0.5, f"缓存全覆盖时 ensure_daily 秒回（{secs3*1000:.0f}ms）")
    check(df3 is not None and len(df3) > 1400, "2015~2020 区间数据完整")

    print("\n" + ("=" * 52))
    print(">>> M1 单只日线跑通: " + ("全部 PASS ✅" if CHECK_OK else "存在 FAIL ❌") + " <<<")
    return 0 if CHECK_OK else 1


if __name__ == "__main__":
    sys.exit(main())
