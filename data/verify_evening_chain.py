# -*- coding: utf-8 -*-
"""晚间链验证脚本（2026-08-14 建立，供 18:30/19:00 链后执行）

用法：python data/verify_evening_chain.py
（用 deepseek-harness-quant 的 venv python 运行；只读验证，不写任何数据）
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" | {detail}" if detail else ""))


def main():
    logs = BASE / "logs"
    out = BASE / "output"
    pool_out = Path(r"data/factorpool/output")
    now = time.time()

    print("== 1. 数据日（应 = 最新交易日，18:30 链后为今日）==")
    # portal_dash 数据日
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8787/api/live/portal_dash", timeout=30).read().decode())
        check("portal_dash data_date", bool(d.get("data_date")), d.get("data_date"))
        # 语义：链后应"已更新"；盘中显示"上一交易日"属正常（18:30 前）
        _sem = d.get("data_semantic") or ""
        check("数据语义", ("已更新" in _sem) or ("盘" in _sem), _sem)
    except Exception as e:
        check("portal_dash", False, str(e)[:60])

    print("== 2. 关键产出文件新鲜度（<6h 内更新 = 链已跑）==")
    checks = [
        ("机会池", logs / "opp_pool_*.json"),
        ("Pitch 长线", logs / "pitch_v2_*.json"),
        ("科技线", logs / "tech_pitch_*.json"),
        ("三层池", out / "pool_layers_*.json"),
        ("今日信号", out / "daily_signal_*.json"),
        ("择时", out / "timing_system_*.json"),
        ("强因子风险", out / "factor_risk_*.json"),
    ]
    for name, pat in checks:
        fs = sorted(glob.glob(str(pat)), key=os.path.getmtime)
        if not fs:
            check(name, False, "无文件")
            continue
        latest = Path(fs[-1])
        age_h = (now - latest.stat().st_mtime) / 3600
        check(name, age_h < 6, f"{latest.name} {age_h:.1f}h")

    print("== 3. 因子池侧（今晚 19:00 链）==")
    for sub, pat, name in [("daily_scores", "daily_*.csv", "面板"),
                           ("", "factor_manifest_*.json", "manifest")]:
        fs = sorted(glob.glob(str(pool_out / sub / pat)), key=os.path.getmtime)
        if fs:
            age_h = (now - Path(fs[-1]).stat().st_mtime) / 3600
            check(f"因子池 {name}", age_h < 12, f"{Path(fs[-1]).name} {age_h:.1f}h")
        else:
            check(f"因子池 {name}", False, "无文件")

    print("== 4. Pitch 改进规格 v2 落地检查 ==")
    try:
        sys.path.insert(0, str(BASE))
        from factors.opportunities import scan
        # 白名单加权
        w = scan._load_pitch_weights()
        check("白名单加权文件", w is not None, f"{len(w)} 因子" if w else "")
        # pv 定版参数
        check("PV_AMOUNT_MIN_YI", getattr(scan, "PV_AMOUNT_MIN_YI", None) == 0.2, str(getattr(scan, "PV_AMOUNT_MIN_YI", None)))
        # ★2026-08-14 顶级买点标签机制（择时高概率+高稀有度+强买入，极其严格）
        _tbm = getattr(scan, "TOP_BUY_TIMING_MIN", None)
        _tbl = getattr(scan, "TOP_BUY_LEVEL", None)
        check("顶级买点标签机制", _tbm == 75.0 and _tbl == "适合买入", f"阈值 {_tbm} / 档位 {_tbl}")
    except Exception as e:
        check("scan 导入", False, str(e)[:60])

    print("== 5. API 端点全绿 ==")
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8787/api/live/endpoints", timeout=60).read().decode())
        check("endpoints", d.get("ok_count") == d.get("total"), f"{d.get('ok_count')}/{d.get('total')}")
    except Exception as e:
        check("endpoints", False, str(e)[:60])

    print(f"\n=== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ===")
    for name, detail in FAIL:
        print(f"  FAIL: {name} | {detail}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
