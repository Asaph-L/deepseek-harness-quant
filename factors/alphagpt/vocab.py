# -*- coding: utf-8 -*-
"""factors/alphagpt/vocab.py — A 股因子公式词表（特征 + 算子）

特征 = 可被公式引用的原子量（由 alpha_panel 价量面板派生，日频 date×code）；
算子 = 栈式求值的函数（与 AlphaGPT 原版 12 算子同构 + A 股常用时序算子扩展）。

token 编码：0..F-1 = 特征，F..F+O-1 = 算子（栈 VM 依 token 值路由）。
"""
from dataclasses import dataclass

# ---------------- 算子配置 (name, func, arity) ----------------
# func 接收 numpy 数组（date×code 面板）或标量，返回同形状数组
import numpy as np


def _op_delay(x, d=1):
    out = np.full_like(x, np.nan)
    out[d:] = x[:-d]
    return out


def _op_gate(cond, x, y):
    return np.where(cond > 0, x, y)


def _op_jump(x):
    """截面 z-score 后 >3σ 置 1 的跳变门（原版 relu(z-3)）"""
    mu = np.nanmean(x, axis=1, keepdims=True)
    sd = np.nanstd(x, axis=1, keepdims=True) + 1e-9
    z = (x - mu) / sd
    return np.maximum(z - 3.0, 0.0)


def _op_decay(x):
    return x + 0.8 * _op_delay(x, 1) + 0.6 * _op_delay(x, 2)


def _op_max3(x):
    return np.maximum.reduce([x, _op_delay(x, 1), _op_delay(x, 2)])


def _op_tsmean(x, n=20):
    out = np.full_like(x, np.nan)
    for i in range(x.shape[0]):
        lo = max(0, i - n + 1)
        out[i] = np.nanmean(x[lo:i + 1], axis=0)
    return out


def _op_tsstd(x, n=20):
    out = np.full_like(x, np.nan)
    for i in range(x.shape[0]):
        lo = max(0, i - n + 1)
        out[i] = np.nanstd(x[lo:i + 1], axis=0)
    return out


def _op_tsrank(x, n=20):
    """时间序列分位：当前值在过去 n 天窗口内的位置分位（0-1）"""
    out = np.full_like(x, np.nan)
    for i in range(x.shape[0]):
        lo = max(0, i - n + 1)
        win = x[lo:i + 1]
        with np.errstate(invalid="ignore"):
            out[i] = np.nanmean(win <= x[i], axis=0)
    return out


def _op_csrank(x):
    """截面分位（每日全市场 rank，0-1）"""
    out = np.full_like(x, np.nan)
    for i in range(x.shape[0]):
        row = x[i]
        valid = ~np.isnan(row)
        if valid.sum() < 5:
            continue
        ranks = np.argsort(np.argsort(row[valid])) / (valid.sum() - 1)
        out[i, valid] = ranks
    return out


OPS_CONFIG = [
    ("ADD", lambda x, y: x + y, 2),
    ("SUB", lambda x, y: x - y, 2),
    ("MUL", lambda x, y: x * y, 2),
    ("DIV", lambda x, y: x / (y + 1e-9), 2),
    ("NEG", lambda x: -x, 1),
    ("ABS", np.abs, 1),
    ("SIGN", np.sign, 1),
    ("GATE", _op_gate, 3),
    ("JUMP", _op_jump, 1),
    ("DECAY", _op_decay, 1),
    ("DELAY1", lambda x: _op_delay(x, 1), 1),
    ("MAX3", _op_max3, 1),
    ("TSMEAN20", lambda x: _op_tsmean(x, 20), 1),
    ("TSSTD20", lambda x: _op_tsstd(x, 20), 1),
    ("TSRANK20", lambda x: _op_tsrank(x, 20), 1),
    ("CSRANK", _op_csrank, 1),
]

# ---------------- 特征（A 股价量派生） ----------------
# 名称 → 由 alpha_panel 价量面板构造的函数（输入 P: dict[str, np.ndarray date×code]）
FEATURE_NAMES = (
    "RET5",      # 5 日收益
    "RET20",     # 20 日收益
    "RET60",     # 60 日收益
    "LOG_VOL",   # log(volume)
    "LOG_AMT",   # log(amount)
    "TURN",      # 换手率
    "VOL20",     # 20 日波动
    "AMIHUD",    # 非流动性
    "REV5",      # 5 日反转（-RET5）
    "MAXRET20",  # 20 日最大日收益
    "RANGE",     # (high-low)/close
    "O2C",       # open-to-close 日内收益
)


def build_features(P: dict) -> dict:
    """从 alpha_panel 价量面板 dict（date×code DataFrame）构造特征张量 dict {name: np.ndarray}"""
    import pandas as pd
    close = P["close"]
    ret = close.pct_change()
    feats = {
        "RET5": ret.rolling(5).sum().to_numpy(),
        "RET20": ret.rolling(20).sum().to_numpy(),
        "RET60": ret.rolling(60).sum().to_numpy(),
        "LOG_VOL": np.log(P["volume"].to_numpy() + 1e-6),
        "LOG_AMT": np.log(P["amount"].to_numpy() + 1e-6),
        "TURN": P["turn"].to_numpy(),
        "VOL20": ret.rolling(20).std().to_numpy(),
        "AMIHUD": (ret.abs() / (P["amount"].to_numpy() + 1e-8)).rolling(20).mean().to_numpy()
                  if hasattr(ret.abs() / (P["amount"].to_numpy() + 1e-8), "rolling") else
                  np.full(close.shape, np.nan),
        "REV5": -ret.rolling(5).sum().to_numpy(),
        "MAXRET20": ret.rolling(20).max().to_numpy(),
        "RANGE": ((P["high"] - P["low"]) / close).to_numpy(),
        "O2C": (P["close"] / P["open"] - 1).to_numpy(),
    }
    return {k: np.asarray(v, dtype=float) for k, v in feats.items()}


@dataclass(frozen=True)
class FormulaVocab:
    feature_names: tuple
    operator_names: tuple

    @property
    def feature_count(self):
        return len(self.feature_names)

    @property
    def operator_offset(self):
        return len(self.feature_names)

    @property
    def token_names(self):
        return self.feature_names + self.operator_names

    @property
    def size(self):
        return len(self.token_names)

    def encode(self, tokens):
        """token 名字列表 → id 列表"""
        name2id = {n: i for i, n in enumerate(self.token_names)}
        return [name2id[t] for t in tokens]

    def decode(self, ids):
        return [self.token_names[i] for i in ids]


FORMULA_VOCAB = FormulaVocab(
    feature_names=FEATURE_NAMES,
    operator_names=tuple(cfg[0] for cfg in OPS_CONFIG),
)
