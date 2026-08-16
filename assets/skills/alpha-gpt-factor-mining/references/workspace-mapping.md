# 工作区落地映射表（AlphaGPT → 本工作区）

> 目的：把 AlphaGPT 的每个环节映射到 `量化代码库思路库（工作区）` 已有的数据、脚本和验证流程上，做到"方法移植、工具复用"。

## 1. 数据层映射

| AlphaGPT 需要 | 工作区现状 | 接入方式 |
|---|---|---|
| 特征张量（股票×时间×特征） | `因子池/scripts/quality_gate_check.py` 加载 bars.db（qfq、is_st=0、2019-01-01 起）+ hist_mv.db（月度市值）+ stock_basic.db（行业） | 复用同一加载段；特征按 `references/formula-language.md` 第 2.3 节映射 |
| 目标收益（label） | 仓库用 open-to-open；工作区惯例是 fwd20/fwd60（未来 20/60 日收益） | 用次日开盘可成交口径构造 `fwd_ret`；禁未来函数 |
| 涨跌停/停牌处理 | bars.is_st 已过滤；涨停/一字板判断逻辑在 quality_gate_check.py 第 2 节 | 复用它（ret≥0.095 记 is_limup，high==low 记一字板） |

## 2. 特征词表映射（6-10 个即可启动）

| AlphaGPT token | 工作区因子/字段 | 说明 |
|---|---|---|
| RET | ret = close/prev_close - 1 | 现成 |
| RET5 | ret.rolling(5).sum() | 现成 |
| VOL_CHG | volume/MA20(volume) - 1 | 现成 |
| V_RET | ret × (vol_chg + 1) | 现成 |
| TREND | close/MA60(close) - 1 | 现成 |
| TURN（替代 LIQ_SCORE） | 20 日均换手率（bars.turn） | A 股流动性代理 |
| LIMUP_EX（可选） | limup_ex_5（非一字板涨停反转） | 工作区最强因子族，作特征输入 |
| VOLAT（可选） | 20 日收益波动率 | 低波族 |
| AMIHUD（可选） | |ret|/amount | 已入池因子 |
| MV（约束用） | log(hist_mv.circ_mv) | 不在公式里，用于域约束 |

所有特征 **robust 归一化**（median/MAD，clip ±5）。

## 3. 算子词表（直接复用，无需改动）

`ADD SUB MUL DIV NEG ABS SIGN GATE JUMP DECAY DELAY1 MAX3`（基础 12）+ `DELTA5 MA20 STD20 TS_RANK20`（A 股 4）。实现参考 `_AlphaGPT_ref/model_core/ops.py`，把 torch 换成 numpy/pandas 向量化（股票×时间二维，按股票 groupby 处理滞后/滚动）。

## 4. 打分环节映射（最关键）

| 阶段 | AlphaGPT 内置（meme/A股版） | 工作区替代（推荐） |
|---|---|---|
| 初筛 reward | Sortino / 累计收益-回撤罚 | **截面 RankIC + ICIR@20**（逐日截面 Spearman，基准 ICIR>0.3） |
| 组合闸门 | 单一标的时序仓位 | **组合层实测**：季度调仓 Top10% 市值中性，净超额 >+0.3pp |
| 深度验证 | 无（OOS 报告） | **五步验证链**：暴露审计 → Brinson 归因 → 双中性 → 失效期 → 容量 |
| 最终交付 | best_formula.json | **验证卡**（模板见验证链 V1 第 3 节）+ 适用域 + 入池登记 |

> 结论：把 `backtest()` 的 reward 函数整体替换为"ICIR/组合超额"即可让 AlphaGPT 挖出**直接入池口径**的因子。工作区已验证"截面 ICIR ≠ 组合能力"（8% 转化率），所以 skill 的 Phase 3 初筛用 ICIR 海选、Phase 4 必须走组合层实测，两段都不可省。

## 5. 验证链工具清单（Phase 4 调用）

| 工具 | 用途 | 对应步骤 |
|---|---|---|
| `quality_gate_check.py` | verify_factor 一键验证卡（组合层/双中性/分层） | 2/3/5 |
| `quality_exposure_check.py` | 暴露审计 | 3 |
| `quality_brinson_industry.py` | Brinson 归因 | 4 |
| `quality_ind_mv_neutral.py` | 行业×市值双中性 | 5 |
| `quality_decay_check.py` | 失效监控 | 补充 A |
| `quality_ind2_map.py` | 二级行业地图 | 补充 |

## 6. 去重与复合约束（Phase 5）

- 池内锚点因子：reversal20、sentiment、turnover、lowvol、amihud、limup_ex_5、turn_std20、涨停质量评分。
- 新公式与锚点 Spearman：
  - >0.5 → 二选一（保留实证更强的）；
  - >0.8 → 必弃；
  - <0.2 → 才值得做复合候选。
- 复合前必查相关矩阵（工作区教训：rev_60×质量相关 0.364 → 复合稀释 -1.9pp）。

## 7. 执行建议

1. 优先 **LLM 引擎**（Phase 2-B）：零训练成本，直接按 SKILL.md 第七节提示模板批量采样 20-50 条。
2. 想忠实复刻 RL 引擎时：装 torch，把 `times.py` 的 `DeepQuantMiner` 改造为读工作区 bars.db + 替换 reward 为 ICIR，BATCH 1024 / MAX_SEQ 8 / ITER 400，CPU 也能跑（模型很小：d_model 64、2 层、4 头）。
3. 每轮交付一张验证卡 + 一个入池决策，绝不跳过 Phase 4。
