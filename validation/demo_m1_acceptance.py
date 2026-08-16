# -*- coding: utf-8 -*-
"""M1 里程碑验收：单只三源一致性 + 财报校验挂接

验证点（M1 验收标准：单只三源拉取 + 缓存读写跑通）：
1. 三源日线收盘价一致性：baostock(主) vs akshare(新浪备) vs tushare(校验)
2. 缓存读写：写入后二次读取秒回、covers 全覆盖判定
3. 财报校验挂接：finance_calc 自算单季同比 vs AkShare 外部指标（口径说明）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK = True


def check(cond, msg):
    global OK
    if not cond:
        OK = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")


def main():
    print("=" * 60)
    print("M1 里程碑验收：单只三源一致性 + 缓存 + 财报校验")
    print("=" * 60)
    import warnings
    warnings.filterwarnings("ignore")

    code = "600519.SH"
    start, end = "20260701", "20260806"

    # 1) 三源日线收盘价
    print("\n[1] 三源日线收盘价一致性（贵州茅台 7-8 月）")
    from data.fetcher_baostock import fetch_daily as bs_daily
    from data.fetcher_akshare import fetch_daily as ak_daily
    from data.fetcher_tushare import get_daily as ts_daily

    df_bs = bs_daily(code, start, end, adjust="none")     # 不复权，纯收盘价对比
    df_ak = ak_daily(code, start, end, adjust="")         # 新浪不复权
    df_ts = ts_daily(code, start, end)

    def close_map(df, date_col, close_col):
        m = {}
        for _, r in df.iterrows():
            d = str(r[date_col])
            if len(d) == 8 and d.isdigit():      # 'YYYYMMDD' → 'YYYY-MM-DD'
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            m[d[:10]] = float(r[close_col])
        return m

    m_bs = close_map(df_bs, "date", "close")
    m_ak = close_map(df_ak, "date", "close")
    m_ts = close_map(df_ts, "trade_date", "close")
    print(f"  baostock {len(m_bs)} 日 | akshare {len(m_ak)} 日 | tushare {len(m_ts)} 日")

    common = sorted(set(m_bs) & set(m_ts))
    check(len(common) >= 20, f"baostock×tushare 共同交易日 ≥20（实际 {len(common)}）")
    if common:
        max_diff = max(abs(m_bs[d] - m_ts[d]) / m_bs[d] for d in common)
        check(max_diff < 0.001, f"收盘价最大偏差 <0.1%（实际 {max_diff*100:.4f}%）")
    common2 = sorted(set(m_bs) & set(m_ak))
    if common2:
        max_diff2 = max(abs(m_bs[d] - m_ak[d]) / m_bs[d] for d in common2)
        check(max_diff2 < 0.001, f"baostock×新浪 最大偏差 <0.1%（实际 {max_diff2*100:.4f}%）")

    # 2) 缓存读写
    print("\n[2] 缓存读写（本地唯一读取接口）")
    from data.cache import DailyCache
    from data.fetcher_baostock import ensure_daily
    import time
    cache = DailyCache()
    t0 = time.time()
    df = ensure_daily(code, "2024-01-01", "2024-12-31", "qfq", cache=cache)
    secs = time.time() - t0
    check(df is not None and len(df) > 230, f"缓存覆盖 2024 全年（{len(df) if df is not None else 0} 行）")
    check(secs < 0.5, f"缓存读取秒回（{secs*1000:.0f}ms）")
    check(cache.covers(code, "2020-01-01", "2026-08-01", "qfq"), "covers() 2020~2026 全覆盖")

    # 3) 财报校验挂接（自算 vs 外部）
    print("\n[3] 财报校验挂接（finance_calc 自算 vs AkShare）")
    import akshare as ak
    from data.finance_calc import build_factor_table, check_consistency
    df_fin = ak.stock_financial_abstract_ths(symbol="600519", indicator="按报告期")
    rows, cum_p = build_factor_table(df_fin)
    notes = check_consistency(rows[-4:])
    check(len(notes) > 0, f"近 4 期自算 vs 外部对比生成（{len(notes)} 条）")
    for n in notes:
        print(f"    · {n}")

    print("\n" + "=" * 60)
    print(">>> M1 验收: " + ("全部 PASS ✅" if OK else "存在 FAIL ❌") + " <<<")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
