# -*- coding: utf-8 -*-
"""factors/alphagpt/generator.py — 公式生成器（随机 / LLM）

随机生成器：随机采样 token 序列，保证栈平衡、单值结束、深度受限（语法过滤）。
LLM 生成器：DeepSeek API 提示词生成公式 token 序列（可选；无 key 时仅随机）。

用法：
  from factors.alphagpt.generator import random_formula, generate_batch
  formulas = generate_batch(200)          # [["RET20","TSRANK20","CSRANK","NEG"], ...]
"""
import random

from .vocab import FORMULA_VOCAB

FEATURE_IDS = list(range(FORMULA_VOCAB.feature_count))
OP_IDS = {i + FORMULA_VOCAB.operator_offset: FORMULA_VOCAB.operator_names[i]
          for i in range(len(FORMULA_VOCAB.operator_names))}
MAX_DEPTH = 6


def random_formula(max_depth: int = MAX_DEPTH) -> list:
    """生成一条随机合法公式（token id 列表，后缀表达式，栈平衡单值结束）
    ★2026-08-23 修复：递归构建（原 while 循环条件恒假 → 只产单特征 → 去重后死循环）"""
    feat_count = FORMULA_VOCAB.feature_count
    n_ops = len(OP_IDS)

    def gen(depth: int) -> list:
        # 叶子：特征（保证推进）；非叶子：算子 + 递归操作数
        if depth <= 1 or random.random() < 0.55:
            return [random.randrange(feat_count)]
        op_id = random.randrange(n_ops) + feat_count
        arity = _arity_of(op_id)
        parts = []
        for _ in range(arity):
            parts += gen(depth - 1)
        return parts + [op_id]

    return gen(max_depth)


def _arity_of(op_id: int) -> int:
    from .vocab import OPS_CONFIG
    idx = op_id - FORMULA_VOCAB.operator_offset
    return OPS_CONFIG[idx][2]


def generate_batch(n: int, max_depth: int = MAX_DEPTH) -> list:
    """批量生成 n 条公式（去重，返回 token id 列表）"""
    seen = set()
    out = []
    while len(out) < n and len(seen) < n * 30:
        t = tuple(random_formula(max_depth))
        if t not in seen:
            seen.add(t)
            out.append(list(t))
    return out


def names_of(tokens: list) -> list:
    return FORMULA_VOCAB.decode(tokens)


def llm_generate(api_key: str = None, base_url: str = "https://api.deepseek.com",
                 n: int = 10, extra_hint: str = "") -> list:
    """LLM 生成公式（DeepSeek API，可选）。无 key 返回 []（走随机生成器）。
    ★2026-08-23 修复：① 按 n 发多次请求（原 1 次只回 1 行）② 过滤词表外 token 的行
    （LLM 会编造不存在的特征，如 CSMIX——整条含未知 token 即拒）③ 提示词明确禁止编造"""
    if not api_key:
        return []
    import json
    import urllib.request
    feat_names = "、".join(FORMULA_VOCAB.feature_names)
    op_names = "、".join(FORMULA_VOCAB.operator_names)
    vocab_set = set(FORMULA_VOCAB.token_names)
    out = []
    for _ in range(max(1, min(n, 8))):
        prompt = (
            f"你是 A 股因子挖掘引擎。用下面的词表生成一条合法因子公式（后缀表达式，栈式求值，"
            f"最终栈深=1）。\n特征：{feat_names}\n算子：{op_names}（二元算子用前两个操作数，"
            f"GATE 三元：cond,x,y）\n{extra_hint}\n"
            "【硬性要求】只能使用上述词表中的 token，禁止编造任何不存在的名字。"
            "只输出一行 token 名，空格分隔，例如：RET20 TSRANK20 CSRANK NEG\n"
        )
        req = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps({
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200, "temperature": 1.1,
            }).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode())
            for choice in d.get("choices", []):
                text = choice.get("message", {}).get("content", "")
                for line in text.splitlines():
                    names = [x.strip() for x in line.replace("，", " ").split() if x.strip()]
                    # ★过滤含词表外 token 的行（LLM 编造如 CSMIX）
                    if not names or not all(x in vocab_set for x in names):
                        continue
                    if _valid_names(names) and names not in out:
                        out.append(names)
        except Exception:
            pass
    return [FORMULA_VOCAB.encode(x) for x in out]


def _valid_names(names: list) -> bool:
    try:
        from .vm import validate_names
        return validate_names(names)
    except Exception:
        return False
