# 更新日志

## v1.0.0（2026-08-16）

首个开源发布版本基线（代码 + HARNESS 运行时 + 演示数据 + 更新机制）。

- 全功能：Pitch 决策链 / 因子引擎 / 五池远期验证（含牛散主观）/ ETF 映射 / 全站 UI
- HARNESS 深度嵌入（桥接插件 + 7 空白牛散 skill + API Key 接入点）
- 动态化铁律落地：回测策略外部注册（config/strategies.yaml）、ETF 候选池配置化（config/etf_pool.yaml）
- 回测验收标准 skill（backtest-acceptance）
- 更新机制：scripts/update.py（manifest 驱动覆盖，用户配置/数据保护，应用前自动备份）

更新方式：见 docs/更新与发布.md
