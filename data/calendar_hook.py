# -*- coding: utf-8 -*-
"""data/calendar_hook.py — 日历窗口因子层（2026-08-11 百轮#65，H16/H17 实证落地）

★实证结论（回测师 17 年长样本，H16/H17）：
  - 春节后 5 交易日 +3.68% / 胜率 88% —— 全年最强日历窗口（CAL 2 月强互证）
  - 国庆前后 +1.4~1.7% → 国庆后 4 交易日 +4 分
  - 两会后 10 交易日 +0.68% / 65% → +3 分（政策定调利好）
  - 经工会后 10 交易日 -1.47% / 44% → -3 分（政策利好兑现）

★落地裁决（总指导 2026-08-11）：日历效应是**市场级**（全市场平均），
  挂在两处：① live_brief 简报「日历窗口」提示条（择时层）② scan 打分统一加减分（个股无差别）。
  窗口登记表按年维护（春节需农历，一年一更；外包/总指导可扩展）。

★2026 窗口（交易日经 bars.db 实测）：
  - spring_post5:   2026-02-24 ~ 2026-03-02   （2/24 春节后首个交易日 +5 交易日）
  - lianghui_post10: 2026-03-16 ~ 2026-03-27   （3/13 人大闭幕 +10 交易日）
  - national_post4:  2026-10-09 ~ 2026-10-14   （预估：国庆后首个交易日 +4；放假安排公布后校正）
  - econwork_post10: 2026-12-15 ~ 2026-12-29   （预估：中央经济工作会议 12 月中旬 +10 交易日）
"""
from datetime import date, datetime

# 窗口登记表：{year: [(窗口ID, 标签, 起, 止, 加分)]}  日期为 datetime.date
WINDOWS = {
    2026: [
        ("spring_post5",   "春节后5交易日", date(2026, 2, 24), date(2026, 3, 2),  +8,
         "+3.68%/88% 全年最强窗口"),
        ("lianghui_post10","两会后10交易日", date(2026, 3, 16), date(2026, 3, 27), +3,
         "+0.68%/65% 政策定调利好"),
        ("national_post4", "国庆后4交易日", date(2026, 10, 9), date(2026, 10, 14), +4,
         "+1.4~1.7% 假日资金回流"),
        ("econwork_post10","经工会后10交易日", date(2026, 12, 15), date(2026, 12, 29), -3,
         "-1.47%/44% 政策利好兑现"),
    ],
}


def get_window(d: str = None) -> dict | None:
    """输入 'YYYY-MM-DD'（默认今天）→ 命中的日历窗口 dict 或 None。
    返回: {window, label, start, end, bonus, evidence, days_left}"""
    if d is None:
        d = datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    for wid, label, s, e, bonus, ev in WINDOWS.get(dt.year, []):
        if s <= dt <= e:
            return {
                "window": wid, "label": label,
                "start": s.isoformat(), "end": e.isoformat(),
                "bonus": bonus, "evidence": ev,
                "days_left": (e - dt).days,
            }
    return None


def upcoming(d: str = None, horizon_days: int = 14) -> list:
    """未来 horizon_days 天内将进入的窗口（提前提醒，供简报预告）"""
    if d is None:
        d = datetime.now().strftime("%Y-%m-%d")
    dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    out = []
    for year, wins in WINDOWS.items():
        if year < dt.year:
            continue
        for wid, label, s, e, bonus, ev in wins:
            if s > dt and (s - dt).days <= horizon_days:
                out.append({"window": wid, "label": label, "start": s.isoformat(),
                            "bonus": bonus, "days_to": (s - dt).days, "evidence": ev})
    return sorted(out, key=lambda x: x["days_to"])


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    w = get_window(d)
    print("当前窗口:", w if w else "无（正常市场）")
    print("未来预告:", upcoming(d))
