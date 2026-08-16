# -*- coding: utf-8 -*-
"""DeepSeek HARNESS Quant · 主入口（CLI）

用法：
    python main.py update      数据更新（盘后增量）
    python main.py validate    P0.5 因子可行性验证（入场券，强制先做）
    python main.py screen      周度排名 + 三池更新
    python main.py backtest    回测 + Ablation 对照
    python main.py report      生成 Web 看板 / 操作指令表
"""
import sys
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((BASE_DIR / "config" / "params.yaml").read_text(encoding="utf-8"))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    print(f"[DeepSeek HARNESS Quant v2.5] 命令: {cmd}")
    print(f"配置已加载: 数据源={list(CONFIG['data']['sources'].values())}")

    if cmd in ("update", "validate", "screen", "backtest", "report"):
        print(f">>> '{cmd}' 模块开发中——下一阶段实现（当前为骨架）")
        # 各命令的实现在对应包中逐步落地：
        #   update    → data/
        #   validate  → validation/   （P0.5 因子验证，第一优先级）
        #   screen    → strategy/ranking.py + pool_manager.py
        #   backtest  → backtest/bt_engine.py
        #   report    → report/
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
