# 代码骨架（融合工作流实测可用版）

> 全部代码在 2026-08-15 trial 中实跑通过（Python 3.13 / pandas 3.0.5 / canslim-quant .venv）。
> 完整可复现脚本：`_trial_alphagpt/semi_phase*.py`。

## 1. 环境与面板

```python
import sys
sys.path.insert(0, r"DSHQuant（本地主系统）\factor_pool\scripts")
sys.path.insert(0, r"DSHQuant（本地主系统）\factor_pool")
from research_engine import load_universe, get_industry   # 面板缓存 0.4s
from core.panel_v1 import _factor_single                  # 特征唯一入口
from core.combo_backtest import combo_backtest            # 验收v1 组合回测

panel = load_universe()                                   # (date, code) MultiIndex
ind = get_industry(panel)                                 # code -> 行业大类
```

## 2. 特征矩阵（符号化，C39 域示例）

```python
C39 = ind[ind.str.startswith("C39", na=False)].index.tolist()
FEATURES = ["sentiment", "turnover", "reversal20", "lowvol", "amihud", "beta_20", ...]
feat_mats = {}
for fn in FEATURES:
    s = _factor_single(panel, None, None, fn)
    m = s.unstack("code").astype(np.float32).rank(axis=1, pct=True)  # 截面 rank
    feat_mats[fn] = m[C39]          # 域内子集；行业相对特征再 rank 一次得 *_rel
```

## 3. 符号化 StackVM（三态返回）

```python
OP_ARITY = {'ADD':2,'SUB':2,'MUL':2,'DIV':2,'NEG':1,'ABS':1,'SIGN':1,'GATE':3,
            'JUMP':1,'DECAY':1,'DELAY1':1,'MAX3':1,'DELTA5':1,'MA20':1,'STD20':1,'TS_RANK20':1}

def validate(tokens):
    stack = 0
    for t in tokens:
        if t in FEATURES: stack += 1
        elif t in OP_ARITY:
            a = OP_ARITY[t]
            if stack < a: return False
            stack = stack - a + 1
        else: return False
    return stack == 1

def execute(tokens):
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
        return None, f"实现异常: {type(e).__name__}: {e}"   # 绝不静默吞异常
```

时序算子（pandas 3.x，无 axis 参数）：
```python
def ts_shift(df, d): return df.shift(d)
def op_ma(df, w=20): return df.rolling(w, min_periods=1).mean()
def op_zscore(df, w=20):
    m = df.rolling(w, min_periods=1).mean(); s = df.rolling(w, min_periods=1).std() + 1e-6
    return (df - m) / s
def op_decay(df): return df + 0.8*ts_shift(df,1) + 0.6*ts_shift(df,2)
```

## 4. ICIR 向量化（P4）

```python
def rankic(factor_df, target_df):
    fr = factor_df.rank(axis=1); tr = target_df.rank(axis=1)
    mask = factor_df.notna() & target_df.notna()
    fr = fr.where(mask); tr = tr.where(mask)
    fc = fr.sub(fr.mean(axis=1), axis=0); tc = tr.sub(tr.mean(axis=1), axis=0)
    n = mask.sum(axis=1)
    ic = (fc*tc).sum(axis=1) / (np.sqrt((fc**2).sum(axis=1))*np.sqrt((tc**2).sum(axis=1)) + 1e-9)
    return ic.where(n >= 20).dropna()     # 逐日 IC 序列 → ICIR = mean/std
```

## 5. 去重闸门（P4，ICIR 排序前）

```python
ANCHORS = {n: feat_mats[n] for n in ["reversal20","sentiment","turnover","lowvol",
                                     "amihud","turn_std20","turn_mid_prox"] if n in feat_mats}
def corr_with_anchors(cand):
    out = {}
    r = cand.rank(axis=1)
    for name, am in ANCHORS.items():
        mask = cand.notna() & am.notna()
        rr = r.where(mask); ar = am.rank(axis=1).where(mask)
        rc = rr.sub(rr.mean(axis=1), axis=0); ac = ar.sub(ar.mean(axis=1), axis=0)
        n = mask.sum(axis=1)
        c = (rc*ac).sum(axis=1)/(np.sqrt((rc**2).sum(axis=1))*np.sqrt((ac**2).sum(axis=1))+1e-9)
        c = c.where(n >= 20).dropna()
        out[name] = c.mean() if len(c) else np.nan
    return out
# 判定: max(corrs) > 0.5 淘汰 / 0.3-0.5 警示 / <0.3 通过
```

## 6. 组合层 T+1（P6，团队引擎）

```python
mat = evaluate(tokens)
if direction == -1: mat = -mat
score = mat.stack().rename("score")
score.index.names = ["date", "code"]
res = combo_backtest(panel, score, name, rebalance_days=20, top_n=10,
                     min_stocks=20, fix_qfq=True)
stats = res["stats"]   # 累计/年化/回撤/夏普/胜率
```

⚠️ 注意：
- score 用 `research_engine.load_universe()` 面板即可（同 data_loader.build_universe 缓存版；规范要求 `rp.load_panel()` 口径一致即可）
- **域内选股天然实现**：域外股票 score=NaN，`sort_values(descending).head(top_n)` 只选到域内
- code 口径：进 combo 前 `score.index = score.index.set_levels(score.index.levels[1].str.split('.').str[0].str.zfill(6), level=1)`（若带后缀）

## 7. 域内等权基准（P6，行业域必做——引擎只有全市场基准）

```python
panel_dom = panel[panel.index.get_level_values('code').isin(DOMAIN)]
close_dom = panel_dom["close"].unstack("code")
ret_dom = close_dom.pct_change().dropna(how="all")          # 按 code 算收益（勿跨 code pct_change=inf）
dates = ret_dom.index; rebal = dates[::rebalance_days]
rets = []
for i in range(len(rebal)-1):
    seg = ret_dom.loc[(ret_dom.index > rebal[i]) & (ret_dom.index <= rebal[i+1])]
    rets.append(seg.mean(axis=1).fillna(0))                 # 截面等权
bench = pd.concat(rets)
ann = (1+bench).prod()**(252/len(bench)) - 1
dd = ((1+bench).cumprod()/(1+bench).cumprod().cummax()-1).min()
excess_ann = stats["年化"] - ann                            # 超额年化
```

## 8. 分年度 + holdout（P7）

```python
ic.index = pd.to_datetime(ic.index)                         # pandas3 缓存索引先转 datetime
for y, g in ic.groupby(ic.index.year):
    print(y, g.mean()/(g.std()+1e-9))                       # 分年 ICIR
holdout = ic[ic.index >= "2025-01-01"]                      # holdout 2025-26
```

## 9. 正交性（P9，与团队定稿 turn_low）

```python
# turn_low = 全市场 40日 low turnover top20（团队定稿锚点）
turn20 = _factor_single(panel, None, None, "turnover")
cand = evaluate(tokens).stack()
corr = cand.groupby(level="date").corr(turn20.stack()).mean()   # <0.3 才可入池
```

## 10. 数据有效性审计（P3，★最优先）

```python
s = _factor_single(panel, None, None, fn)
cov = s.groupby(s.index.get_level_values('date').dt.year).apply(lambda x: x.notna().mean())
print(cov)   # 任何年份 <0.7 → 该年剔除或整因子弃用；回测范围 ≠ 因子有效范围
```
