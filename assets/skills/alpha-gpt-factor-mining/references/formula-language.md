# 公式语言规范（AlphaGPT 蒸馏）

> 来源：`_AlphaGPT_ref/model_core/{vocab,ops,vm}.py` + `_AlphaGPT_ref/times.py`（A 股适配版）。

## 1. 总则

公式是一棵**表达式树**的线性编码（逆波兰式 token 序列）。节点分两类：

- **特征（叶子）**：arity = 0，直接从特征张量取一行。
- **算子（内部节点）**：arity ≥ 1，从栈中弹取操作数，计算结果压回。

一条合法公式 = 执行完后栈中恰好剩余 1 个值；否则判非法（奖励层面直接负分）。

## 2. 特征词表

### 主流程（`vocab.py`，6 个，meme 场景）

| token | 定义 | 备注 |
|---|---|---|
| `RET` | log(close/lag1(close)) | robust 归一化 |
| `LIQ_SCORE` | clamp(liquidity/fdv × 4, 0, 1) | 流动性健康度 |
| `PRESSURE` | tanh(3 × (close-open)/(high-low)) | 买卖力量 |
| `FOMO` | 成交量一阶/二阶差分，clamp ±5 | 量能异动 |
| `DEV` | (close - MA20)/MA20 | 偏离均值 |
| `LOG_VOL` | log1p(volume) | 对数量 |

### A 股适配（`times.py`，5 个）

| token | 定义 | 备注 |
|---|---|---|
| `RET` | (close-lag1)/lag1 | robust 归一化 |
| `RET5` | pct_change(5) | 中短期动量 |
| `VOL_CHG` | vol/MA20(vol) - 1 | 量能相对水平 |
| `V_RET` | ret × (vol_chg + 1) | 量价结合 |
| `TREND` | close/MA60(close) - 1 | 长期趋势 |

### 落地到本工作区（建议替换示例）

| token | 工作区实现 | 来源 |
|---|---|---|
| `RET` | close/prev_close - 1 | bars.db |
| `TURN` | 20 日均换手率 | bars.turn |
| `VOL_CHG` | volume/MA20(volume) - 1 | bars.volume |
| `LIMUP_EX` | 非一字板涨停标记 × ret（5 日滚动和） | 工作区已有 limup_ex_5 逻辑 |
| `VOLAT` | 20 日收益波动率 | bars |
| `DEVIATION` | close/MA60 - 1 | bars |
| `AMIHUD` | |ret|/amount | 工作区已有 amihud |
| `MV` | log 市值（中小盘域约束用） | hist_mv |

所有特征统一 **robust 归一化**：`(x - median) / (MAD + 1e-6)`，clip 到 [-5, 5]。

## 3. 算子词表

### 基础 12 算子（`ops.py`）

| token | arity | 实现 |
|---|---|---|
| `ADD` | 2 | x + y |
| `SUB` | 2 | x - y |
| `MUL` | 2 | x * y |
| `DIV` | 2 | x / (y + 1e-6)（主流程）/ x / (y + 1e-6·sign(y))（A股版，保号保护） |
| `NEG` | 1 | -x |
| `ABS` | 1 | |x| |
| `SIGN` | 1 | sign(x) |
| `GATE` | 3 | (cond>0) ? x : y |
| `JUMP` | 1 | relu(zscore(x) - 3)，极端跳变检测 |
| `DECAY` | 1 | x + 0.8·lag1(x) + 0.6·lag2(x) |
| `DELAY1` | 1 | lag1(x) |
| `MAX3` | 1 | max(x, lag1(x), lag2(x)) |

### A 股版追加 4 算子（`times.py`）

| token | arity | 实现 |
|---|---|---|
| `DELTA5` | 1 | x - lag5(x) |
| `MA20` | 1 | 线性衰减加权 20 日均（权重 1..20 归一化） |
| `STD20` | 1 | 20 日滚动 zscore（(x-mean)/std） |
| `TS_RANK20` | 1 | 20 日滚动 zscore（近似 Rank） |

## 4. 求值语义（StackVM）

```python
def execute(formula_tokens, feat_tensor):
    stack = []
    for token in formula_tokens:
        if token 是特征:
            stack.append(feat_tensor[token])        # 压栈
        elif token 是算子:
            args = [stack.pop() for _ in range(arity)]  # 弹 arity 个
            res = op(*reversed(args))               # 计算
            res = nan_to_num(res, nan=0, posinf=1, neginf=-1)  # 清洗
            stack.append(res)                       # 压回
        else:
            return None                             # 非法
    return stack[0] if len(stack) == 1 else None
```

任意异常（栈溢出/不足、形状错）→ 返回 None → 公式判非法。

## 5. 生成合法性掩码（训练时用）

维护 `open_slots`（栈剩余坑位）：
- 初始 = 1（根节点位置）。
- 选特征：open_slots -= 1；选 arity=k 算子：open_slots += k-1。
- 每步掩码：
  - open_slots == 0 → 只能选特征（填坑）；
  - open_slots >= 剩余步数 → 只能选特征（否则填不满）；
  - 否则特征和算子都可选。
- 结束条件：步数走完（公式长度上限 MAX_FORMULA_LEN）。

## 6. 示例公式

meme 版风格（token → 可读）：
```
[RET, DEV, SUB, FOMO, MUL, JUMP]  →  JUMP( FOMO × (RET - DEV) )
[LIQ_SCORE, RET, GATE, DECAY]     →  DECAY( GATE(LIQ_SCORE, RET, ...) )
```

A 股版风格：
```
[RET5, TREND, SUB, STD20, MUL]    →  STD20( (RET5 - TREND) × ... )
```

解码器（`times.py decode`）：前缀式 `OP(feat, feat)` 递归还原。

## 7. 注意事项

- **DIV 保号**：A 股版 `y + 1e-6·sign(y)` 防止除以接近 0 的负数导致符号翻转。
- **NaN/Inf 就地清洗**：0 / ±1，绝不让异常值传播。
- **常量判定**：结果 std < 1e-4 视为常量因子，判负分（无信息量）。
- **公式越短越稳**：A 股版 MAX_SEQ_LEN=8 的注释："限制公式长度，防止过拟合，短小精悍的公式往往更稳"。
