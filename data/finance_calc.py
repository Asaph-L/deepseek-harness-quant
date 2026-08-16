# -*- coding: utf-8 -*-
"""自研财务关键指标计算器（反向验证数据源）

设计目标（用户指示：自己简单计算报表 → 反向验证外部数据源）：
1. 从基础财报数据（报告期累计值）自算关键指标：单季净利润、单季同比、年度 CAGR
2. 与外部数据源（AkShare）提供的指标对比，验证口径与可信度
3. 只算简单关键数据，先跑 demo

关键认知：
- AkShare 同花顺财务摘要的"净利润"是【报告期累计值】，"净利润同比增长率"是【累计同比】
- 我们的 C 因子（欧奈尔"当季 EPS 同比"）需要【单季值】→ 必须自算：本期累计 - 上期累计
- 自算单季同比 vs 数据源累计同比，口径不同 → 差异是预期的，这本身就是反向验证
"""
from datetime import datetime

# ---------------- 数值解析 ----------------

def parse_num(x):
    """解析 '1.47亿' / '46.84%' / False / 12345 等 → float 或 None"""
    if x is None or x is False:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s or s.lower() in ("false", "nan", "none", "-"):
        return None
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return None
    mul = 1.0
    if s.endswith("亿"):
        mul, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mul, s = 1e4, s[:-1]
    try:
        return float(s) * mul
    except ValueError:
        return None


def quarter_month(period):
    """报告期 'YYYY-MM-DD' → (year, month)；失败返回 None"""
    try:
        d = datetime.strptime(str(period)[:10], "%Y-%m-%d")
        return d.year, d.month
    except (ValueError, TypeError):
        return None


# ---------------- 核心计算 ----------------

def to_cum_map(periods, values):
    """构造 {(year, month): 累计值} 时间映射"""
    m = {}
    for p, v in zip(periods, values):
        ym = quarter_month(p)
        vv = parse_num(v)
        if ym and vv is not None:
            m[ym] = vv
    return m


def single_quarter_values(cum_map):
    """累计值 → 单季值：本期累计 - 上一报告期累计

    注意：Q1(03-31) 累计即单季（基期为 0，不可用上年年报做基期！）
    """
    sq = {}
    for (y, m), cum in sorted(cum_map.items()):
        if m == 3:
            base = 0.0                # Q1 累计 = 单季（第一季无更短报告期）
        elif m == 6:
            base = cum_map.get((y, 3))
        elif m == 9:
            base = cum_map.get((y, 6))
        elif m == 12:
            base = cum_map.get((y, 9))
        else:
            base = None
        sq[(y, m)] = (cum - base) if base is not None else None
    return sq


def yoy(series, key, period_len=1):
    """同比：(本期 - 去年同期) / |去年同期|；series 为 {(y,m): val}"""
    y, m = key
    cur = series.get(key)
    prev = series.get((y - 1, m))
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def annual_cagr(cum_map, n_years=3):
    """近 n 年年度净利润复合增速 CAGR：用年报(month=12)序列"""
    annual = {y: v for (y, m), v in cum_map.items() if m == 12}
    years = sorted(annual)
    if len(years) < n_years:
        return None, []
    last, first = annual[years[-1]], annual[years[-n_years]]
    if first is None or last is None or first <= 0:
        return None, [(y, annual[y]) for y in years[-n_years:]]
    cagr = (last / first) ** (1.0 / (n_years - 1)) - 1.0
    return cagr, [(y, annual[y]) for y in years[-n_years:]]


# ---------------- 因子口径输出 ----------------

def build_factor_table(df, profit_col="净利润", revenue_col="营业总收入",
                       ext_yoy_col="净利润同比增长率", ext_rev_yoy_col="营业总收入同比增长率"):
    """从 AkShare 财务摘要 df 构建自算因子表，并与外部指标对比

    返回 dict：periods / 累计 / 单季 / 自算单季同比 / 外部累计同比 / 自算营收同比 / ROE
    """
    periods = df["报告期"].tolist()
    profits = df[profit_col].tolist()
    revenues = df[revenue_col].tolist()
    ext_yoy = df[ext_yoy_col].tolist() if ext_yoy_col in df else [None] * len(df)

    cum_p = to_cum_map(periods, profits)
    cum_r = to_cum_map(periods, revenues)
    sq_p = single_quarter_values(cum_p)
    sq_r = single_quarter_values(cum_r)

    rows = []
    for (y, m) in sorted(cum_p):
        rows.append({
            "period": f"{y}-{m:02d}",
            "cum_profit": cum_p.get((y, m)),
            "sq_profit": sq_p.get((y, m)),
            "sq_yoy": yoy(sq_p, (y, m)),
            "sq_rev_yoy": yoy(sq_r, (y, m)),
        })
    # 外部同比（累计口径）按报告期对齐
    ext_map = {}
    for p, v in zip(periods, ext_yoy):
        ym = quarter_month(p)
        vv = parse_num(v)
        if ym and vv is not None:
            ext_map[ym] = vv
    for r in rows:
        y, m = int(r["period"][:4]), int(r["period"][5:7])
        r["ext_cum_yoy"] = ext_map.get((y, m))
    return rows, cum_p


def check_consistency(rows):
    """一致性检查：自算单季同比 vs 外部累计同比（预期口径不同，只做说明性对比）"""
    notes = []
    for r in rows[-4:]:
        sq, ext = r["sq_yoy"], r["ext_cum_yoy"]
        if sq is not None and ext is not None:
            diff = abs(sq - ext)
            tag = "接近" if diff < 0.05 else "差异(口径)"
            notes.append(f"{r['period']} 自算单季同比 {sq*100:+.1f}% vs 外部累计同比 {ext*100:+.1f}% [{tag}]")
    return notes
