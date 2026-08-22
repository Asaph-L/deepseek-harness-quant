# -*- coding: utf-8 -*-
"""factors/alphagpt/__init__.py — A 股版 AlphaGPT 因子公式引擎（蒸馏自 imbue-bit/AlphaGPT）

思路（与原版一致）：公式语言表达因子 → StackVM 求值 → 回测奖励驱动生成器迭代。
差异：特征/数据用本仓库 A 股面板（alpha_panel），VM 用 numpy 纯实现（无 torch/Solana 依赖），
生成器支持 随机（无 API）/ LLM（DeepSeek API，可选）。

用法：
  from factors.alphagpt.vm import StackVM
  from factors.alphagpt.vocab import FORMULA_VOCAB
  vm = StackVM()
  panel = vm.execute_tokens(tokens, feats)     # → date×code 因子面板
"""
