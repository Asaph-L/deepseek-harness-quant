# -*- coding: utf-8 -*-
"""report/daily_signal.py — 每日信号（v3 口径）生成器（★2026-08-22 重建）

原脚本缺失（开源仓库未提供），dev_auto 第 3 步 / refresh_after_data 依赖它生成
output/daily_signal.json（固定名，供 /api/signal、pool_layers、position_monitor）+ 时间戳版
output/daily_signal_YYYYMMDD_HHMMSS.json（供 health_check 时效链）。

信号 = 择时（timing_system.evaluate）⊕ 持仓（equal_weight_timing.portfolio）：
  - 择时：政策40%+宏观25%+情绪20%+宽度15% → level/score/emoji
  - 持仓：等权 + Regime 仓位 + 硬过滤 → codes/regime_cash_ratio/target_position_pct

用法：python report/daily_signal.py
"""
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OUT = BASE / "output"
DEFAULT_CAPITAL = 200000.0   # 模拟/实盘初始资金（pool_layers 默认同源）


def build() -> dict:
    from factors.policy.timing_system import evaluate as timing_evaluate, write_outputs as write_timing
    from strategy.equal_weight_timing import portfolio as build_portfolio

    timing = timing_evaluate()
    write_timing(timing)
    date = timing.get("date") or datetime.now().strftime("%Y-%m-%d")
    p = build_portfolio(date)

    sig = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": date,
        # ---- 择时 ----
        "level": timing.get("level"),
        "emoji": timing.get("emoji"),
        "score": timing.get("score"),
        "reason": timing.get("reason", ""),
        "dims": timing.get("dims", {}),
        "regime_fit": timing.get("regime_fit", {}),
        "style_state": timing.get("style_state", {}),
        # ---- 持仓 ----
        "regime_cash_ratio": p.get("regime_cash_ratio", 0.0),
        "cash": p.get("regime_cash_ratio", 0.0),
        "n_stocks": p.get("n_stocks", 0),
        "codes": p.get("codes", []),
        "target_position_pct": p.get("target_position_pct"),
        "capital": DEFAULT_CAPITAL,
        "formula": "择时(timing_system v3) ⊕ 持仓(equal_weight_timing 等权+Regime+硬过滤)",
    }
    return sig


def main():
    sig = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "daily_signal.json").write_text(
        json.dumps(sig, ensure_ascii=False, indent=1), encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT / f"daily_signal_{ts}.json").write_text(
        json.dumps(sig, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sig['emoji']} {sig['level']}（{sig['score']}）| {sig['date']} | "
          f"持仓 {sig['n_stocks']} 只 · 现金 {sig['regime_cash_ratio']:.0%}")
    print(f"已存 output/daily_signal.json + output/daily_signal_{ts}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
