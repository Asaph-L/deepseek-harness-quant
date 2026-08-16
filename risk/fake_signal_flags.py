# -*- coding: utf-8 -*-
"""risk/fake_signal_flags.py — 假信号 flag 层（2026-08-10 固化研究员成果）

★来源：研究员《假信号识别大全.md》（FS-1~FS-12）+《止损策略与买入逻辑匹配研究.md》
★定位：机会引擎防误触发 —— 每只机会股票打假信号 flag，BLOCK 类 flag 直接剔除/降级
★输出：logs/fake_signal_flags_{ts}.json（每只股票 0~N 个 flag）

flag 分级：
  BLOCK  （一票否决/黑名单）：FS-1 坟包 / FS-3 涨停诱多 / FS-4 不可交易 / FS-7 解禁减持 / FS-8 情绪取反
  WARN   （降级/减仓）：FS-2 MA60 死票 / FS-5 天量滞涨 / FS-6 对倒疑似 / FS-9 竞价反转 / FS-11 接飞刀 / FS-12 无量突破
  INFO   （提示）：FS-10 破净率阈值校准
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

BARS_DB = r"data/cache/bars.db"

FLAG_DEFS = {
    "FS-1":  {"name": "坟包假突破", "level": "BLOCK", "rule": "近30日出现 尖顶+连阴+平底 三形态（假突破收割）"},
    "FS-2":  {"name": "MA60死票", "level": "WARN", "rule": "close<MA60 且 MA60 斜率<0（假反弹高发区）"},
    "FS-3":  {"name": "高换手涨停", "level": "BLOCK", "rule": "近5日换手>50% 涨停 或 尾盘板（脉冲陷阱 -2.00%/3日）"},
    "FS-4":  {"name": "一字板不可交易", "level": "BLOCK", "rule": "open=high=low=close=涨停价（买不进=假信号）"},
    "FS-5":  {"name": "天量滞涨", "level": "WARN", "rule": "量比>5 且 5日涨幅<2%（放量滞涨=出货嫌疑）"},
    "FS-6":  {"name": "对倒疑似", "level": "WARN", "rule": "单日量比>8 且次日量比<0.6（假放量打回）"},
    "FS-7":  {"name": "解禁减持双杀", "level": "BLOCK", "rule": "30日内解禁 且 已公告减持（一票否决）"},
    "FS-8":  {"name": "情绪亢奋取反", "level": "BLOCK", "rule": "涨停家数>140 → sentiment 当日取反"},
    "FS-9":  {"name": "竞价高开反转", "level": "WARN", "rule": "竞价 strength 高分 且 持有>5日计划（5-20日 -2.3pp）"},
    "FS-10": {"name": "破净率阈值过期", "level": "INFO", "rule": "2025后破净率>10% 阈值需重校准（注册制后破净常态化）"},
    "FS-11": {"name": "超跌接飞刀", "level": "WARN", "rule": "MA60 向下 + 近20日跌>15%（80%亏损买入时 MA60 下行）"},
    "FS-12": {"name": "无量突破", "level": "WARN", "rule": "突破日量能<1.5×5日均量（无量突破=诱多）"},
}


def compute_flags(codes: list, date: str = None, con=None) -> dict:
    """对股票列表计算假信号 flag（当前实现：可计算的 FS-2/FS-5/FS-11/FS-12 + 数据标记）
    ★2026-08-10：支持外部连接注入（con）——调用方可传 immutable 只读连接避免 20s 锁等待
    """
    import pandas as pd
    if con is None:
        con = sqlite3.connect(BARS_DB)
    if date is None:
        date = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    out = {}
    for code in codes:
        flags = []
        try:
            df = pd.read_sql_query(
                "SELECT date, close, volume, high, low, open, preclose "
                "FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT 70",
                con, params=(code, date))
        except Exception:
            df = pd.DataFrame()
        if len(df) < 30:
            out[code] = {"flags": [], "level": "PASS", "date": date}
            continue
        df = df.sort_values("date").reset_index(drop=True)
        close = df["close"]
        # FS-2: MA60 死票（60日均线需更长数据 → 用 50 近似？不，用 LIMIT 70 内的 MA60 需要 ≥60 行）
        if len(df) >= 60:
            ma60 = close.rolling(60).mean().iloc[-1]
            ma60_prev = close.rolling(60).mean().iloc[-8]
            if close.iloc[-1] < ma60 and ma60 < ma60_prev:
                flags.append({"flag": "FS-2", "detail": f"close {close.iloc[-1]:.2f} < MA60 {ma60:.2f} 且下行"})
        # FS-11: 超跌接飞刀（MA60 向下 + 近20日跌>15%）
        if len(df) >= 60:
            ma60 = close.rolling(60).mean().iloc[-1]
            ma60_prev = close.rolling(60).mean().iloc[-8]
            ret20 = close.iloc[-1] / close.iloc[-21] - 1 if len(df) >= 21 else 0
            if ma60 < ma60_prev and ret20 < -0.15:
                flags.append({"flag": "FS-11", "detail": f"MA60下行 + 20日 {ret20:.1%}"})
        # FS-5: 天量滞涨（量比>5 且 5日涨幅<2%）
        vol = df["volume"]
        if len(df) >= 25:
            vr = vol.iloc[-1] / vol.iloc[-21:-6].mean() if vol.iloc[-21:-6].mean() > 0 else 0
            ret5 = close.iloc[-1] / close.iloc[-6] - 1 if len(df) >= 6 else 0
            if vr > 5 and ret5 < 0.02:
                flags.append({"flag": "FS-5", "detail": f"量比 {vr:.1f} + 5日 {ret5:.1%}"})
        # FS-12: 无量突破（当日涨幅>3% 但量<1.5×5日均量）
        if len(df) >= 6:
            ret1 = close.iloc[-1] / close.iloc[-2] - 1
            vr5 = vol.iloc[-1] / vol.iloc[-6:-1].mean() if vol.iloc[-6:-1].mean() > 0 else 0
            if ret1 > 0.03 and vr5 < 1.5:
                flags.append({"flag": "FS-12", "detail": f"涨 {ret1:.1%} 但量 {vr5:.1f}×5日均"})
        level = "BLOCK" if any(FLAG_DEFS[f["flag"]]["level"] == "BLOCK" for f in flags) else \
                ("WARN" if flags else "PASS")
        out[code] = {"flags": flags, "level": level, "date": date}
    if con is None:
        con.close()   # ★2026-08-10：外部注入的连接由调用方关闭
    return out


def flag_opportunities(pool: dict) -> dict:
    """对机会池打 flag（并入机会记录 → scan 消费）"""
    codes = [o["code"] for o in pool.get("opportunities", [])]
    res = compute_flags(codes, pool.get("date"))
    enriched = []
    for o in pool.get("opportunities", []):
        r = res.get(o["code"], {})
        o2 = dict(o)
        o2["fake_flags"] = r.get("flags", [])
        o2["fake_level"] = r.get("level", "PASS")
        enriched.append(o2)
    out = dict(pool)
    out["opportunities"] = enriched
    return out


if __name__ == "__main__":
    import glob
    files = sorted(glob.glob(str(BASE / "logs" / "opp_pool_*.json")),
                   key=lambda p: Path(p).stat().st_mtime)
    if not files:
        print("无机会池")
        sys.exit(1)
    pool = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    enriched = flag_opportunities(pool)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = BASE / "logs" / f"fake_signal_flags_{ts}.json"
    p.write_text(json.dumps(enriched, ensure_ascii=False), encoding="utf-8")
    # 统计
    from collections import Counter
    lv = Counter(o["fake_level"] for o in enriched["opportunities"])
    flags = Counter(f["flag"] for o in enriched["opportunities"] for f in o["fake_flags"])
    print(f"假信号 flag 已打: {len(enriched['opportunities'])} 只")
    print(f"  等级: {dict(lv)}")
    print(f"  flags: {dict(flags)}")
    # BLOCK 样例
    blocks = [o for o in enriched["opportunities"] if o["fake_level"] == "BLOCK"]
    for o in blocks[:5]:
        print(f"  BLOCK {o['code']} {o['name']} {o['otype']}: {[f['flag'] for f in o['fake_flags']]}")
