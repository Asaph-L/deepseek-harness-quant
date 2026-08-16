---
name: factor-mining-workflow
description: 完整因子挖掘工作流（融合版）。AlphaGPT 公式生成引擎（LLM/RL 海选）+ 因子池 factor-pool-methodology 验收体系（验收v1/9步流水线/证伪知识）的端到端流水线：定域→建词表→生成→校验→ICIR初筛→去重闸门→组合层T+1→分年度holdout→全曲线→正交性→容量→归档→人机迭代。当用户要求"挖新因子/完整跑一轮因子挖掘/验证候选因子/做因子研究"时使用。
whenToUse: 用户要求完整因子挖掘流程、挖新因子、把候选因子推进到入池决策、或需要 AlphaGPT 生成+团队验收的组合流程。
metadata:
  merged_from:
    - alpha-gpt-factor-mining（生成/海选段）
    - factor-pool-methodology（验证/裁决段，桌面/技能/）
  trial_basis: 2026-08-15 半导体赛道实测（_trial_alphagpt/）
---

# 完整因子挖掘工作流（融合版 v1.0）

> 融合两个 skill：**AlphaGPT 生成引擎**（公式语言/LLM 海选/奖励迭代）+ **因子池验收体系**（验收 v1/9 步流水线/17 项证伪）。
> 分工一句话：**AlphaGPT 负责"大量产生候选"，团队方法论负责"严格裁决谁活下来"**。
> 实测校准：2026-08-15 半导体赛道跑通全链（20 候选 → 19 合法 → 4 过 ICIR → 1 过去重 → 0 入池）。

## 零、开工前（每次必做，约 10 分钟）

1. 加载本 skill（融合版）＋ 团队权威手册 `因子池/docs/回测引擎与方法论核心资产手册_v1_20260815.md`（冲突以手册为准）
2. 查证伪知识库 `因子池/output/combo_reports/反人性因子证伪知识库_20260815.md`——**避免重复已证伪赛道**（17 项）
3. 确认数据边界：**turn 只用 2019+**；hist_mv 2020 起；bars.db 无 high_limit（用 pct_chg 近似）
4. 确认解释器：`DSHQuant（历史目录名）\.venv\Scripts\python.exe`（pandas 3.0.5）
5. 定域三问（问用户或自答）：目标域（全市场/中小盘 Q1-Q3/行业）｜持有期（20/60/120）｜方向约束与禁区

## 一、工作流全景（10 阶段）

```
AlphaGPT 段（生成·海选）                因子池段（验证·裁决）
─────────────────────────              ─────────────────────────
P1 建词表(特征+算子)                    P6 组合层 T+1（验收v1）★裁决
P2 生成候选(LLM/RL 引擎)                P7 分年度 + holdout 2025-26
P3 数据有效性审计(★最先做)               P8 全曲线 w 扫描（平台区）
P4 ICIR 初筛 + 去重闸门                  P9 正交性 + 容量/可交易性
P5 (衔接) 候选表提交验证链               P10 决策归档 + 人机迭代
```

**核心铁律（两方共识）**：截面 ICIR 只是门票，组合层 T+1 才是裁决（8% 转化率）；一轮 0 通过是常态，全灭也是有效输出。

## 二、AlphaGPT 段（P1-P5）

### P1 建词表（把赛道翻译成公式语言）
- 特征：工作区已有因子工程映射 6-16 个。**符号化 token**（直接用特征名，禁止数字索引——特征数一变全错位，实测 5/19 误判）。
- 特征获取：`core.panel_v1._factor_single(panel, None, None, name)`（**不是** FACTOR_REGISTRY.compute——126 因子 compute_fn 全为 None）。
- 面板：`research_engine.load_universe()`（parquet 缓存 0.4s；列 open/high/low/close/volume/amount/turn/pct_chg/is_st；无 preclose，收益用 pct_chg/100）。
- 行业域：stock_basic 只有证监会一级（84 类，如 C39 含消费电子），无申万二级——**行业域是代理域，报告必须标注口径**。
- 算子：ADD SUB MUL DIV NEG ABS SIGN GATE JUMP DECAY DELAY1 MAX3 DELTA5 MA20 STD20 TS_RANK20（16 个，符号化）。
- 特征通道：原始值 + rank 双通道；时序算子（MA/STD/DECAY）用原始值通道（rank 后窗口方差退化）。
- pandas 3.x：`df.shift(d)`、`df.rolling(w, min_periods=1)`（`rolling(axis=)` 已移除）。

### P2 生成候选（三种引擎任选）
- **A. LLM 引擎**（推荐，零训练成本）：提示模板见 `references/llm-prompt-template.md`；每轮 20-50 条，**统一逆波兰式**（可读前缀式仅展示，禁止混用——实测 19 条全判非法）。
- **B. RL 引擎**（忠实复刻 AlphaGPT）：`times.py` DeepQuantMiner 逻辑，reward 换成 P4/P6 打分；模型小 CPU 可训。
- **C. 外部引擎接入**（2026-08 调研，见 `references/external-engines.md`）：Qlib Alpha158/360（MIT，隔离 venv，IC/ICIR 口径互校）、alphagen（组合层协同奖励移植，无 license 只研究）、GP（gplearn+因子池特征做对照基线）。**license 红线：无 license 项目只借鉴不并入可分发资产**。
- 必做校验-重试闭环（论文实证 10 条仅 4 条正确）：栈模拟语法校验 → mock 执行语义校验 → 非法项带报错重生成（τ=3）。
- **execute 三态返回**：合法 / 语法非法 / 实现异常（带异常信息）——禁止 `except: return None` 静默吞异常（会把 pandas bug 误报成"公式非法"，实测 6 条被误杀）。

### P3 数据有效性审计（★因子池段最优先，但必须在初筛前）
- 分年覆盖率核查：`factor.groupby(year).notna().mean()`——**回测范围 ≠ 因子有效范围**。
- NaN 段 = 随机选股噪声 = 结论污染；覆盖不足年份直接剔除。
- 行业域内再做一次分年核查（域内样本可能逐年萎缩）。

### P4 ICIR 初筛 + 去重闸门
- ICIR 向量化：`factor.rank(axis=1)` 后逐日 Pearson（=Spearman）——禁止逐日循环（1848 天超时）。
- 基准 `|ICIR|>0.3`，分年度 8 年无翻转（行业域可放宽提示）。
- **★去重闸门（前置，ICIR 排序前）**：候选 vs 池内锚点（reversal20/sentiment/turnover/lowvol/amihud/turn_std20/turn_mid_prox）逐日 Spearman：
  - >0.5 淘汰（同构，实测 ICIR 榜首相关 0.78-1.00 被同构因子占领）
  - 0.3-0.5 警示 🟡；<0.3 通过
  - 淘汰项反馈生成器（"赛道已覆盖"），下一轮提示词携带
- 输出候选表：公式｜可读表达式｜ICIR@20/60｜与锚点最大相关｜机制。

### P5 衔接：候选表提交验证链
- 选通过项进 P6；明确方向（direction：ICIR 为负则取反用）。
- code 口径统一：`code.str.split('.').str[0].str.zfill(6)`（引擎面板带 .SZ 后缀 vs 验证链纯 6 位，不统一**静默全 NaN**）。
- merge 前统一键类型：`pd.to_datetime`。

## 三、因子池段（P6-P10）

### P6 组合层 T+1（★唯一裁决）
- 引擎：`因子池/core/combo_backtest.py` 的 `combo_backtest(panel, score, name, rebalance_days, top_n, ...)`——验收 v1 全实现（T+1 d_next 机制 / 成本万3+万1.3+印花税+滑点 / qfq 跳变修正 / 单股≤50% / 现金兜底）。
- **score 要求**：`(date, code)` MultiIndex Series，rank 大=好；用 `rp.load_panel()` 的面板。
- **★域内基准**：行业域选股必须自算域内等权基准（同调仓周期），不能只对比全市场等权（超额虚高，实测差 3 倍）。基准实现见 `references/code-skeletons.md`。
- 验收：净超额 >+0.3pp/期（组合层口径）；输出验证卡。

### P7 分年度 + holdout
- 分年度超额/ICIR 全表；holdout 2025-26 保持率 ≥70% 才可入池。
- 红旗：全周期好但 holdout 差 = 前段红利（alpha003 教训；实测 semi_defensive 2026 转负命中）。

### P8 全曲线 w 扫描
- 权重/参数网格 w=0~1 步进：平台区非尖峰才算稳（防单点过拟合）。
- 调仓周期×top_n 网格同验。

### P9 正交性 + 容量
- **★与 turn_low 相关 <0.3**（团队定稿锚点，不是 lowvol 等近似）——混合稀释即否。
- 容量：涨停占比、冲击预算、可交易性审计（成本 0.4%/期 + 滑点）。

### P10 决策归档 + 人机迭代
- 决策矩阵：✅入池（P0/P1+适用域）/ 🟡复合候选（先查相关<0.2）/ ❌淘汰（记录进证伪知识库）。
- 归档：报告 `因子池/output/combo_reports/研究_{主题}_{日期}.md`；留言 `沟通/留言_{发送方}_{主题}_{时间}.md`；闭环留言移历史。
- **★P6 三件套登记（入池后接入主系统，零改主系统代码）**：通过验证链的因子必须登记三件套 → 主系统下次 scan 自动生效：
  1. `output/daily_scores/daily_{date}_{ts}.csv` 加 `{code}_rank` 列（0-1 截面 rank，非空率≥50%）
  2. `output/health/health_{date}.csv` 加一行（icir20/60/120 + status）
  3. `output/factor_manifest_{date}.json` factors 数组加一条（★核心：category 决定信号族自动归类，direction/icir/status）
  - **强因子直通门槛**：ICIR120 ≥ 0.5 且 t ≥ 4 → 自动进 EXT 名单（fundamental_lowfreq 除外）
  - 详细规范见 `_trial_alphagpt/新因子pitch流程与编程规范_20260815.md`（三件套格式/命名纪律/category 映射）
- 人机迭代：反馈（方向反了/太窄/换赛道/换持有期）→ 调整词表/打分 → 回 P2，≥2 轮。
- **期望值校准**：一轮 20 候选 → 校验 ~95% 合法 → ICIR ~40% → 去重剩 1-2 条 → 组合层 0 通过是常态；全灭 = 记录已覆盖赛道，换词表再迭代。

## 四、两方 skill 对照（谁管什么）

| 环节 | AlphaGPT skill | factor-pool-methodology |
|---|---|---|
| 公式语言/生成/校验 | ✅ 核心 | — |
| ICIR 初筛/去重 | ✅ | 截面快筛（门票） |
| 验收 v1/T+1 引擎 | 调用 | ✅ 权威 |
| 数据审计/分年度/全曲线 | — | ✅ |
| 正交性(turn_low)/容量 | 去重闸门(池内锚点) | ✅ 与 turn_low |
| 证伪知识/决策归档 | — | ✅ |
| 人机交互迭代 | ✅ Phase 5 | 9 步流水线 |

## 五、环境与踩坑速查（实测）

| 项 | 值/做法 |
|---|---|
| 解释器 | canslim-quant\.venv\Scripts\python.exe |
| 面板 | research_engine.load_universe()（0.4s 缓存） |
| 特征 | _factor_single(panel, None, None, name) |
| 公式 | 符号化逆波兰；execute 三态 |
| 验证链 | combo_backtest（验收 v1）；import quality_gate_check 有 230s 副作用勿用 |
| 基准 | 域内等权自算（行业域必做） |
| 编码 | 脚本 UTF-8；PowerShell 传中文引号易坏——写文件跑 |
| code | 进验证链前 zfill(6) 去后缀 |

## 六、产出清单（每轮必交付）

1. 候选表（P4 输出 CSV）
2. 验证卡（P6 每因子一张）
3. 研究报告 `output/combo_reports/研究_{主题}_{日期}.md`（现状→数据→结论→处置）
4. 留言汇报 `沟通/`（含产出物完整路径 + 待决策清单）
5. 复现脚本（可重跑，路径写进报告）

## 参考文件

- 本 skill references/：`llm-prompt-template.md`（生成提示模板）、`code-skeletons.md`（符号化 StackVM/ICIR/去重/域内基准代码骨架）、`external-engines.md`（外部开源引擎接入参考：Top5/对齐点/license 红线）、`trial-findings.md`（实测问题 20+ 条）
- 团队资产：`桌面/技能/factor-pool-methodology/`、`因子池/docs/回测引擎与方法论核心资产手册_v1_20260815.md`、`因子池/output/combo_reports/反人性因子证伪知识库_20260815.md`
- 外部调研：`调研_开源AI自动挖因子引擎_2023-2026_20260816.md`（工作区根目录，506 行）
- AlphaGPT 源：`_AlphaGPT_ref/`（model_core/、times.py）、论文蒸馏 `_AlphaGPT_ref/散落资料_20260815/`
