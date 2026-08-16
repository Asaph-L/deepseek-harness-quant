---
name: alpha-gpt-factor-mining
description: 基于 AlphaGPT（github.com/imbue-bit/AlphaGPT）蒸馏的自动因子公式生成方法论。用"特征+算子"公式语言表达因子，StackVM 求值，回测奖励驱动公式生成器（REINFORCE）迭代挖掘，再对接本工作区五步验证链入池。当用户要挖新因子、生成 alpha 公式、让 AI 自动写因子、或落地 AlphaGPT 方法时使用。
whenToUse: 用户要求挖掘/生成/自动编写 alpha 因子公式，或明确提到 AlphaGPT、自动因子生成、公式语言挖因子。
metadata:
  source_repo: https://github.com/imbue-bit/AlphaGPT
  source_license: 见仓库 LICENSE
  local_reference: _AlphaGPT_ref/
---

# AlphaGPT 自动因子挖掘（蒸馏版）

> 蒸馏自 https://github.com/imbue-bit/AlphaGPT（本地参考副本：`_AlphaGPT_ref/`，含 `model_core/`、`times.py` A 股适配版）。本 skill 把该系统的核心闭环——**公式语言表达 → 解释执行 → 回测打分 → 奖励驱动迭代**——蒸馏为可在本工作区执行的挖掘流程，并与《因子入池验证链标准流程_V1》对接。

## 一、核心思想（先读这个）

AlphaGPT 不是"预测模型"，而是**自动写因子的系统**：

```
特征张量(原始行情 → 特征工程)
        │
        ▼
公式生成器(小 Transformer) ──自回归──► 公式 token 序列(逆波兰式)
        ▲                                      │
        │                                      ▼
        │                              StackVM 解释执行 → 因子信号
        │                                      │
        │                                      ▼
        └── 策略梯度(REINFORCE) ◄── 回测评分(reward) ◄── 信号回测
```

- **公式 = token 序列**：token 只有两类——特征（叶子）和算子（内部节点），构成表达式树。
- **生成器用回测分数当奖励训练**：好公式得高分 → 生成器更倾向产出同风格公式；坏公式扣分 → 被抑制。整个训练不需要人工标注，奖励信号来自回测本身。
- **交易层只消费最终信号分数**：生成、验证、执行三层解耦——这是它最值得借鉴的工程设计。

## 二、公式语言规范（本 skill 的"词汇表"）

### 2.1 特征词表（叶子，arity=0）

主流程 6 特征（`model_core/vocab.py`）：

| token | 含义 |
|---|---|
| `RET` | 对数收益（robust 归一化） |
| `LIQ_SCORE` | 流动性/FDV 健康度（本项目为 meme 场景；A 股可替换为换手/成交额相关） |
| `PRESSURE` | 买卖力量不平衡（K线实体强度，tanh 压缩） |
| `FOMO` | 成交量加速度（量能异动） |
| `DEV` | 价格偏离均值（泵偏离度） |
| `LOG_VOL` | 对数成交量 |

A 股适配特征（`times.py`，5 个）：`RET`（当日收益）、`RET5`（5 日收益）、`VOL_CHG`（量/20日均量-1）、`V_RET`（量价结合）、`TREND`（价/60日均线-1）。

> 落地建议：把本工作区已有的因子工程化特征（换手率、涨停标记、波动率、市值等）映射进词表即可换赛道——词表是插件式的，`FEATURE_NAMES` 可替换。

### 2.2 算子词表（内部节点）

主流程 12 算子（`model_core/ops.py`）：

| token | arity | 语义 |
|---|---|---|
| `ADD` / `SUB` / `MUL` / `DIV` | 2 | 四则（DIV 有 epsilon 保护） |
| `NEG` / `ABS` / `SIGN` | 1 | 取负 / 绝对值 / 符号 |
| `GATE` | 3 | 条件门控：condition>0 选 x，否则选 y |
| `JUMP` | 1 | 极端跳变检测：zscore>3 触发（relu） |
| `DECAY` | 1 | 衰减叠加：x + 0.8·lag1 + 0.6·lag2 |
| `DELAY1` | 1 | 滞后 1 期 |
| `MAX3` | 1 | 当前/lag1/lag2 最大值 |

A 股适配算子（`times.py`）：`DELTA5`（5 日差分）、`MA20`（线性衰减均线）、`STD20`（20 日 zscore，捕捉异常波动）、`TS_RANK20`（20 日滚动标准化，近似 Rank）。

### 2.3 语法与求值

- 公式是**逆波兰式 token 序列**，`StackVM`（`model_core/vm.py`）顺序执行：遇特征压栈，遇算子弹 arity 个操作数计算后压回；栈中恰好 1 个值 = 合法公式；任何 NaN/Inf 就地清洗为 0/±1。
- 非法公式（栈溢出/栈不足/常量因子 std<1e-4）直接判负分，保证生成器学不会发垃圾公式。
- 公式长度上限 `MAX_FORMULA_LEN`（主流程 12 / A 股版 8）——**短小精悍的公式更稳，防过拟合**。

### 2.4 生成合法性控制（关键工程点）

训练时用**严格 action masking**（`times.py get_strict_mask`）：维护 `open_slots`（栈剩余坑位），
- 剩余步数不够填坑时必须选特征（叶子）；
- 反之才允许选算子；
- 保证每条生成公式都是合法表达式树，从源头消灭语法垃圾。

## 三、回测奖励设计（reward 即"评判标准"）

这是 AlphaGPT 的灵魂——**怎么打分决定挖出什么因子**。两种版本：

**meme 版**（`model_core/backtest.py`）：
```
signal = sigmoid(因子) ；仓位 = (signal>0.85) & 流动性安全
净PnL = 仓位×收益 - 换手×滑点(基础费率+冲击成本，单边≤5%)
score = 累计净收益 - 2×大回撤次数（单日<-5% 记一次）
活动度<5 天 → 重罚 -10；无持仓 → -2；score 取中位数
```

**A 股版**（`times.py backtest`，用 Sortino 当 reward）：
```
signal = tanh(因子)；仓位 = sign(signal)；换手 = |Δ仓位|
pnl = 仓位×次日开盘到开盘收益 - 换手×COST_RATE(双边万一，保守取万五)
sortino = mean(pnl)/下行std×15.87
惩罚：mu<0 → -2；均换手>0.5 → -1；从不持仓 → -2；样本<10 → -2
reward = clamp(sortino, -3, 5)
```

> 落地建议：本工作区用验证链工具替代内置回测即可无缝切换（见第六节）——reward 换成 `quality_gate_check.py` 的净超额/ICIR 组合分，挖出来的因子直接就是"入池口径"的。

## 四、训练闭环（REINFORCE，`model_core/engine.py` / `times.py`）

每步迭代：
1. **生成**：batch 个公式自回归采样（带 action mask），累计 log-prob。
2. **求值**：StackVM 执行全部公式 → 过滤非法/常量。
3. **打分**：回测每个合法公式得 reward；记录历史最佳（best_score + best_formula）。
4. **更新**：`adv = (reward - mean)/std`；`loss = -Σlog_prob·adv`（REINFORCE 策略梯度）；AdamW 步进。
5. **正则**：可选 LoRD（低秩衰减，Newton-Schulz 迭代，只压注意力矩阵，抑制秩塌缩，提升小模型泛化，见 `lord/experiment.py`）；AdamW weight_decay 1e-5。
6. 训练结束存 `best_formula`（token 序列）+ 训练历史。

**离线复用要点**：如果你不想训练模型，可以直接把"生成"环节换成 LLM 提示采样（见第七节），保留"求值→打分→择优"三段，就是一套 LLM 版 AlphaGPT。

## 五、样本外验证（防自欺）

`times.py final_reality_check` 的纪律：
- 80/20 训练/测试切分，**测试段绝不参与训练 reward**；
- 严格 OOS 报告：年化收益、年化波动、Sharpe（无风险 2%）、最大回撤、Calmar；
- 目标用**开盘到开盘收益**（open-to-open），信号与收益严格对齐，避免收盘价未来函数；
- 涨跌停/停牌近似检查（下一开盘相对昨收超 ±9.5% 视为不可成交）。

## 六、在本工作区的落地流程（核心指令）

当用户要求"挖/生成/自动写因子"时，按此流程执行：

### Phase 0 定域（先问清楚）
- 目标域：全市场 / 中小盘(Q1-Q3) / 特定行业；持有期 20/60/120 日。
- 数据口径：用工作区 bars.db（qfq、is_st=0）+ hist_mv（市值）+ stock_basic（行业），参考 `因子池/scripts/quality_gate_check.py` 的加载方式。
- 明确方向约束与禁区（如涨停事件类须非一字板过滤、禁未来函数因子）。

### Phase 1 建词表（把赛道翻译成公式语言）
- 特征：从工作区已有因子工程映射 6-10 个（如 `RET`、`TURN`、`VOL_CHG`、`LIMUP_EX`、`VOLAT`、`DEVIATION`、`AMIHUD`…），全部 robust 归一化（中位数/MAD，clip ±5，`times.py robust_norm`）。
- 算子：直接复用 12 算子（ADD/SUB/MUL/DIV/NEG/ABS/SIGN/GATE/JUMP/DECAY/DELAY1/MAX3），A 股版可加 DELTA5/MA20/STD20/TS_RANK20。
- 实现一个 ~30 行的 `StackVM`（参考 `_AlphaGPT_ref/model_core/vm.py`，把 torch 换成 numpy/pandas 向量化即可）。

**★本工作区实测真相（2026-08-15 trial 核查，必须遵守）**：
1. **公式 token 必须符号化**（直接用特征名/算子名如 `["reversal20","lowvol","SUB","turn_std20","MUL"]`）——数字索引与词表大小强耦合，特征数一变全部错位（trial 中 5/19 误判实证）。
2. **统一用逆波兰式**（特征压栈、算子弹 arity 个）；"可读前缀式"只用于展示（to_prefix 还原），LLM 生成时禁止混用（trial 中 19 条全判非法的实证）。
3. **解释器**：用 `DSHQuant（历史目录名）\.venv\Scripts\python.exe`（pandas 3.0.5）；系统默认 python 无数据包。
4. **特征获取**：不要用 `FACTOR_REGISTRY[n].compute()`（126 个因子 compute_fn 全为 None）——统一用 `core.panel_v1._factor_single(panel, None, None, name)`；面板用 `research_engine.load_universe()`（parquet 缓存 0.4s，列 = open/high/low/close/volume/amount/turn/pct_chg/is_st，无 preclose，收益用 pct_chg/100）。
5. **pandas 3.x 时序算子**：`df.shift(d)`、`df.rolling(w, min_periods=1).mean()`——`rolling(axis=)` 已移除，会抛 TypeError。
6. **特征通道**：保留原始值 + rank 双通道，时序算子（MA/STD/DECAY）用原始值通道，避免 rank(0-1) 后窗口方差退化。
7. **execute 三态返回**：合法 / 语法非法 / 实现异常（带异常信息）——禁止 `except: return None` 静默吞异常（会把实现 bug 误报成"公式非法"，trial 中 6 条公式被误杀的实证）。
8. **ICIR 向量化**：`factor.rank(axis=1)` 后逐日 Pearson（=Spearman），禁止逐日 Python 循环（1848 天会超时）。
9. **code 口径**：进验证链前 `code.str.split('.').str[0].str.zfill(6)`——引擎面板带 `.SZ` 后缀、验证链纯 6 位，不统一会**静默全 NaN**（n=0，不报错）。
10. **验证链复用注意**：`import quality_gate_check` 有模块级副作用（自动跑 5 因子全链 ~230s）；推荐 `qgc.df = df; qgc.rebal = ...` 注入面板后调用 `verify_factor`。

### Phase 2 生成候选（两种引擎任选）
- **A. RL 引擎**（忠实复刻）：装 torch 后跑 `times.py` 的 `DeepQuantMiner` 逻辑（BATCH 1024、MAX_SEQ 8、TRAIN_ITER 400），reward 换成第六节打分。产出 top 公式 token。依赖：`torch>=2.0` + `numpy`（主流程）、`tushare`（A 股数据，`times.py` 用）、`matplotlib/seaborn`（画图，可选）——清单见 `_AlphaGPT_ref/requirements*.txt`。模型很小（d_model 64、2 层、4 头），CPU 可训。
- **B. LLM 引擎**（推荐，零训练成本）：按第七节提示模板让 LLM 批量产出候选公式（要求：合法逆波兰式/前缀式、短于 8 token、带机制解释），每轮 20-50 条。**必做校验-重试闭环**（论文实证：每轮 10 条约仅 4 条正确）：AST 语法校验 → mock 数据执行语义校验（抓 log(0)/sqrt(-5)/单位不兼容）→ 非法项带报错信息重生成，重试上限 τ=3；合法项并入下一轮 prompt 历史。

### Phase 3 求值与初筛
- StackVM 求值 → 去掉常量因子（std<1e-4）、NaN 过多、覆盖不足的公式。
- 截面 RankIC / ICIR（20 日，逐日截面 Spearman），基准 `|ICIR|>0.3`；分年度 8 年无翻转。
- **★去重闸门（前置，2026-08-15 trial 实证必须）**：ICIR 排序前先算候选与池内锚点因子（reversal20/sentiment/turnover/lowvol/amihud/turn_std20/turn_mid_prox 等）的逐日 Spearman 相关（rank(axis=1) 后逐日 Pearson 向量化）：
  - 与任一锚点 **>0.5 → 淘汰**（同构，无增量）；
  - **0.3-0.5 → 警示**（标 🟡，入组合层前说明差异点）；
  - **<0.3 → 通过**。
  - 实证：ICIR 榜首的 `ABS(ADD(sentiment,sentiment))` 与 sentiment 相关 1.000、`MA20(MUL(SIGN(turnover),amihud))` 与 amihud 0.967——**不先去重，按 ICIR 选出的全是池内因子变形**。
  - 被淘汰的同构项反馈给生成器（"已覆盖，换赛道"），下一轮提示词携带。
- 输出候选表：公式 | 可读表达式 | ICIR@20/60 | 与锚点最大相关 | 机制一句话。

### Phase 4 五步验证链（强制闸门，不可跳过）
对通过初筛的候选逐个调用工作区工具：
1. 截面海选（ICIR>0.3、8 年无翻转、机制可解释）→ `quality_gate_check.py` 或同等脚本；
2. 组合层实测（季度调仓 Top10% 市值中性，2020-2026，验收净超额 >+0.3pp）；
3. 暴露审计（行业分布 / 行业中性化回测 / 市值分层）；
4. 归因交叉验证（Brinson：行业内选股 vs 行业配置）；
5. 双中性复验（行业×市值双中性，确定适用域）。
每因子产出一张《验证卡》（模板见 `因子池/因子入池验证链标准流程_V1.md` 第 3 节）。

### Phase 5 择优与反馈（人机交互闭环）
- 把验证卡呈现给用户：通过 → 入池建议（P0/P1 + 适用域）；🟡 → 复合候选（先查与现有因子相关 <0.2 才值得复合）；❌ → 淘汰并记录。
- 依据用户反馈（"方向反了" / "太窄" / "换行业试试" / "换个持有期"）调整词表/打分口径，回到 Phase 2 迭代，至少 2 轮。
- 复合前必查相关矩阵，与池内因子 Spearman >0.5 二选一（保留实证更强的）。
- **★期望值校准（2026-08-15 trial 实测）**：LLM 引擎一轮 19-20 条候选，经历校验(~95% 合法)→ICIR 门槛(~40%)→去重闸门(仅剩 1-2 条)→组合层验证（**0 条通过是常态**）。不要因为一轮全灭就怀疑流程——这正是 AlphaGPT 的本质（工作区 8% 转化率；trial 复现：唯一进验证链的候选 -1.63pp 未过）。**全灭也是有效输出**：记录"已覆盖赛道"，换词表/换机制主题再迭代。

### Phase 6 交付
- 输出：公式（token + 可读表达式 + 因子代码）、验证卡、适用域、失效监控建议（滚动 12 月 <-5pp 降权）。
- 交付物落在 `因子池/output/` 下，登记入池状态，更新 README 因子清单。

## 七、LLM 版生成提示模板（Phase 2-B 直接用）

```
你是因子挖掘助手。用以下公式语言生成 alpha 候选（逆波兰式）。

特征（叶子）：RET, RET5, VOL_CHG, V_RET, TREND
算子：ADD SUB MUL DIV NEG ABS SIGN DELTA5 MA20 STD20 TS_RANK20

规则：
1. 每条公式 = 逆波兰 token 序列，长度 ≤ 8，必须是合法表达式树（叶子数量 = 内部节点数 + 1）。
2. 解释执行语义：遇特征压栈，遇算子弹操作数。
3. 目标：预测未来 N 日收益的截面排序能力，机制需可解释（如"缩量回调后的反弹动能"）。
4. 避免：常量因子、未来函数、与池内因子（reversal20/sentiment/turnover/lowvol/amihud）高度同构的公式。
5. 输出格式（每条约 3 行）：
   formula: [RET5, SUB, TREND, MUL, STD20]
   可读: STD20(TREND × (RET5 - TREND))   ← 前缀式
   机制: 一句话经济逻辑
   ICIR@20 预测: 0.2-0.5   ← 自评区间，须诚实

现在生成 20 条，主题：[用户主题]
```

## 八、铁律（沿用工作区 + 仓库经验）

1. **截面 ICIR 不能直接交易**（工作区 8% 转化率实证；trial 复现：ICIR0.317 候选组合层 -1.63pp）——组合层实测是唯一真相，验证链不可跳过。
2. **超额 ≠ alpha**：组合超额必须做暴露审计 + Brinson 归因交叉验证。
3. **防未来函数**：目标收益用 open-to-open 或次日开盘可成交口径；涨停/停牌日不可成交要检查。
4. **防常量/垃圾公式**：std<1e-4 直接判负分，从奖励层面压制。
5. **短公式优先**：MAX_FORMULA_LEN 8-12，越长越易过拟合。
6. **严格 OOS**：测试段绝不被训练 reward 污染，报告 Sharpe/回撤/Calmar 全套。
7. **复合前查相关**：与池内因子 Spearman >0.5 二选一，>0.8 必弃。
8. **每个结论给适用域**：没有适用域的因子不可派单。
9. **★去重先于排序**（trial 实证）：ICIR 榜首常被池内因子变形占领（相关 0.78-1.00），必须先过相关闸门再按 ICIR 排序。
10. **★执行器不许静默吞异常**：实现 bug 与"公式非法"必须可区分（三态返回），否则调试即灾难。
11. **★同构项是信息不是垃圾**：被去重淘汰的公式告诉生成器"这个赛道已覆盖"，反哺下一轮提示词。

## 九、参考文件

- 仓库本地副本：`_AlphaGPT_ref/`（`model_core/` 主实现，`times.py` A 股版，`CATREADME.md` 速读）
- 公式语言与算子详细语义：`references/formula-language.md`
- 落地映射表（特征/算子/验证链对接）：`references/workspace-mapping.md`
- HKUST Alpha-GPT 论文方法论（LLM 引擎的校验-重试-停止检查点、提示模板、操作符词典、few-shot 实例）：`references/hkust-paper-methodology.md`
- **★实施核查实录（13 个问题+根因+修复，全链路实测基线）：`references/trial-findings.md`；完整报告在 `_trial_alphagpt/实施核查报告_20260815.md`**
- 论文完整蒸馏报告（`_AlphaGPT_ref/散落资料_20260815/alpha_gpt_蒸馏报告.md`，30KB）与 `Alpha-GPT2.0_蒸馏报告.md`（2.0 甄别：5 页草稿无实验数据，"双智能体"非论文原文；同目录含全文抓取 html/txt/pdf 原件）
- 工作区验证链：`因子池/因子入池验证链标准流程_V1.md`、`因子池/scripts/quality_gate_check.py`
- HKUST 原始论文（人机交互 alpha 挖掘，概念同源但非本仓库）：arXiv:2308.00016（v1）/ arXiv:2402.09746（2.0）
