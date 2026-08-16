# 实施核查实录（2026-08-15 trial）——问题、根因与反哺

> 这是 skill 在真实工作区（D 盘 bars.db 2019+、823 万行、5776 只）全链路跑通后的核查记录。
> 完整报告：`_trial_alphagpt/实施核查报告_20260815.md` ｜ 本文件为 skill 内嵌速查版。

## 一、环境基线（首次执行必须核对）

| 项 | 值 |
|---|---|
| 解释器 | `DSHQuant（历史目录名）\.venv\Scripts\python.exe`（系统默认 python 无数据包） |
| 版本 | Python 3.13.14 / pandas 3.0.5 / numpy 2.4.6 |
| 面板 | `research_engine.load_universe()`（parquet 缓存 0.4s；列: open/high/low/close/volume/amount/turn/pct_chg/is_st；无 preclose；收益用 pct_chg/100） |
| 特征 | `core.panel_v1._factor_single(panel, None, None, name)`（**不是** FACTOR_REGISTRY.compute——126 因子 compute_fn 全为 None） |
| 验证链 | `quality_gate_check.verify_factor`；import 有副作用（自动跑 5 因子全链 ~230s），用 `qgc.df=df; qgc.rebal=...` 注入 |
| 数据路径 | `D:\劳威派量化数据\cache\{bars,hist_mv,stock_basic}.db` |

## 二、13 个问题的根因速查

### 实现/环境类（8）
1. **ICIR 逐日循环超时** → 向量化：`rank(axis=1)` 后逐日 Pearson。
2. **DataFrame.std().item() 报错** → `float(out.std().to_numpy().mean())`。
3. **默认 python 无包** → 用 canslim-quant .venv。
4. **FACTOR_REGISTRY.compute 全 None** → 用 `_factor_single`。
5. **面板无 preclose** → 用 pct_chg 或 close/prev shift。
6. **pandas 3.0 移除 rolling(axis=)** → `df.rolling(w, min_periods=1)`（默认沿 index）。
7. **merge 类型不匹配** → 键统一 `pd.to_datetime`。
8. **code 口径不一致 → 静默全 NaN** → `code.str.split('.').str[0].str.zfill(6)`（最隐蔽，merge 不报错）。

### 公式语言类（3）
9. **数字索引编码脆弱**（特征数变则全错位）→ **符号化 token**。
10. **to_prefix 不递归** → 栈式还原。
11. **前缀式/逆波兰式混用全判非法** → **统一逆波兰式**，提示模板明示"可读前缀式仅展示用"。

### 方法论类（2，最重要的反哺）
12. **同构因子占领 ICIR 榜首**（相关 0.78-1.00 实证）→ **去重闸门前置**：与锚点 >0.5 淘汰、0.3-0.5 警示、<0.3 通过；同构项反馈生成器。
13. **ICIR 过门槛 ≠ 组合层有效**（0.317 → -1.63pp 实证）→ 全灭是常态；一轮 20 条 0 通过也有效输出。

## 三、修复后的核心实现骨架（符号化逆波兰）

```python
def validate(tokens):          # 栈模拟: 叶子+1, 算子-arity+1, 终栈==1
    stack = 0
    for t in tokens:
        if t in FEATURES: stack += 1
        elif t in OP_ARITY:
            a = OP_ARITY[t]
            if stack < a: return False
            stack = stack - a + 1
        else: return False
    return stack == 1

def execute(tokens):           # 三态: (矩阵,None) / (None,"语法非法") / (None,"实现异常: ...")
    if not validate(tokens): return None, "语法非法"
    stack = []
    try:
        for t in tokens:
            if t in FEATURES: stack.append(feat_mats[t])
            else:
                arity = OP_ARITY[t]
                args = [stack.pop() for _ in range(arity)][::-1]
                res = OPS_IMPL[t](*args).replace([np.inf,-np.inf],[1.0,-1.0]).fillna(0.0)
                stack.append(res)
        out = stack[0]
        if float(out.std().to_numpy().mean()) < 1e-4: return None, "常量因子"
        return out, None
    except Exception as e:
        return None, f"实现异常: {type(e).__name__}: {e}"

def to_prefix(tokens):         # 逆波兰 -> 前缀可读
    assert validate(tokens)
    stack = []
    for t in tokens:
        if t in FEATURES: stack.append(t)
        else:
            arity = OP_ARITY[t]
            args = [stack.pop() for _ in range(arity)][::-1]
            stack.append(f"{t}({','.join(args)})")
    return stack[0]
```

## 四、去重闸门实现（Phase 3 前置）

```python
def corr_with_anchors(cand, anchors):   # 逐日 Spearman 取均值
    r = cand.rank(axis=1)
    out = {}
    for name, am in anchors.items():
        mask = cand.notna() & am.notna()
        rr = r.where(mask); ar = am.rank(axis=1).where(mask)
        rc = rr.sub(rr.mean(axis=1), axis=0); ac = ar.sub(ar.mean(axis=1), axis=0)
        n = mask.sum(axis=1)
        c = (rc*ac).sum(axis=1) / (np.sqrt((rc**2).sum(axis=1))*np.sqrt((ac**2).sum(axis=1)) + 1e-9)
        c = c.where(n >= 30).dropna()
        out[name] = c.mean() if len(c) else np.nan
    return out
# 判定: max(corrs) > 0.5 淘汰 / 0.3-0.5 警示 / <0.3 通过
```

## 五、性能基线（一轮成本）

面板 0.4s + 12 特征 93s + 19 公式求值 22s + ICIR 60s + 去重 30s + 验证链 240s（含 import 副作用）≈ **7 分钟/轮**（排除副作用约 3 分钟）。

## 六、对"为什么"的总结

- **为什么同构因子霸榜**：LLM 在池内因子组合上生成"安全"公式最省力，且截面 rank 特征 + 简单算子组合天然复制锚点信号；没有去重闸门，ICIR 排序必然把池内因子的变形选上来——这不是生成器的错，是**评估体系缺了"增量"维度**。
- **为什么 ICIR 通过仍组合层挂**：ICIR 度量的是截面排序的时序稳定性，组合层额外叠加了调仓频率、成本、市值中性、行业暴露等约束；两者相关性弱是 A 股实证（8% 转化率）。skill 的价值不在"提高通过率"，而在**把海选成本压到最低、把验证链用在刀刃上**。
- **为什么执行器必须三态返回**：公式语言是递归结构，实现 bug 与语法错误表象相同（返回 None）；不区分两者，任何环境升级（如 pandas 3.0）都会让全部候选"离奇非法"且无法定位。

## 七、2026-08-15 补充（防守赛道实测教训）

### ISSUE-25：turn_low 口径陷阱
- 团队 turn_low = `factor_turnover(panel)`（**原始换手率 20 日均值**）+ 低换手 **top20%**（约 1100 只），**不是**注册表 rank + top20 只
- 用错规格 → 复现 -37% 假负；修正后精确复现团队 +15.95% ✅
- 教训：复现团队定稿因子前，先读 `core/factors.py` 的专用函数定义（`factor_turnover` 等），别用注册表近似

### ISSUE-26：score 归一化改变选股（最隐蔽）
- 混合测试用 min-max 归一化表示 turn_low（`(x-min)/(x-max)`），turn20 含大量 NaN → **min-max 改变缺失分布 → 选出完全不同股票** → 混合 +26.4% 假象
- 真实结果（rank 表示）：**任何混合都稀释 turn_low**（夏普 0.71-0.91 vs 1.11）——"防守混合均失败"再次成立
- 教训：**score 必须用 rank 表示，禁止 min-max**；混合测试前先验证单因子口径可复现（turn_low 应精确复现 +15.95%）

### 防守赛道真实结论（修正版）
- `def_quiet_lowvol`（TS_RANK20(turn_quiet×lowvol)）：单因子年化 +18.7% 超 turn_low +2.7pp，但回撤 -28.4%（3 倍）、夏普 0.79——**🟡 观察级，不入池**
- 唯一真增量候选仍为 turn_low；AlphaGPT 生成因子连续两轮（半导体/防守）未产出可入池因子——符合 8% 转化率校准

### ISSUE-27：score 生成顺序铁律（rank 先, fill 后）
- `fillna(0.0)` 在 `rank()` **前**会污染排名：填充值参与排名 → 大量股票 rank≈0.5 假象（daily_scores 全列 0.5001 实证）
- **正确顺序**：`raw.replace(inf) → raw.rank(pct=True) → fillna(0.0)`（先排名后填充；未覆盖=0 不参与排名）
- 影响：topN 选股不受影响（填充值排名靠后），但 daily_scores 输出会失真 → 三件套登记前必须检查 rank 分布

### 质量交叉域结论（2026-08-15）
- **q_x_quiet_turn = TS_RANK20(fscore × turn_quiet × turnover)**：组合层 +5.2pp（40d top20）、8 年全正、holdout 131%、与 turn_low 正交 0.017、容量 6051 万/日——**🟡 观察级（唯一组合层正超额候选）**
- 质量×其他量价结构（高开/振幅/影线/偏度/量价相关）39 候选全证伪——**turn_quiet 是质量交叉的唯一有效成分**
- fscore PIT 口径核查通过（`_apply_pit` 用 ann_date merge_asof backward，无未来函数）
