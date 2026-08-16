# -*- coding: utf-8 -*-
"""factors/opportunities/registry.py — 机会类型注册表（v1.0 · 2026-08-08）

设计重构核心：择时抓取市场一切个股机会（反转/低估值/突破/价值重估/事件/质量折价），
每类机会 = 一组因子 + 触发规则 + 证据链。类型注册制，可扩展。

与因子池（factors/pool/registry.py）的关系：
- 因子池：管理因子本身的生命周期（candidate→active→retired）
- 机会注册表：把 active 因子组装成"机会类型"（什么信号组合 = 一类机会）

用法：
  from factors.opportunities.registry import OPPORTUNITY_TYPES, get_trigger_fns
  for otype, spec in OPPORTUNITY_TYPES.items():
      print(otype, spec["name"], spec["evidence"])
"""
import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------
# 机会类型定义（第一版 6 类）
#   factors:   因子键列表（与 scan.py 计算的面板列名一致）
#   trigger:   触发条件（lambda 函数，输入因子行 Series，输出 True/False）
#   evidence:  证据链（CS 编号/研究报告）
# ---------------------------------------------------------------
OPPORTUNITY_TYPES = {
    "reversal": {
        "name": "反转",
        "desc": "超跌反弹 / 均值回归：A股动量全区间反向，超跌后反弹是统计上最稳定的alpha",
        "factors": ["mom120", "drawdown_60d", "rsi14", "vol_ratio", "roe", "non_st"],
        "trigger_desc": "60日回撤>25% 且 20日动量转正 且 量比≥1.2 且 ROE>0 且 非ST",
        "evidence": "CS-01 动量RankIC -0.070；小盘池反转（华福）；低波+0.099(CS-04)；★17年回测实证v2（2011-2026，1840 样本）：6月胜率仅 39% / 均收益 -1.8%（负期望）→ 门槛已提高至 68（宁缺毋滥）",
        "weight": {"mom120": 0.3, "drawdown_60d": 0.3, "rsi14": 0.2, "vol_ratio": 0.2},
    },
    "value": {
        "name": "低估值",
        "desc": "价值修复：PB/PE 极端低估 + 基本面不恶化 → 均值回归",
        "factors": ["pb_pct", "pe_pct", "div_yield", "roe", "liability", "non_st"],
        "trigger_desc": "PB<历史20%分位 且 ROE>8%(单季口径2%) 且 负债率<70% 且 非ST",
        "evidence": "价值因子大盘池有效；防守引擎 BP/EP 实证（CS-02）；★17年回测实证v2（2011-2026，ROE口径修正后 2379 样本）：6月胜率 62.5% / 均收益 +11.6% / 盈亏比 2.42（安全垫实证🏆，logs/17年回测验证报告v2_20260810.md）",
        "weight": {"pb_pct": 0.4, "pe_pct": 0.2, "div_yield": 0.2, "roe": 0.2},
    },
    "breakout": {
        "name": "突破",
        "desc": "趋势确认：长期基底后放量突破 = 新趋势启动（欧奈尔基底 / VCP）",
        "factors": ["near_high_250", "vol_ratio", "vol_contract", "ma50_up", "ma200_up"],
        "trigger_desc": "距52周高点<5% 且 突破20日平台 且 量比>1.5 且 MA50/MA200 向上",
        "evidence": "near_high_250 唯一120日转正技术因子(+0.012)；VCP社区实证；★2026-08-09 两段稳健性实测：环境敏感（2011-14 胜率54.6% vs 2019-26 仅39.0%），当前校准用现代段口径，高分突破需谨慎；★17年回测实证v2（2011-2026，6731 样本）：6月胜率 45.5% / 均收益 +5.4%（收益正胜率低，环境敏感）",
        "weight": {"near_high_250": 0.3, "vol_ratio": 0.3, "vol_contract": 0.2, "ma50_up": 0.1, "ma200_up": 0.1},
    },
    "revalue": {
        "name": "价值重估",
        "desc": "盈利拐点/戴维斯双击：业绩超预期 → 估值+盈利双击",
        "factors": ["sq_nyoy", "yoy_accel", "gross_margin_chg", "pe_pct", "inst_surv"],
        "trigger_desc": "单季净利同比>50% 且 连续2期加速 且 毛利率提升 且 PE分位<80%",
        "evidence": "华创CANSLIM 2.0 一致预期增强(21.5%→29.5%)；PEAD CS-06；★17年回测实证v2（2011-2026，7669 样本）：6月胜率 53.6% / 均收益 +8.7%（稳定正期望）",
        "weight": {"sq_nyoy": 0.4, "yoy_accel": 0.3, "gross_margin_chg": 0.15, "pe_pct": 0.15},
    },
    "event": {
        "name": "事件驱动",
        "desc": "政策/行业景气 → 板块重定价（A股政策市）",
        "factors": ["ind_moneyflow", "limit_up_cnt", "sector_break", "roe", "sq_nyoy"],
        "trigger_desc": "行业资金流连续3日净流入 且 板块指数突破 且 个股基本面合格",
        "evidence": "中信建投政策信号=板块超额核心驱动；政策因子研究报告（EPU已入池）；★17年回测实证v2（2011-2026，6893 样本）：6月胜率 51.5% / 均收益 +8.3%（稳定正期望）",
        "weight": {"ind_moneyflow": 0.4, "limit_up_cnt": 0.2, "sector_break": 0.2, "roe": 0.1, "sq_nyoy": 0.1},
    },
    "quality_gap": {
        "name": "质量折价",
        "desc": "白马错杀：优质公司短期利空过度反应 → 高质量低价格",
        "factors": ["roe", "drawdown_60d", "div_yield", "liability", "cfo_health", "inst_hold_chg"],
        "trigger_desc": "ROE>15% 且 回撤>25% 且 负债率<60% 且 现金流健康",
        "evidence": "质量因子+0.048(CS-02)；Oversold Quality 策略实证（个股优选层研究荟萃）；★17年回测实证v2（2011-2026，ROE口径修正后 520 样本）：6月胜率 70.4% / 均收益 +14.6%（7 类最强🏆，logs/17年回测验证报告v2_20260810.md）",
        "weight": {"roe": 0.3, "drawdown_60d": 0.25, "div_yield": 0.15, "liability": 0.15, "cfo_health": 0.15},
    },
    "pv_consensus": {
        "name": "量价共识",
        "desc": "量价多因子强共识：因子池五强（换手中位接近/情绪/低换手/反转/低波）中 ≥4 个 rank 前 20% → 低换手+低波+情绪稳定的冷门优质股（财务因子平庸但量价极强）",
        "factors": ["turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol"],
        "trigger_desc": "因子池五强 rank 前20% 命中 ≥4（外部信号源 daily_scores）且 非ST",
        "evidence": "B-6 总指导发现（2026-08-09）：量价五强命中股与机会池交集为 0 → 第 7 类补量价驱动覆盖；五强因子 ICIR：turn_mid_prox 0.868 / sentiment 0.566 / turnover 0.514 / reversal20 0.484 / lowvol 0.472；★17年回测实证v2（2011-2026，8832 样本）：6月胜率 53.3% / 均收益 +4.3%（第 7 类正期望实证，logs/17年回测验证报告v2_20260810.md）",
        "weight": {"turn_mid_prox": 0.3, "sentiment": 0.2, "turnover": 0.2, "reversal20": 0.15, "lowvol": 0.15},
    },
}

# 机会类型顺序（扫描输出/看板显示用）
ORDER = ["reversal", "value", "breakout", "revalue", "event", "quality_gap", "pv_consensus"]

# 评分门槛（大池子统一标尺，见 score.py）
SCORE_THRESHOLDS = {
    "pool_min": 50,       # 进大池子的最低机会分
    "pitch_global": 70,   # Pitch 全局门槛（跨类 Top）
    "pitch_same_type": 80,  # 同类内分位门槛（同类 Top 20% 约等于 ≥80 分位）
}


def get_trigger_fns():
    """返回 {otype: (描述, lambda 触发函数)}；触发函数在 scan.py 中实现（依赖面板列）"""
    return {ot: spec["trigger_desc"] for ot, spec in OPPORTUNITY_TYPES.items()}


def summary() -> str:
    lines = ["## 机会类型注册表（v1.0 · 6 类）", ""]
    for ot in ORDER:
        s = OPPORTUNITY_TYPES[ot]
        lines.append(f"### {ot} {s['name']}")
        lines.append(f"- 逻辑：{s['desc']}")
        lines.append(f"- 触发：{s['trigger_desc']}")
        lines.append(f"- 证据：{s['evidence']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
