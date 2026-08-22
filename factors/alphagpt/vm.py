# -*- coding: utf-8 -*-
"""factors/alphagpt/vm.py — A 股版 StackVM 公式求值器（numpy 实现，蒸馏自 AlphaGPT）

公式 = token id 序列（特征/算子，见 vocab.FORMULA_VOCAB），栈式求值：
  特征 → 压栈；算子 → 弹出 arity 个操作数，求值后压回。
合法公式以单值结束（栈深 1）。

用法：
  from factors.alphagpt.vocab import FORMULA_VOCAB, build_features
  from factors.alphagpt.vm import StackVM
  feats = build_features(P)                      # P = alpha_panel 价量面板 dict
  tokens = FORMULA_VOCAB.encode(["RET20", "TSRANK20", "CSRANK", "NEG"])
  panel_df = vm.execute(tokens, feats, index=P["close"].index, columns=P["close"].columns)
"""
import numpy as np

from .vocab import FORMULA_VOCAB, OPS_CONFIG


class StackVM:
    def __init__(self):
        self.feat_offset = FORMULA_VOCAB.operator_offset
        self.op_map = {i + self.feat_offset: (cfg[1], cfg[2])
                       for i, cfg in enumerate(OPS_CONFIG)}

    def execute(self, tokens, feats: dict, index=None, columns=None):
        """执行公式 token id 序列 → date×code 因子面板（DataFrame）或 None"""
        stack = []
        try:
            for token in tokens:
                token = int(token)
                if token < self.feat_offset:
                    name = FORMULA_VOCAB.feature_names[token]
                    if name not in feats:
                        return None
                    stack.append(feats[name])
                elif token in self.op_map:
                    func, arity = self.op_map[token]
                    if len(stack) < arity:
                        return None
                    args = [stack.pop() for _ in range(arity)][::-1]
                    res = func(*args)
                    if res is None:
                        return None
                    res = np.asarray(res, dtype=float)
                    stack.append(np.nan_to_num(res, nan=0.0, posinf=1e9, neginf=-1e9))
                else:
                    return None
            if len(stack) == 1:
                out = stack[0]
                if index is not None and columns is not None:
                    import pandas as pd
                    return pd.DataFrame(out, index=index, columns=columns)
                return out
            return None
        except Exception:
            return None

    def execute_tokens(self, token_names: list, feats: dict, index=None, columns=None):
        """按 token 名字执行（便捷入口）"""
        return self.execute(FORMULA_VOCAB.encode(token_names), feats, index, columns)


# ---------------- 公式语法（生成器用） ----------------

def validate_formula(tokens: list) -> bool:
    """校验 token 序列是否为合法公式（栈平衡且单值结束）"""
    stack = 0
    feat_offset = FORMULA_VOCAB.operator_offset
    for t in tokens:
        t = int(t)
        if t < feat_offset:
            stack += 1
        else:
            arity = dict((i + feat_offset, cfg[2]) for i, cfg in enumerate(OPS_CONFIG))[t]
            if stack < arity:
                return False
            stack -= arity - 1
        if stack > 8:   # 防爆炸
            return False
    return stack == 1


def validate_names(token_names: list) -> bool:
    try:
        return validate_formula(FORMULA_VOCAB.encode(token_names))
    except Exception:
        return False
