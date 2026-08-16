# HKUST Alpha-GPT 论文方法论补充（LLM 引擎参考）

> 来源：HKUST Alpha-GPT（arXiv:2308.00016 / EMNLP 2025 demo）。**注意：这是与 imbue-bit/AlphaGPT 仓库同名但不同的项目**——本篇是"LLM 人机交互 alpha 挖掘"，仓库是"REINFORCE 公式工厂"。本文件只借用前者的 **LLM 生成 + 校验 + 交互** 工程细节，用来强化 SKILL.md 的 Phase 2-B（LLM 引擎）与 Phase 5（人机交互）。
> 完整报告：`_AlphaGPT_ref/散落资料_20260815/alpha_gpt_蒸馏报告.md`（30KB，含全部表格与原文引用）。

## 0. 2.0 论文甄别（arXiv:2402.09746，防误用）

- 2402.09746 **只有 v1**（2024-02-15），是 5 页草稿（页脚 "Draft. Work in progress"），仅描述三层多智能体架构（Alpha Mining / Alpha Modeling / Alpha Analysis layer），**无任何实验/IC/Sharpe/回测数据**。
- "alpha agent + execution agent 双智能体 + interaction module"这组专名**不在论文正文**——来源是已删除的官方 GitHub 仓库（IDEA-Research/Alpha-GPT，404）文档与社区传播。引用时须标注"社区版本，非论文原文"。
- EmergentMind 主题页把 1.0 的 Table 1 IC 表误标为 2.0 实验结果——**业绩数字一律以本文件 §7 的 1.0 数据为准**。
- 2.0 有价值的工程增量（来自仓库文档/社区）：每层 = LLM agent + 工具集 + 记忆（注释 alpha 库/对话历史/use logs/model zoo/知识图谱）+ 预置 SOP（不做复杂规划）；交互反馈分**明确反馈**（评分/选择）与**隐含反馈**（对话上下文推断）；去重去相关用 LSH/ANN/KD-Tree + SHAP/LIME 重要性。

## 1. 三阶段 agentic workflow（交互骨架）

```
Ideation（构思）        Implementation（实现）           Review（评审）
研究员自然语言想法  →   LLM 生成 alpha 表达式     →   回测(IC/Sharpe/收益)
   ↓ 检索知识库增强       ↓ GP 遗传规划增强               ↓ LLM 解释 top alpha
结构化 prompt            Alpha 数据库                   反馈给研究员 → 迭代
```

## 2. LLM 生成的核心工程细节（skill 可直接照搬）

### 2.1 提示模板骨架（论文 Figure 5 还原）

```
System:
You are a quant researcher developing formulaic alphas.
## Instructions
write python expressions with ...（规定算子/操作数/数量/格式）
## Specifications
operators: ...（算子清单+用法）
operands: ...（操作数清单+含义，如 "high_1D": highest intraday price）

User:
## Trading Ideas
{用户想法（已消歧）}
## Examples
{检索到的 few-shot 示例：alpha001 ```expr``` Description: ...}
> Now, please write me at least 10 such expressions.
```

### 2.2 输出格式与解析（必须强制）

每条的固定三件套 + 正则：
```
**名称**
```python
表达式
```
描述段
```
解析正则：`\*\*(.*?)\*\*\n+```python\n(.*?)\n```\n+(.*?)(?=\n\*\*|$)`

### 2.3 校验与重试（论文实证：每轮 10 个 alpha 只有约 4 个正确！）

- **语法校验**：AST 解析器。
- **语义校验**：mock 数据 + 运行时上下文执行，捕获 `log(0)` / `sqrt(-5)` / 单位不兼容（volume+close）等异常。
- **重试机制**：非法表达式 → 构造 retry prompt（含原表达式 + 报错信息）→ 重生成；重试上限 τ。
- 合法 alpha 并入后续 prompt 历史（`update_prompt`），上下文累积。

### 2.4 生成合法性约束（防幻觉）

- 操作数带属性：`price_degree`（价格阶数）+ `is_unitless`（无量纲），做**单位一致性**校验。
- 操作数词典 + 术语表同时给 LLM 和解析器用，两头防幻觉。

## 3. 操作符词典（Table 1，四类，生成时的"算子表"）

| 类别 | 算子 |
|---|---|
| 时序 | shift, ts_corr, ts_cov, ts_decayed_linear, ts_min/max, ts_argmax/min, ts_mean, ts_median, ts_zscore_scale, ts_maxmin_scale, ts_skew, ts_kurt, ts_delta, ts_delta_ratio, ts_ir, ts_ema, ts_percentile, ts_linear_reg, ts_rank, ts_sum, ts_product, ts_std |
| 横截面 | zscore_scale, winsorize_scale, normed_rank, cwise_max, cwise_min |
| 分组 | grouped_demean, grouped_max/min/sum/mean/std, grouped_zscore_scale, grouped_winsorize_scale |
| 逐元素 | relu, neg, abs, log, sign, pow, round, add, minus, cwise_mul, div, greater, less |

> 与本 skill 的逆波兰式词表对照：论文用**前缀式 Python 函数调用**，仓库用**逆波兰 token 序列**——两者是同一棵树的不同编码。落地时二选一，建议 LLM 引擎用前缀式（LLM 更擅长），求值统一转成你的 StackVM。

## 4. 论文原文 alpha 实例（可直接当 few-shot 示例）

| 交易想法 | 表达式 | 逻辑 |
|---|---|---|
| 资金流 | `div(cwise_mul(cwise_max(minus(close,shift(close,1)),0),amount,cwise_mul(close,volume)))` | 上涨日资金流入 / 成交额 |
| 量价相关 | `zscore_scale(ts_corr(close,volume,20))` | 20 日量价相关，zscore 突出极端 |
| 上影线 | `div(cwise_max(minus(high,open),minus(high,close)),minus(high,low))` | 上影线占比大 → 抛压 |
| 动量转换 | `ts_delta(ts_rank(div(ts_delta(close,1),close),10),1)` | 相对价格变化排名的一阶差分 |
| 平滑趋势 | `zscore(ts_delta(ts_ema(ts_rank(close,10),10,0.5),1))` | 平滑价格趋势幅度的标准化变化 |
| 金叉/均值回复 | `-(close-open)/((high-low)+0.001)` | 相对日内波动的涨幅取负 |

## 5. GP 增强与停止准则（可选深度挖掘）

- **过拟合** → 迭代中样本外评估 + 拟合正则化（降复杂度）+ 早停。
- **多样性丧失** → 迭代中加约束（距离/相关性惩罚）。
- **非法表达式** → 规则库（数学规则、单位一致性、金融领域规则）。
- **fitness = IC**，记录 train_ic/test_ic 成对输出。
- **停止点**：out-of-sample IC 前 5 轮快升、约 15 轮收敛后停止（论文实证）。

## 6. 人机交互反馈的三种注入方式（Phase 5 参考）

1. 自然语言方向（review 阶段新意见）；
2. 表达式/搜索配置修改（GP 超参 JSON 或要求重写）；
3. 评分/筛选（勾选保留/剔除，进入会话历史）。

## 7. 论文量化结果（校准预期，别指望一轮出奇迹）

- 单轮 LLM 生成：10 个约 4 个正确 → **校验+重试不可省**。
- IC 演进：Seed 0.58% → 10 轮 search enhancement 1.23% → 1 轮交互+10 轮增强 **2.23%**（约 4 倍）。
- 与人类研究员对比（GPT-4 打分）：Alpha-GPT 8.16 vs 人类 6.81（86.6% 胜率）。
- WorldQuant IQC 2024：全球 top-10（41000+ 队）。
