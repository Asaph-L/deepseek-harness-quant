# -*- coding: utf-8 -*-
"""factors/risk/strong_factor_table.py — ★强因子白名单参数表（2026-08-14 F1 最终口径）

来源：因子池 F1_强因子判定标准确认_20260814.md v2（基于 _factor_history_2026-08-13.pkl
当前方向重算，3 年 727 截面）——27 名义强因子的 n_days/IC胜率/拥挤代理/t120。

用途：factor_risk.py 强因子判定做最终门槛校验（health CSV 无胜率/n_days 列，
F1 已现场算好，此处固化避免主系统重复计算）。

判定口径（F1 四节）：
  有效 + ICIR120≥0.5 + t≥4 + IC胜率≥60% + 拥挤代理<90 + F3 实证家族代表 + n_days≥250 + 非 lowfreq
"""
# factor -> {n_days, win_rate, crowd_proxy, t120, icir120, f3_family, style_exposed}
# style_exposed: "true"=F4 双中性归零（不进直通）/ "partial"=部分暴露（可直通但标注）/ "false"=真实alpha
STRONG_TABLE = {
    "open_prem_20":       {"n_days": 365, "win_rate": 0.874, "crowd_proxy": 40.3, "t120": 25.0, "icir120": 1.857, "f3_family": "F1 open_prem_20族", "style_exposed": "partial"},
    "gap_ret_20":         {"n_days": 365, "win_rate": 0.879, "crowd_proxy": 42.5, "t120": 25.1, "icir120": 1.788, "f3_family": "F1 open_prem_20族", "style_exposed": "partial"},
    "limup_ex_5":         {"n_days": 365, "win_rate": 0.904, "crowd_proxy": 72.9, "t120": 25.7, "icir120": 1.620, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "limit_up_cnt_5d":    {"n_days": 365, "win_rate": 0.899, "crowd_proxy": 73.4, "t120": 25.1, "icir120": 1.574, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "chip_concentration": {"n_days": 365, "win_rate": 0.844, "crowd_proxy": 86.0, "t120": 16.6, "icir120": 1.562, "f3_family": "F10 chip_concentration族", "style_exposed": "partial"},   # ★2026-08-14 组合层全库验证通过（+7.65pp/0 负年/OOS +5.11pp，与 amihud 同级）→ 独立成族进直通（原 F2 族成员）；拥挤 86 偏热 + F4 部分暴露 0.574 需监控
    "lhb_jg_cnt_20":      {"n_days": 365, "win_rate": 0.945, "crowd_proxy": 60.8, "t120": 27.7, "icir120": 1.516, "f3_family": "F3 lhb_jg_cnt_20族", "style_exposed": "false"},
    "limup_ex_ret_20":    {"n_days": 365, "win_rate": 0.907, "crowd_proxy": 80.3, "t120": 20.0, "icir120": 1.473, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "shebao_chg_pct":     {"n_days": 363, "win_rate": 0.876, "crowd_proxy": 55.1, "t120": 22.5, "icir120": 1.428, "f3_family": "F4 shebao_chg_pct族", "style_exposed": "true"},
    "shebao_chg":         {"n_days": 365, "win_rate": 0.915, "crowd_proxy": 59.2, "t120": 22.6, "icir120": 1.389, "f3_family": "F4 shebao_chg_pct族", "style_exposed": "true"},
    "limit_up_turn":      {"n_days": 364, "win_rate": 0.907, "crowd_proxy": 57.7, "t120": 25.1, "icir120": 1.368, "f3_family": "F2 limup_ex_5族", "style_exposed": "unknown"},
    "alpha003":           {"n_days": 365, "win_rate": 0.751, "crowd_proxy": 47.4, "t120": 9.6,  "icir120": 1.165, "f3_family": "F5 alpha003族", "style_exposed": "partial"},
    "alpha015":           {"n_days": 365, "win_rate": 0.819, "crowd_proxy": 62.2, "t120": 15.5, "icir120": 1.079, "f3_family": "F6 alpha015族", "style_exposed": "partial"},
    "alpha006":           {"n_days": 365, "win_rate": 0.742, "crowd_proxy": 63.0, "t120": 9.8,  "icir120": 1.068, "f3_family": "F5 alpha003族", "style_exposed": "false"},
    "consec_limit_up":    {"n_days": 365, "win_rate": 0.868, "crowd_proxy": 66.6, "t120": 18.8, "icir120": 1.007, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "limit_up_flag":      {"n_days": 365, "win_rate": 0.868, "crowd_proxy": 66.6, "t120": 18.8, "icir120": 1.006, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "alpha050":           {"n_days": 365, "win_rate": 0.795, "crowd_proxy": 51.5, "t120": 14.6, "icir120": 0.983, "f3_family": "F6 alpha015族", "style_exposed": "partial"},
    "o2c_sum_20":         {"n_days": 365, "win_rate": 0.836, "crowd_proxy": 58.6, "t120": 18.2, "icir120": 0.881, "f3_family": "F7 o2c_sum_20族", "style_exposed": "false"},
    "consec_limit_down":  {"n_days": 363, "win_rate": 0.846, "crowd_proxy": 62.5, "t120": 16.3, "icir120": 0.874, "f3_family": "F8 consec_limit_down族", "style_exposed": "false"},
    "limit_down_flag":    {"n_days": 363, "win_rate": 0.846, "crowd_proxy": 62.8, "t120": 16.3, "icir120": 0.872, "f3_family": "F8 consec_limit_down族", "style_exposed": "false"},
    "turn_std20":         {"n_days": 365, "win_rate": 0.718, "crowd_proxy": 66.8, "t120": 9.0,  "icir120": 0.798, "f3_family": "F2 limup_ex_5族", "style_exposed": "partial"},
    "vol_contract":       {"n_days": 365, "win_rate": 0.701, "crowd_proxy": 70.1, "t120": 12.6, "icir120": 0.716, "f3_family": "F2 limup_ex_5族", "style_exposed": "partial"},
    "vol_curvature":      {"n_days": 365, "win_rate": 0.701, "crowd_proxy": 69.6, "t120": 12.7, "icir120": 0.714, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "alpha044":           {"n_days": 365, "win_rate": 0.701, "crowd_proxy": 56.7, "t120": 8.6,  "icir120": 0.677, "f3_family": "F6 alpha015族", "style_exposed": "false"},
    "open_gap":           {"n_days": 365, "win_rate": 0.775, "crowd_proxy": 59.7, "t120": 11.2, "icir120": 0.600, "f3_family": "F9 open_gap族", "style_exposed": "false"},
    "turnover":           {"n_days": 365, "win_rate": 0.663, "crowd_proxy": 64.7, "t120": 5.9,  "icir120": 0.578, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "sentiment":          {"n_days": 365, "win_rate": 0.732, "crowd_proxy": 61.1, "t120": 13.5, "icir120": 0.565, "f3_family": "F2 limup_ex_5族", "style_exposed": "false"},
    "max_ret20":          {"n_days": 365, "win_rate": 0.652, "crowd_proxy": 56.7, "t120": 8.7,  "icir120": 0.559, "f3_family": "F2 limup_ex_5族", "style_exposed": "true"},
}

# 判定门槛（F1 四节最终口径）
MIN_N_DAYS = 250          # 样本量下限（防新因子早产）
MIN_WIN_RATE = 0.60       # IC 胜率
MAX_CROWD_PROXY = 90.0    # 拥挤代理

# ★2026-08-14 直通白名单排除（组合层实证）：截面强 ≠ 组合强
#   lhb_jg_cnt_20：截面 ICIR 1.516 全库前三，但组合层 -5.63pp / 6/7 负年
#   （因子池 accept6_openprem.py 统一框架，中小盘 Top10% 市值中性、季度调仓、含成本）
#   → 移出直通白名单（factor_risk.py EXPRESS_BANNED 消费），保留风险档案展示与事件确认加分。
#   恢复条件：组合层正超额实证（非截面 ICIR）达标后移除本集合。
#   ★2026-08-14 14:05 追加 limup_ex_5（因子池 accept12_limup.py）：全样本 +2.43 全靠 2022 熊市，
#   2024+ 全负、严格 OOS test -6.06——事件因子截面强组合层失效（lhb 同款），
#   与 F4「涨停族 20 日无持续优势」互证。降为事件确认/科技线用途，不独立直通。
EXPRESS_BANNED = {"lhb_jg_cnt_20", "limup_ex_5"}
