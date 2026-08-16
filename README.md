# DeepSeek HARNESS Quant

**自然语言驱动的 AI 量化系统**：DeepSeek HARNESS 深度嵌入——用自然语言控制系统、魔改系统、自动挖掘因子；全模块化、信息 API 化、数据每日动态更新。

> 开源 MIT ｜ 代码 + HARNESS 运行时 + 演示数据随包分发 ｜ 密钥零残留，开箱即用

## 为什么有效

不靠单一神奇因子，靠三样：**被严格证伪过的因子、正确的组合结构、机械化的纪律执行**。

- **三重实证纪律**：T+1 无前视（信号日收盘→次日开盘）· PIT 口径（真实披露日）· 分年度 + 覆盖率分年核查
- **证伪文化**：17+ 项假 alpha 公开留档（热度板块/散户情绪择时/MA20 择时/GTJA191/Qlib-ML 全量等），不造神只实证
- **关键洞察**：因子 ICIR 高 = 排序能力（雷达）≠ 持仓收益。高分集中持仓实证 -60% 回撤；主力走 turn_low 分散防守（+15.95%/-8.7%/1.11，2022 熊市 +6.7%），pitch 高分只做卫星仓
- **纪律执行**：持股 ≤5、先卖后买、一字板跳过、类型定制止损（防守类删除硬止损，避免策略被摧毁）

## 什么策略

**低频主观量化 + Pitch 决策系统 + 自动挖掘验证的因子池。**

- **低频**：周/月频调仓，容量友好（0.5-1 亿）；买入指令 = L2 决策卡片聚合，由人审批，非全自动黑箱
- **Pitch 系统**：全市场扫描 → 候选卡（评分/胜率/1/2/3 年证据/止损）→ 人工审批 → 唯一买入指令；远期池自动追踪 T+1/5/20/60 实际收益
- **自动挖掘因子**：AlphaGPT 方法论蒸馏成 skill（公式语言 + StackVM + REINFORCE 生成器迭代），AI 自动生成因子公式
- **自带大量因子**：123+ 因子（量价/短线涨跌停/情绪日内/机构/基本面低频/行业/Alpha101 复刻），全库最强 limup_ex_5 1.239、open_prem_20 0.958、lhb_jg_cnt_20 0.900
- **统一验证链**：九步入池（ICIR 初筛→去重→组合层 T+1→分年度 holdout→正交→容量→归档）

## 为什么创新

把 AI 从"写代码的工具"升级为"系统本身的一部分"，并用工程化实证兜底。

- **AI 原生架构**：HARNESS 深度嵌入，AI 控制层与量化执行层分离；自然语言控制系统、自主审计/修复/归档、动态插件热更新
- **因子研究生成器驱动**：LLM/RL 自动生成+回测+迭代，替代人工枚举
- **主观智慧可验证**：7 位牛散蒸馏成可验证假设，接入五池远期验证（按决策者分组，数据说话）
- **证伪文化工程化**：把量化最隐蔽的坑（截面 ICIR ≠ 组合层、前视、覆盖率污染、过拟合）做成强制流程
- **全模块化动态更新**：因子注册即自动接入；配置化 + API 化；每日自愈式自动链 + 因子健康实时监控

## 架构

```
data/       数据获取（Tushare/akshare/baostock）+ 本地缓存 + Point-in-Time（cache.py 唯一读取接口）
factors/    因子引擎 + 机会扫描（scan.py --pitch）+ 远期池（五池验证）+ 择时/政策
strategy/   决策链（L0 择时 → L2 Pitch 审批 → T+1 执行）+ 组合构建（turn_low 防守主力 + pitch 卫星）
risk/       风控（RiskAgent 六道 + 类型定制止损 + 数据审计 + Beneish M-Score + 假信号排雷）
backtest/   回测引擎（T+1 开盘执行 / 一字板过滤 / 成本模型 / 结论分级）
etf/        ETF 映射（策略暴露表达成可交易配置）
deck/ ui_v2/  Web 决策台（9 页：门户/控制/决策/持仓/因子/回测/ETF/远期/说明）
harness/    DeepSeek HARNESS 运行时（核心：AI 控制台对话 / 牛散主观桥 / 动态插件 /quant/*）
config/     动态化配置（strategies.yaml / etf_pool.yaml，带 .example 模板）
```

## 快速开始（演示数据开箱即用）

```bash
pip install -r requirements.txt
python scripts/build_demo_db.py     # 生成合成演示数据（30 股 × 250 日）
python launcher.py                  # 一键启动 deck:8787 + HARNESS:3080（AI 控制台）
```

也可用单文件 EXE（Release 附件）或完整包（Release zip，含 HARNESS 运行时）。可选配置：`config/params.yaml.example`（Tushare token）、`harness/home/.credentials.yaml.example`（DeepSeek API Key）。启动后打开 http://127.0.0.1:8787，门户→控制页即可与 AI 对话（发「1」触发自主推进）。

## 数据与许可

- MIT License；行情数据来自第三方（Tushare 等），禁止再分发——只提供获取脚本 + 合成演示数据
- 仅供个人研究学习，不构成投资建议

## 版本

见 `CHANGELOG.md`；更新机制 `scripts/update.py`（manifest 驱动，用户配置/数据保护，应用前自动备份）。
