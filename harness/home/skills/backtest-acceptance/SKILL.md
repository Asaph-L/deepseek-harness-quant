---
name: backtest-acceptance
description: 回测验收标准与引擎调用指南（deepseek-harness-quant 定稿口径）。T+1/成本/数据边界/口径披露清单 + 如何编写回测程序、调用回测引擎（bt_runner）、把结果放入回测归档（output/backtest_archive/）。当用户要求"回测/验证策略/上传回测/跑回测/把结果放进回测结果"时使用。
whenToUse: 用户提到回测、策略验证、回测验收、把策略跑一遍、结果放回测结果里、新增策略回测。
metadata:
  version: v1.0
  created: 2026-08-16
  scope: deepseek-harness-quant（主系统）
---

# 回测验收标准与引擎调用指南（v1.0）

> 用途：①让 AI 有统一标准判断"一个回测结果是否可接受"；②给 AI 完整的引擎调用方法，
> 使其能**编写回测程序 → 调用回测引擎 → 把结果放入回测结果归档**。

---

## 一、回测验收标准（定稿口径，逐项核对）

### 1. 执行口径（硬性）
- [ ] **T+1 对齐**：信号日（t）收盘选股 → 次日（t+1）**开盘价**买入；禁止当日收盘价成交（前视）
- [ ] **不可成交过滤**：一字板涨停（open≈high）买不进必须剔除；停牌日收益按 NaN 处理（不 ffill 硬凑）
- [ ] **涨跌停/退市**：ST 与退市股按策略域规则处理（保留或剔除要写明）

### 2. 成本模型（默认）
- [ ] 佣金 万 2.6（双边）+ 印花税 0.05% 卖出 + 滑点 0.1%
- [ ] 调仓成本按换手摊入（月频全换仓 ≈ 0.26%×2 + 0.05% + 0.2%）
- ⚠️ 披露项：2019-2023 印花税实为 0.1%（分段费率未实现）——报告必须标注

### 3. 数据边界（必须披露）
- [ ] 行情：bars.db qfq 前复权；**2019 年起完整**（换手率 turn 列 2019 前缺失）
- [ ] **2019 年前换手类因子结论一律作废**（覆盖不足，不可回补）
- [ ] 市值：hist_mv.db（2020-06 起）｜ 财报：finance_ts.db（PIT：ann_date 对齐）

### 4. 样本与结论分级
- [ ] 样本 ≥ 60 个交易日；月频/季度调仓需覆盖 ≥ 1 个完整牛熊（建议 2021-2025 默认窗）
- [ ] 结论分级（与项目惯例一致）：
  | 分级 | 条件 |
  |---|---|
  | ✅ 有效 | 年化 > 0 且 夏普 > 0.5，通过 T+1/成本/口径核对 |
  | 🟡 观察 | 年化 > 0 但夏普 ≤ 0.5，或样本不足/窗口特殊 |
  | ❌ 无效 | 年化 ≤ 0，或存在前视/口径违规 |
- [ ] 展示：年化/回撤/夏普/索提诺/卡玛/胜率/期末净值（`bt_report.compute_metrics` 已计算）

### 5. 已知口径近似（报告必须主动披露）
1. 策略引擎部分脚本用收盘价近似（bt_runner 已用 T+1 open + 一字板过滤，是正例）
2. 印花税分段未实现（见 §2）
3. 幸存者偏差：模拟盘覆盖 93.2%，残留 7% 需披露
4. 因子回测（backtest_all_factors）为 top10% 季度调仓、JSON-only 归档

---

## 二、引擎调用指南（AI 编写回测的标准姿势）

### 1. 动态回测（推荐：即时跑 + 自动归档）
```python
# 在 deepseek-harness-quant 仓库根运行
from backtest.bt_runner import run_backtest, list_strategies

print(list_strategies())                       # 策略目录（前端菜单同源）
r = run_backtest(strategy="tech3", topn=5, stocks=300,
                 start="2021-01-01", end="2025-12-31")
print(r["metrics"])                            # 年化/回撤/夏普/索提诺/卡玛/胜率…
```
- 已注册策略：`tech3`（技术三因子·月频）`script1`（大市值三因子·月频）
  `turn_low`（低换手防御·40 交易日）`factor_all`（因子全量·批处理）
- **每次运行自动归档**：历史 `output/backtest_archive/bt_{name}_{ts}.json` + 最新 `latest_{key}.json`
  → 前端 /backtest 页「历史记录」自动列出（无需手动放置）

### 2. 注册新策略（把 AI 写的策略接进菜单/引擎）
在 `backtest/bt_runner.py`：
1. `STRATEGIES` 注册表加一条：`{"name","category","desc","factors","defaults","rebalance"}`
2. 写评分函数（返回 DataFrame：行=交易日，列=股票，**score 越大越好**）：
   - 例：`_turn_low_score(closes)`（20 日均换手 rank 取反）
3. `run_backtest` 的评分分发加一个分支
4. 调仓周期：`rebalance="M"`（月频）/ `"Q"`（季度）/ `40`（交易日数）

### 3. 独立回测脚本（自定义流程时）
```python
import sys; sys.path.insert(0, r"<deepseek-harness-quant 根>")
from data.cache import DailyCache            # 唯一数据读取接口
from backtest.bt_report import archive, compute_metrics

# ... 自己算 returns（pd.Series，index=交易日）与 benchmark ...
metrics = compute_metrics(returns)
archive(returns, params={"name": "我的策略", "strategy": "my_custom"},
        benchmark=bench, name="my_custom", category="策略",
        factors=["因子A", "因子B"], verdict="有效" if metrics["annual_return"] >= 0 else "无效",
        save_html=True)                       # 可选 HTML 报告
```
- 归档文件自动写入 `output/backtest_archive/`（时间戳命名防覆盖）
- 前端 /backtest → 历史记录 → 分类筛选（策略/复刻/因子）+ 有效/无效 + 关键词搜索即可看到

### 4. 手动放置结果（第三方/外部回测）
- 格式：JSON 对齐 `bt_report.archive` payload（含 `metrics/params/dates/nav/benchmark/factors/verdict`）
- 放 `output/backtest_archive/bt_{name}_{YYYYMMDD_HHMMSS}.json`
- 或直接调用 `archive()`（推荐，保证格式正确）

---

## 三、验收动作模板（AI 判断结果时按此输出）

```
【回测验收】策略 <name>（<参数>）
- T+1/成本/一字板：✅/❌ <说明>
- 数据边界：✅/❌ <窗口/字段说明>
- 指标：年化 x% / 回撤 y% / 夏普 z / 样本 n 天
- 结论：✅有效 / 🟡观察 / ❌无效
- 披露：<口径近似清单>
- 归档：output/backtest_archive/<file>（已/未归档）
```

---

## 参考
- 引擎：`deepseek-harness-quant/backtest/bt_runner.py`、`bt_report.py`（归档/指标）
- 因子全量回测：`deepseek-harness-quant/backtest/backtest_all_factors.py`
- 数据读取：`deepseek-harness-quant/data/cache.py`
- 前端：`/backtest`（策略目录 API：`/api/live/backtest_strategies`）
