# -*- coding: utf-8 -*-
"""财务自算器 DEMO：反向验证 AkShare 财务数据（茅台 + 宁德时代）

验证点：
1. 自算单季净利润（累计差分）与数据源累计值的数学一致性
2. 自算单季同比 vs 数据源累计同比（口径差异说明）
3. 近 3 年年度净利润 CAGR（A 因子）
4. ROE / 营收增速 参考
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.finance_calc import (build_factor_table, annual_cagr, parse_num,
                               check_consistency, quarter_month, to_cum_map)


def analyze(symbol, name):
    print("=" * 68)
    print(f"{name}（{symbol}）财务自算反向验证")
    print("=" * 68)
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    df = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
    print(f"数据范围: {df['报告期'].iloc[-1]} ~ {df['报告期'].iloc[0]}，共 {len(df)} 期")

    rows, cum_p = build_factor_table(df)
    recent = rows[-6:]

    print("\n[自算单季 vs 累计 vs 外部同比]")
    print(f"{'报告期':<12}{'累计净利润':>14}{'自算单季':>14}{'自算单季同比':>12}{'外部累计同比':>12}")
    for r in recent:
        def f(x, suf="%"):
            return f"{x*100:+.1f}{suf}" if x is not None else "-"
        print(f"{r['period']:<12}{r['cum_profit']/1e8 if r['cum_profit'] else 0:>12.2f}亿"
              f"{r['sq_profit']/1e8 if r['sq_profit'] else 0:>13.2f}亿"
              f"{f(r['sq_yoy']):>12}{f(r['ext_cum_yoy']):>12}")

    print("\n[口径说明] 自算=单季同比（欧奈尔 C 因子口径）；外部=累计同比。两者口径不同属正常。")

    cagr, pairs = annual_cagr(cum_p, n_years=3)
    if cagr is not None:
        print(f"\n[A 因子] 近 3 年年度净利润 CAGR = {cagr*100:+.1f}%")
        print(f"         年报净利润: " + " → ".join(f"{y}:{v/1e8:.1f}亿" for y, v in pairs))
    else:
        print("\n[A 因子] 近 3 年净利润 CAGR: 数据不足或基期为负")

    print("\n[一致性检查]")
    for n in check_consistency(recent):
        print(f"  · {n}")

    # ROE 参考（数据源双口径）
    roe_cols = [c for c in df.columns if "净资产收益率" in c]
    if roe_cols:
        latest = df.iloc[-1]
        vals = ", ".join(f"{c}={latest[c]}" for c in roe_cols if parse_num(latest[c]) is not None)
        print(f"\n[ROE 参考] 最新期({latest['报告期']}): {vals}")
    print()


if __name__ == "__main__":
    analyze("600519", "贵州茅台")
    analyze("300750", "宁德时代")
    print(">>> DEMO 完成：自算器与数据源口径差异已可视化，可作管道数据校验底座 <<<")
