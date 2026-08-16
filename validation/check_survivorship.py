# -*- coding: utf-8 -*-
"""
validation/check_survivorship.py — 幸存者偏差检查（第06课落地，待办队列 ★项）

背景：M2 股票列表来自 baostock query_stock_basic（当前在市股票），若不含退市股，
回测只用"活下来的股票"→ 收益虚高 50%+（第06课原文警示）。本脚本在 M2 完成后验收用，
也可随时运行（只读，不干扰后台下载）。

检查内容：
1. 退市股清单：akshare 沪深退市（东财 datacenter，python 可通，已实测）
2. 全市场列表（baostock 当前在市 5205 只）是否含退市股
3. 本地缓存 bars.db 已入库代码是否含退市股
4. 结论：幸存者偏差风险评级 + M2 补拉建议

用法：
  python validation/check_survivorship.py
"""
import os
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import akshare as ak
import yaml

CFG = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
CACHE_DIR = CFG["data"]["cache_dir"]


def norm(code):
    """归一为 6 位数字代码（容忍 sh.600519 / 600519.SH / 600519 三种格式）"""
    s = str(code).strip().upper()
    if "." in s:
        left, right = s.split(".", 1)
        # baostock: sh.600519 / tushare: 600519.SH
        return left.zfill(6) if right in ("SH", "SZ", "BJ") else right.zfill(6)
    return s.zfill(6)


def get_delisted():
    """沪深退市股清单（代码集合）+ 明细 DataFrame"""
    delisted = set()
    frames = []
    src = {}
    try:
        sh = ak.stock_info_sh_delist()
        col = "公司代码" if "公司代码" in sh.columns else sh.columns[0]
        codes = sh[col].astype(str).str.zfill(6)
        delisted |= set(codes)
        sh["code"] = codes
        sh["market"] = "SH"
        frames.append(sh)
        src["sh"] = len(sh)
    except Exception as e:
        print(f"[WARN] 上交所退市清单获取失败: {type(e).__name__} {str(e)[:100]}")
    try:
        sz = ak.stock_info_sz_delist()
        col = "证券代码" if "证券代码" in sz.columns else sz.columns[0]
        codes = sz[col].astype(str).str.zfill(6)
        delisted |= set(codes)
        sz["code"] = codes
        sz["market"] = "SZ"
        frames.append(sz)
        src["sz"] = len(sz)
    except Exception as e:
        print(f"[WARN] 深交所退市清单获取失败: {type(e).__name__} {str(e)[:100]}")
    detail = None
    if frames:
        import pandas as pd
        detail = pd.concat(frames, ignore_index=True)
    return delisted, src, detail


def get_listed_baostock(retries=3):
    """baostock 当前在市列表（M2 实际使用列表：type=1 股票 + status=1 在市，同 bulk_loader）

    注：后台 M2 下载并发占用 baostock 连接 → 加重试。
    """
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"login: {lg.error_msg}")
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                raise RuntimeError(f"query: {rs.error_msg}")
            codes = set()
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()          # code, code_name, ipoDate, outDate, type, status
                if len(row) < 6:
                    continue
                if row[4] != "1" or row[5] != "1":   # 仅股票 + 仅在市（同 bulk_loader）
                    continue
                codes.add(norm(row[0]))
            bs.logout()
            if len(codes) > 1000:                # 粗校验：正常应 5000+，防并发限流半截返回
                return codes, None
            raise RuntimeError(f"列表过短({len(codes)} 只)，疑似并发限流")
        except Exception as e:
            bs.logout()
            if attempt < retries - 1:
                import time
                time.sleep(2 * (attempt + 1))
            else:
                return None, str(e)
    return None, "重试耗尽"


def get_cached_codes():
    """本地缓存已入库代码（只读）"""
    db = os.path.join(CACHE_DIR, "bars.db")
    if not os.path.exists(db):
        return None, f"缓存不存在: {db}"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT code FROM daily_bar").fetchall()
        return {norm(r[0].split(".")[0]) for r in rows}, None
    finally:
        con.close()


def main():
    print("=" * 62)
    print("幸存者偏差检查（第06课：无退市股 → 回测收益虚高 50%+）")
    print("=" * 62)

    # 1. 退市股清单（持久化供 M2 补拉）
    delisted, src, detail = get_delisted()
    print(f"\n[1] 退市股清单: 沪 {src.get('sh', 0)} 只 / 深 {src.get('sz', 0)} 只 / 合计 {len(delisted)} 只")
    if detail is not None and len(detail):
        out_csv = os.path.join(CACHE_DIR, "delisted_list.csv")
        try:
            detail.to_csv(out_csv, index=False, encoding="utf-8-sig")
            print(f"    已保存补拉清单 → {out_csv}")
        except Exception as e:
            print(f"    [WARN] 清单保存失败: {e}")

    # 2. baostock 当前在市列表
    listed, err = get_listed_baostock()
    if listed is None:
        print(f"[WARN] baostock 列表获取失败: {err}（跳过 [2]）")
    else:
        print(f"[2] baostock 当前在市: {len(listed)} 只")
        in_listed = delisted & listed
        print(f"    其中含退市股: {len(in_listed)} 只 → {'❌ 列表污染（需清洗）' if in_listed else '✅ 列表干净（无退市股混入）'}")
        missing = delisted - listed
        print(f"    未覆盖退市股: {len(missing)} 只（如 000004 国华退 2026-07 终止上市）")

    # 3. 本地缓存
    cached, cerr = get_cached_codes()
    if cached is None:
        print(f"[3] 本地缓存: {cerr}")
        return
    print(f"[3] 本地缓存已入库: {len(cached)} 只")
    cached_delisted = delisted & cached
    print(f"    其中退市股: {len(cached_delisted)} 只 → "
          f"{'✅ 已有退市股（M2 列表或含退市）' if cached_delisted else '⚠️ 完全不含退市股（幸存者偏差确认）'}")
    if cached_delisted:
        print(f"    示例: {sorted(cached_delisted)[:10]}")

    # 4. 结论
    print("\n" + "=" * 62)
    if cached and not cached_delisted:
        print("结论: ⚠️ 幸存者偏差风险【已确认】")
        print("  - 当前缓存 100% 为在市股票，退市股覆盖 0 只")
        print("  - 影响: 回测只用幸存者 → 收益虚高（第06课: 可虚高 50%+）")
        print("  - 处置: M2 全量完成后，按 delisted_list.csv 补拉退市股历史（2019 至今）")
        print("  - ★补拉接口实测（2026-08-06）: 新浪 stock_zh_a_daily 对退市股 FAIL(KeyError)")
        print("    → 腾讯 stock_zh_a_hist_tx 可用（000004 国华退 2020至今 1533行 ✅ /")
        print("      600002 齐鲁退市 2000-2006 1485行 ✅）→ M2 后补拉用腾讯源")
    elif cached_delisted:
        print("结论: ✅ 缓存含退市股，幸存者偏差风险低")
    else:
        print("结论: ⚠️ 数据不足，无法判定")
    print("=" * 62)


if __name__ == "__main__":
    main()
