# -*- coding: utf-8 -*-
"""data/subj_quant.py — 主观×量化融合系统（相对独立模块 · 2026-08-14）

用户需求：「做一个相对独立的系统，菜单单独做出来」——主观判断与量化系统的融合层。
设计来源：因子池《主观量化融合规格v1_A事件标签_B贝叶斯》+《研究_主观与量化平衡》。

定位（分层漏斗，非加权）：
  量化广筛（机器，跑全市场）→ 主观裁决（人，否决/确认/标注）→ 最终 pitch
  红线：禁止"主观 0.5 + 量化 0.5"加权；否决权归主观、入选权归量化。

本模块职责（主系统侧）：
  A 事件标签：研究员输入 {code, tag, confidence} → event_tags 入库（供因子池算 tag_stats 胜率）
  C 约束层：否决清单 veto_list（一票否决）
  B 贝叶斯：前端计算器（P=先验×似然/基准），后端提供标签/校准数据

存储：data/cache/subj_quant.db（独立 SQLite，与主决策链解耦）
"""
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "cache" / "subj_quant.db"

# 标签词表 v1（三类，与因子池规格 2.2 一致）
TAG_WORDS = {
    "业绩类": ["中报预增", "年报预增", "扭亏", "超预期"],
    "治理类": ["实控人增持", "回购", "股权激励", "分拆上市"],
    "事件类": ["中标", "重组获批", "破净", "高送转"],
}


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _conn()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS event_tags (
        date TEXT, code TEXT, name TEXT, tag TEXT, confidence REAL, note TEXT,
        PRIMARY KEY(date, code, tag)
    );
    CREATE TABLE IF NOT EXISTS tag_stats (
        tag TEXT PRIMARY KEY, n INT, winrate REAL, excess REAL, p_value REAL, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS tag_stats_sector (
        tag TEXT, sector TEXT, n INT, winrate REAL, excess REAL, PRIMARY KEY(tag, sector)
    );
    CREATE TABLE IF NOT EXISTS veto_list (
        code TEXT PRIMARY KEY, name TEXT, reason TEXT, date TEXT
    );
    """)
    con.commit()
    con.close()


def add_tag(code, name, tag, confidence, note=""):
    """研究员录入一条主观标签"""
    init_db()
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO event_tags VALUES (?,?,?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d"), code.strip().upper(), name, tag,
         float(confidence), note))
    con.commit()
    con.close()
    return True


def add_veto(code, name, reason):
    """主观否决（一票否决）"""
    init_db()
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO veto_list VALUES (?,?,?,?)",
        (code.strip().upper(), name, reason, datetime.now().strftime("%Y-%m-%d")))
    con.commit()
    con.close()
    return True


def remove_veto(code):
    init_db()
    con = _conn()
    con.execute("DELETE FROM veto_list WHERE code=?", (code.strip().upper(),))
    con.commit()
    con.close()
    return True


def get_state():
    """返回融合系统的完整状态（标签 + 胜率徽章 + 否决清单 + 词表）"""
    init_db()
    con = _conn()
    tags = [dict(r) for r in con.execute(
        "SELECT * FROM event_tags ORDER BY date DESC, code").fetchall()]
    stats = {r["tag"]: dict(r) for r in con.execute(
        "SELECT * FROM tag_stats").fetchall()}
    sector_stats = {}
    for r in con.execute("SELECT * FROM tag_stats_sector").fetchall():
        d = dict(r)
        sector_stats.setdefault(d["tag"], []).append(d)
    vetos = [dict(r) for r in con.execute(
        "SELECT * FROM veto_list ORDER BY date DESC").fetchall()]
    con.close()

    # 给每条标签挂胜率徽章（无统计则"待积累"）
    for t in tags:
        st = stats.get(t["tag"])
        if st and st.get("n", 0) >= 30:
            t["badge"] = {"n": st["n"], "winrate": round(st["winrate"] * 100, 1),
                          "excess": round(st["excess"] * 100, 1), "ok": True}
        else:
            t["badge"] = {"ok": False}

    return {
        "ok": True,
        "tags": tags,
        "tag_stats": list(stats.values()),
        "tag_stats_sector": sector_stats,
        "vetos": vetos,
        "tag_words": TAG_WORDS,
        "n_tags": len(tags),
        "n_vetos": len(vetos),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    st = get_state()
    print(f"主观量化融合系统状态: 标签 {st['n_tags']} 条 / 否决 {st['n_vetos']} 条 / 胜率统计 {len(st['tag_stats'])} 个标签")
