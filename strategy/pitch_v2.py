# -*- coding: utf-8 -*-
"""strategy/pitch_v2.py — ★机会型 Pitch v2（Deck 审批输出 · 2026-08-08）

定位：机会引擎（scan.py --pitch 四重过滤）输出的候选 → 逐只补齐
  「1/2/3 年持有回测（含择时）+ 风控明细 + 买入理由」→ logs/pitch_deck.json
  → 外包 #1 的 Deck 审批界面读取渲染（买入/放弃 → deck_decisions.json）

数据流：
  logs/opportunity_pool.json (机会引擎) 
      → pitch_v2.py 逐只 _hist_holdout 回测 + check_one 风控
      → logs/pitch_deck.json {date, deck: [{code, name, otype, score, ...,
         hist:{1y:{avg,med,win,n},2y,3y}, risk:{level,score,flags}, thesis}]}
      → Deck 界面 → 你审批 → deck_decisions.json

用法：
  python strategy/pitch_v2.py                 # 从机会引擎输出构建 Deck
  python strategy/pitch_v2.py --force         # 忽略机会引擎缓存，重新扫描
  python strategy/pitch_v2.py --status        # 查看 Deck 状态
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np

OP_POOL_GLOB = BASE / "logs" / "opp_pool_*.json"
DECK_OUT = BASE / "logs" / f"pitch_deck_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
DECISIONS = BASE / "logs" / "deck_decisions.json"
BARS_DB = r"data\cache\bars.db"


def get_latest_pool() -> Path:
    """取最新的 opp_pool_*.json（每日/每次运行新文件策略）"""
    files = sorted(OP_POOL_GLOB.parent.glob(OP_POOL_GLOB.name))
    return files[-1] if files else None


def log(msg):
    print(msg, flush=True)


# ---------- 数据 ----------

def _load_deck_pool() -> list:
    """读机会引擎最新的 Pitch 候选"""
    p = get_latest_pool()
    if not p:
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("pitch", [])


def load_index() -> dict:
    """沪深300 收盘（择时修正基准）"""
    con = sqlite3.connect(BARS_DB)
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none'").fetchall()
    con.close()
    return {d: c for d, c in rows}


def load_dynamic_pos() -> dict:
    """动态择时月度仓位 {YYYY-MM: pos}"""
    p = BASE / "output" / "dynamic_regime.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {mo: float(v["pos"]) for mo, v in d.items()}
    except Exception:
        return {}


def _hist_holdout(code: str, years: int, idx: dict, dyn: dict) -> dict:
    """买入持有历史回测（含择时）：2020-2025 每季初买入持有 N 年
    复用 strategy/pitch.py 口径：季初买入 → 持有至 N 年后 → 年化收益
    择时修正：持有期逐月 pos 复利（简化，与 pitch.py 一致）
    Returns {avg, med, win, n}（% 年化）
    """
    con = sqlite3.connect(BARS_DB)
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code=? AND adjust='qfq' ORDER BY date",
        (code,)).fetchall()
    con.close()
    if len(rows) < 300:
        return {"avg": None, "med": None, "win": None, "n": 0}
    dates = [d for d, _ in rows]
    closes = {d: c for d, c in rows}
    idx_dates = sorted(idx.keys())
    rets = []
    # 每季初（1/4/7/10 月的首个交易日）买入
    for y in range(2020, 2026):
        for m in (1, 4, 7, 10):
            buy_d = next((d for d in dates if d.startswith(f"{y}-{m:02d}")), None)
            if not buy_d:
                continue
            # 目标日期 = 买入日 + years 年
            ty = y + years
            tm = m
            if tm > 12:
                tm -= 12
                ty += 1
            sell_d = next((d for d in dates if d.startswith(f"{ty}-{tm:02d}")), None)
            if not sell_d or sell_d <= buy_d:
                continue
            buy_p, sell_p = closes.get(buy_d), closes.get(sell_d)
            if not buy_p or not sell_p or buy_p <= 0:
                continue
            # 年化收益
            days_held = max(len([d for d in dates if buy_d <= d <= sell_d]), 1)
            total = sell_p / buy_p - 1
            annual = (1 + total) ** (252 / days_held) - 1 if total > -1 else -1
            # 择时修正：持有期月度仓位均值
            months_held = {d[:7] for d in dates if buy_d <= d <= sell_d}
            pos_avg = np.mean([dyn.get(m, 1.0) for m in months_held]) if dyn else 1.0
            annual_adj = annual * pos_avg if pos_avg > 0 else annual
            rets.append(annual_adj * 100)
    if not rets:
        return {"avg": None, "med": None, "win": None, "n": 0}
    return {"avg": round(float(np.mean(rets)), 1),
            "med": round(float(np.median(rets)), 1),
            "win": round(float(np.mean([1 if r > 0 else 0 for r in rets])), 3),
            "n": len(rets)}


def build_deck(force: bool = False) -> dict:
    deck = _load_deck_pool()
    if not deck:
        return {"error": "机会引擎无 Pitch 候选（先跑 scan.py --pitch）", "date": datetime.now().strftime("%Y-%m-%d")}

    idx = load_index()
    dyn = load_dynamic_pos()

    # 风控（批量模式：单连接一次加载，避免逐只开连接被安全层拦截）
    try:
        from factors.opportunities.scan import batch_check_one
    except Exception:
        batch_check_one = None

    out_deck = []
    for o in deck:
        code = o["code"]
        # 历史回测 1/2/3 年
        hist = {}
        for y in (1, 2, 3):
            hist[f"{y}y"] = _hist_holdout(code, y, idx, dyn)
        # 风控
        risk = {}
        if batch_check_one:
            rc = batch_check_one(code)
            risk = {"level": rc.get("level"), "score": rc.get("score"),
                    "flags": [f["id"] for f in rc.get("flags", [])]}
        else:
            risk = {"level": o.get("risk_level"), "score": o.get("risk_score"),
                    "flags": o.get("risk_flags", [])}
        # 买入理由（一句话 thesis）
        thesis = (
            f"{o['otype_name']}机会：{o.get('trigger', '')}；"
            f"预期空间 {o.get('upside_est', '-')}%，同类 #{o.get('rank_in_type', '-')}，"
            f"全局 #{o.get('rank_global', '-')}"
            + (f"；多类型共识 {o.get('also_types', [])}" if o.get("also_types") else "")
        )
        out_deck.append({
            "code": code, "name": o.get("name", code), "industry": o.get("industry", ""),
            "otype": o.get("otype"), "otype_name": o.get("otype_name"),
            "score": o.get("score"), "note": o.get("note"),
            "upside_est": o.get("upside_est"), "winrate_est": o.get("winrate_est"),
            "rank_in_type": o.get("rank_in_type"), "rank_global": o.get("rank_global"),
            "n_types_hit": o.get("n_types_hit"), "also_types": o.get("also_types", []),
            "factors": o.get("factors", {}),
            "hist": hist,
            "risk": risk,
            "thesis": thesis,
        })

    result = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "n": len(out_deck),
        "deck": out_deck,
        "note": "买入/放弃请写入 deck_decisions.json（Deck 界面自动完成）",
    }
    DECK_OUT.parent.mkdir(parents=True, exist_ok=True)
    DECK_OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def decisions_summary() -> dict:
    """已审批记录摘要"""
    if not DECISIONS.exists():
        return {"n": 0, "items": []}
    lines = [json.loads(l) for l in DECISIONS.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {"n": len(lines), "items": lines[-20:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="重新扫描机会引擎")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        ds = decisions_summary()
        print(f"Deck 已审批 {ds['n']} 条")
        if DECK_OUT.exists():
            d = json.loads(DECK_OUT.read_text(encoding="utf-8"))
            print(f"当前 Deck: {d.get('date')} {d.get('n')} 只候选")
        return

    if args.force:
        import subprocess
        r = subprocess.run([sys.executable, str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"],
                           capture_output=True, text=True, timeout=900)
        print(f"重新扫描: exit={r.returncode}")

    r = build_deck()
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    print(f"=== Deck 构建完成 {r['date']} ===")
    print(f"候选 {r['n']} 只 → {DECK_OUT}")
    for o in r["deck"]:
        h1 = o["hist"].get("1y", {})
        print(f"  {o['code']} {o['name']:8s} [{o['otype_name']}] score={o['score']} "
              f"风控={o['risk'].get('level', '-')} "
              f"1y: 均值{h1.get('avg', '-')}% 胜率{h1.get('win', '-')} n={h1.get('n', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
