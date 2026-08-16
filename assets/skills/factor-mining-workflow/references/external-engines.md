# 外部开源引擎接入参考（调研结论 2026-08-16）

> 完整调研报告：`量化代码库思路库/调研_开源AI自动挖因子引擎_2023-2026_20260816.md`（506 行，20+ 项目逐项核实）
> 数据可信度：星数/活跃度/许可证为 2026-08-05 GitHub API 实测；查不到的一律标注，未编造。
> ★2026-08-15 实证更新：Alpha158 全量对表+验证链终审——**外部因子库不宜直接入池，正确用法=校准基线**（详见 `_trial_alphagpt/Alpha158接入终审报告_20260815.md`）

## 〇、Alpha158 实测教训（必须先读）

1. **去重判定必须用 |corr| 最大值**：负相关（反向同构）同样算同构——VOL_D 与 sentiment -0.984 实测，max(corr) 会漏判。
2. **"外部因子库 ≠ 增量"**：Alpha158 69 因子 50 个 |ICIR|>0.3、21 个 >0.5，但去重后仅 2 幸存、组合层 T+1 全负（-15.8/-20.4pp）。
3. **校准基线是外部引擎的正确用法**：不直接入池，用外部引擎验证我们验证链口径正确性（两套引擎结论一致 = 互校通过），再定自研方向。

## 一、路线演进（3 次切换）

| 时期 | 主线 | 代表 |
|---|---|---|
| 2023 | RL 公式化因子（表达式树采样+IC奖励） | AlphaGen、DeepAlphaGen |
| 2024 | LLM 智能体（生成→回测→反思循环） | AlphaAgent、RD-Agent |
| 2025-26 | LLM+RL 筛选 + 评估基建 | Alpha-R1、AlphaBench、AlphaEval |

**对我们的启示**：生成器已有（AlphaGPT skill），增量在 **筛选器**（Alpha-R1 范式）与 **评估口径对照**（AlphaEval 范式），不须再造生成器。

## 二、Top 5 接入价值（按对我们工作区）

| 排名 | 项目 | 分 | 处置 | 行动 |
|---|---|---|---|---|
| 1 | **Microsoft Qlib + RD-Agent** | 5 | 可直接跑（MIT） | 隔离 venv（qlib 要求 numpy<1.24/pandas<2.x，与 pandas 3.0 冲突）；Alpha158/360 复算 IC/ICIR 与验证链互校 |
| 2 | **ICT-FinD-Lab/alphagen** | 5 | 方法借鉴+代码移植（无 license，研究用） | **与我们公式语言+验证链最同构**；移植"组合层协同奖励"（set-level IC 天然降相关） |
| 3 | imbue-bit/AlphaGPT | 4 | 可直接跑（Apache-2.0） | 已通过 skill 落地，维持 |
| 4 | RndmVariableQ/AlphaAgent | 4 | 方法借鉴（无 license） | 吸收"探索正则化/对抗 alpha decay"→ 写入去重闸门与证伪知识 |
| 5 | FinStep-AI/Alpha-R1 | 4 | 方法借鉴（无 license） | R1 式推理筛选补我们"筛选器"短板 |

## 三、与我们验证链的对齐点（4B 节）

| 验证链环节 | 外部可借鉴 | 行动 |
|---|---|---|
| ICIR 初筛 | AlphaEval 快速过滤 | 过滤阈值作对照参数 |
| 去重闸门 | AlphaAgent 探索正则化 | "惩罚与已知因子高相关候选"= 去重闸门 LLM 版 |
| 组合层 T+1 | alphagen 组合层协同奖励 | 从"单因子合格"升级为"集合互补" |
| 分年度 holdout | RD-Agent 因子-模型联合优化 | holdout 从因子层升到因子+模型层 |
| 正交性 | AlphaForge/FactorMoE | 动态组合工程参考 |
| 容量 | （外部普遍缺失） | **我们的差异化优势，保留自研** |

## 四、三类处置速查

**✅ 可直接跑（license 干净）**：Qlib(MIT) / RD-Agent(MIT) / AlphaGPT(Apache-2.0) / AlphaEval(MIT) / gplearn_cross_factor(MIT) / evolve(MIT) / alpha-mining-system(MIT)

**📖 方法可借鉴（无 license，只读不并入）**：alphagen / AlphaAgent / Alpha-R1 / QuantaAlpha / AlphaForge / alpha-gfn / DeepAlphaGen / AlphaBench / AlphaGPT_Tushare / LLM_QUANT_FACTORY / FactorMAD skill / R&D-Agent-Quant

**⛔ 避开**：AutoAlpha2022（停更高频）/ AlphaNet、Alpha-Generation、Genetic-Alpha、gpquant（2020-23 停更）/ alpha-factory（PyPI 玩具）/ AlphaFactory（商业）/ **AlphaFlow（同名扩散模型论文，无关）**

## 五、红线（必须遵守）

1. **"无 license" = All Rights Reserved**：alphagen/AlphaAgent/Alpha-R1 等只能读代码借鉴，**绝不复制进可分发资产**；生产依赖只选 MIT/Apache/BSD。
2. **Qlib 版本冲突**：必须独立 venv，数据经 bars.db 子集交换，不共环境。
3. **A 股适配坑**：外部代码默认美股（道指/Yahoo/CSV），停牌/涨跌停/复权处理普遍缺失——接 bars.db 必须补这三件套（我们的验证链已内建）。

## 六、三段式路线（长期目标）

```
生成器（AlphaGPT/LLM skill）→ 筛选器（Alpha-R1 式 RL/评分）→ 验证链（现有验收流水线）
```

短期行动：隔离 venv 跑通 Qlib + Alpha158 复算 IC/ICIR 对表 + WorldQuant 101 翻译进公式语言冒烟测试。
