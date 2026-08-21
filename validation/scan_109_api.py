# -*- coding: utf-8 -*-
"""
validation/scan_109_api.py — ★Tushare 主服务器 109 接口全量扫描（2026-08-07）

目的：把教程文档列出的 109 个 Pro 兼容接口逐一实测，输出能力矩阵
      （可用/未开通/失败 + 返回行数 + 耗时），供因子池与数据管道选源。

方法：串行调用（后台已有 6 并发任务，避免打爆服务器），每接口带超时重试。
输出：logs/api_scan_report.md（Markdown 矩阵）
用法：python validation/scan_109_api.py
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _ProShim

API_URL = "https://api.tushare.pro"
REPORT = BASE / "logs" / "api_scan_report.md"

# (接口名, 参数, 说明)
SCAN = [
    # 一、基础数据（9）
    ("trade_cal", dict(exchange="SSE", start_date="20260801", end_date="20260806"), "交易日历"),
    ("stock_basic", dict(exchange="", list_status="L", fields="ts_code,symbol,name"), "股票基础信息"),
    ("etf_basic", dict(market="SH", fields="ts_code,name"), "ETF基础信息"),
    ("etf_index", dict(market="SH"), "ETF基准指数"),
    ("opt_basic", dict(exchange="SSE", fields="ts_code,name"), "期权合约信息"),
    ("fund_basic", dict(market="E", fields="ts_code,name"), "公募基金列表"),
    ("index_basic", dict(market="CSI", fields="ts_code,name"), "指数基本信息"),
    ("namechange", dict(ts_code="000001.SZ"), "股票曾用名"),
    ("new_share", dict(start_date="20260701", end_date="20260806"), "IPO新股列表"),
    # 二、行情（15）
    ("daily", dict(trade_date="20260806"), "A股日线"),
    ("weekly", dict(ts_code="600519.SH", start_date="20260101", end_date="20260806"), "周线"),
    ("monthly", dict(ts_code="600519.SH", start_date="20250101", end_date="20260806"), "月线"),
    ("adj_factor", dict(trade_date="20260806"), "复权因子"),
    ("daily_basic", dict(trade_date="20260806"), "每日指标"),
    ("stk_limit", dict(ts_code="000001.SZ", start_date="20260801", end_date="20260806"), "涨跌停价格"),
    ("suspend_d", dict(trade_date="20260806"), "停复牌"),
    ("fund_daily", dict(ts_code="510300.SH", start_date="20260701", end_date="20260806"), "ETF日线"),
    ("fund_adj", dict(ts_code="510300.SH"), "基金复权因子"),
    ("etf_share_size", dict(trade_date="20260806"), "ETF份额"),
    ("index_daily", dict(ts_code="000300.SH", start_date="20260701", end_date="20260806"), "指数日线"),
    ("index_dailybasic", dict(trade_date="20260806"), "指数每日指标"),
    ("opt_daily", dict(trade_date="20260806"), "期权日线"),
    ("cb_daily", dict(trade_date="20260806"), "可转债行情"),
    ("hk_hold", dict(trade_date="20260806"), "沪深港股通持股"),
    # 三、财务和宏观（36）
    ("income", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "利润表"),
    ("income_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "利润表VIP"),
    ("balancesheet", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "资产负债表"),
    ("balancesheet_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "资产负债表VIP"),
    ("cashflow", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "现金流量表"),
    ("cashflow_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "现金流量表VIP"),
    ("forecast", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "业绩预告"),
    ("forecast_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "业绩预告VIP"),
    ("express", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "业绩快报"),
    ("express_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "业绩快报VIP"),
    ("dividend", dict(ts_code="600519.SH"), "分红送股"),
    ("fina_indicator", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "财务指标"),
    ("fina_indicator_vip", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "财务指标VIP"),
    ("fina_audit", dict(ts_code="600519.SH", period="20251231"), "财务审计意见"),
    ("fina_mainbz", dict(ts_code="600519.SH", period="20251231"), "主营业务构成"),
    ("fina_mainbz_vip", dict(ts_code="600519.SH", period="20251231"), "主营业务构成VIP"),
    ("disclosure_date", dict(end_date="20260831"), "财报披露计划"),
    ("eco_cal", dict(start_date="20260801", end_date="20260831"), "财经日历"),
    ("shibor", dict(start_date="20260701", end_date="20260806"), "Shibor利率"),
    ("shibor_quote", dict(start_date="20260701", end_date="20260806"), "Shibor报价"),
    ("shibor_lpr", dict(start_date="20260101", end_date="20260806"), "LPR利率"),
    ("libor", dict(start_date="20260701", end_date="20260806"), "Libor利率"),
    ("hibor", dict(start_date="20260701", end_date="20260806"), "Hibor利率"),
    ("wz_index", dict(start_date="20260701", end_date="20260806"), "温州民间借贷利率"),
    ("gz_index", dict(start_date="20260701", end_date="20260806"), "广州民间借贷利率"),
    ("cn_gdp", dict(start_year="2025"), "GDP"),
    ("cn_cpi", dict(start_m="202601", end_m="202606"), "CPI"),
    ("cn_ppi", dict(start_m="202601", end_m="202606"), "PPI"),
    ("cn_m", dict(start_m="202601", end_m="202606"), "货币供应量"),
    ("sf_month", dict(start_m="202601", end_m="202606"), "社融月度"),
    ("cn_pmi", dict(start_m="202601", end_m="202606"), "PMI"),
    ("us_tycr", dict(start_date="20260701", end_date="20260806"), "美债收益率曲线"),
    ("us_trycr", dict(start_date="20260701", end_date="20260806"), "美债实际收益率"),
    ("us_tbr", dict(start_date="20260701", end_date="20260806"), "美短债利率"),
    ("us_tltr", dict(start_date="20260701", end_date="20260806"), "美长债利率"),
    ("us_trltr", dict(start_date="20260701", end_date="20260806"), "美长债实际利率"),
    # 四、名单（2）
    ("stock_st", dict(start_date="20260801", end_date="20260806"), "ST名单"),
    ("stock_hsgt", dict(start_date="20260801", end_date="20260806"), "沪深港通名单"),
    # 五、股东/交易/两融（14）
    ("top10_holders", dict(ts_code="600519.SH", period="20251231"), "前十大股东"),
    ("top10_floatholders", dict(ts_code="600519.SH", period="20251231"), "前十大流通股东"),
    ("pledge_stat", dict(ts_code="600519.SH"), "股权质押统计"),
    ("pledge_detail", dict(ts_code="600519.SH"), "股权质押明细"),
    ("repurchase", dict(ann_date="20260806"), "股票回购"),
    ("share_float", dict(start_date="20260801", end_date="20260831"), "限售解禁"),
    ("block_trade", dict(trade_date="20260806"), "大宗交易"),
    ("stk_holdernumber", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "股东人数"),
    ("stk_holdertrade", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "股东增减持"),
    ("top_list", dict(trade_date="20260806"), "龙虎榜每日明细"),
    ("top_inst", dict(trade_date="20260806"), "龙虎榜机构明细"),
    ("margin", dict(trade_date="20260806"), "融资融券汇总"),
    ("margin_detail", dict(trade_date="20260806"), "融资融券明细"),
    ("margin_secs", dict(trade_date="20260806"), "融资融券标的"),
    # 六、特色（33）
    ("cyq_perf", dict(trade_date="20260806"), "每日筹码及胜率"),
    ("cyq_chips", dict(ts_code="600519.SH", trade_date="20260806"), "每日筹码分布"),
    ("stk_factor", dict(trade_date="20260806"), "股票技术因子"),
    ("stk_factor_pro", dict(trade_date="20260806"), "技术因子专业版"),
    ("report_rc", dict(ts_code="600519.SH"), "卖方盈利预测"),
    ("broker_recommend", dict(month="202607"), "券商每月荐股"),
    ("stk_surv", dict(ts_code="600519.SH", start_date="20250101", end_date="20260630"), "机构调研"),
    ("moneyflow", dict(trade_date="20260806"), "个股资金流向"),
    ("moneyflow_ths", dict(trade_date="20260806"), "资金流向THS"),
    ("moneyflow_dc", dict(trade_date="20260806"), "资金流向DC"),
    ("moneyflow_ind_ths", dict(trade_date="20260806"), "行业资金流THS"),
    ("moneyflow_ind_dc", dict(trade_date="20260806"), "板块资金流DC"),
    ("moneyflow_mkt_dc", dict(trade_date="20260806"), "大盘资金流DC"),
    ("moneyflow_hsgt", dict(trade_date="20260806"), "沪深港通资金流"),
    ("limit_list_ths", dict(trade_date="20260806"), "涨跌停THS"),
    ("limit_list_d", dict(trade_date="20260806"), "涨跌停列表"),
    ("limit_step", dict(trade_date="20260806"), "连板天梯"),
    ("limit_cpt_list", dict(trade_date="20260806"), "最强板块"),
    ("ths_hot", dict(trade_date="20260806"), "同花顺热榜"),
    ("dc_hot", dict(trade_date="20260806"), "东财热榜"),
    ("hm_list", dict(), "游资名录"),
    ("hm_detail", dict(trade_date="20260806"), "游资每日明细"),
    ("ths_index", dict(exchange="A"), "同花顺概念指数"),
    ("ths_daily", dict(ts_code="885001.TI", start_date="20260801", end_date="20260806"), "同花顺板块行情"),
    ("ths_member", dict(ts_code="885001.TI"), "同花顺板块成分"),
    ("dc_index", dict(), "东财概念板块"),
    ("dc_daily", dict(ts_code="BK0475", start_date="20260801", end_date="20260806"), "东财板块行情"),
    ("dc_member", dict(ts_code="BK0475"), "东财板块成分"),
    ("tdx_index", dict(), "通达信板块"),
    ("tdx_daily", dict(ts_code="880001.TI", start_date="20260801", end_date="20260806"), "通达信板块行情"),
    ("tdx_member", dict(ts_code="880001.TI"), "通达信板块成分"),
    ("kpl_list", dict(trade_date="20260806"), "开盘啦榜单"),
    ("kpl_concept_cons", dict(ts_code="KPL003", trade_date="20260806"), "开盘啦题材成分"),
]


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)


def main():
    import yaml
    cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))["data"]
    pro = _ProShim(cfg["tushare_token"], cfg.get("tushare_api_url", API_URL))

    results = []
    log(f"开始扫描 {len(SCAN)} 个接口（串行，后台任务继续跑）")
    t_all = time.time()
    for i, (api, params, desc) in enumerate(SCAN, 1):
        fn = getattr(pro, api, None)
        if fn is None:
            results.append((api, desc, "FAIL", "无此方法", 0, 0))
            continue
        t0 = time.time()
        try:
            df = fn(**params)
            dt = time.time() - t0
            n = 0 if df is None else len(df)
            results.append((api, desc, "OK", "", n, dt))
            log(f"[{i}/{len(SCAN)}] OK   {api:22s} {n:>6} 行  {dt:5.1f}s  {desc}")
        except Exception as e:
            dt = time.time() - t0
            msg = str(e)[:80]
            if "not purchased" in msg or "权限" in msg or "积分" in msg:
                status = "NOT_PURCHASED"
            else:
                status = "FAIL"
            results.append((api, desc, status, msg, 0, dt))
            log(f"[{i}/{len(SCAN)}] {status} {api:22s} {dt:5.1f}s  {msg[:60]}")
        time.sleep(0.1)  # 轻微节流

    # 汇总报告
    ok = [r for r in results if r[2] == "OK"]
    np_ = [r for r in results if r[2] == "NOT_PURCHASED"]
    fail = [r for r in results if r[2] == "FAIL"]
    lines = [f"# Tushare 主服务器 109 接口扫描报告（{datetime.now():%Y-%m-%d %H:%M}）", ""]
    lines.append(f"- 总计 {len(results)} | ✅可用 {len(ok)} | 🔒未开通 {len(np_)} | ❌失败 {len(fail)}")
    lines.append(f"- 总耗时 {(time.time()-t_all)/60:.1f} 分钟（串行）")
    lines.append("")
    lines.append("## ✅ 可用接口")
    lines.append("| 接口 | 说明 | 样本行数 | 耗时 |")
    lines.append("|---|---|---|---|")
    for api, desc, _, _, n, dt in sorted(ok, key=lambda r: -r[4]):
        lines.append(f"| `{api}` | {desc} | {n} | {dt:.1f}s |")
    lines.append("")
    lines.append("## 🔒 未开通（需 186 全量档，找客服）")
    lines.append("| 接口 | 说明 | 返回 |")
    lines.append("|---|---|---|")
    for api, desc, _, msg, _, _ in np_:
        lines.append(f"| `{api}` | {desc} | {msg[:50]} |")
    lines.append("")
    lines.append("## ❌ 失败（参数问题，需调参重试）")
    for api, desc, _, msg, _, _ in fail:
        lines.append(f"- `{api}` {desc}: {msg}")
    lines.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    log(f"报告已写入 {REPORT}")
    log(f"汇总: {len(ok)} 可用 / {len(np_)} 未开通 / {len(fail)} 失败")


if __name__ == "__main__":
    main()
