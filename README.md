# DeepSeek HARNESS Quant

自然语言驱动的 A 股量化系统。低频。主观决策 + 写死引擎 + 数据裁决。

AI 不预测个股。这是硬约束，不是选项。

```
驱动层  语言模型        控制 · 挖因子 · 审计 · 牛散蒸馏
执行层  写死引擎        评分 · 回测 · 风控 · 扫描（确定性 Python，可复现）
事实层  数据系统        PIT · T+1 · 覆盖率分年核查（可证伪）
```

## 开始

新用户直接走 [一键部署](docs/一键部署.md)。接入 AI API 后，数据源、配置、验证全交给 AI 向导，不必读本页其余内容。

## 接入 DeepSeek API

接入 API 后，语言模型接管驱动层。控制、挖因子、审计、魔改，全部解锁。不接入，则只有写死引擎，没有 AI。

**前提**：完整包（zip）已内置 HARNESS 运行时；单文件（exe）不含 HARNESS，AI 控制台不可用。系统需 Node.js 22.19+ 或 24+（https://nodejs.org），无则 HARNESS 自动跳过、量化系统照常。

**最快方式**：双击 `接入API.cmd`，粘贴 Key，自动写入并验证。

```bash
# 1. 获取 Key
#    https://platform.deepseek.com/api_keys

# 2. 复制凭据模板，填入 Key（或直接用接入API.cmd）
copy harness\home\.credentials.yaml.example harness\home\.credentials.yaml
#    编辑 .credentials.yaml：
#    DEEPSEEK_API_KEY: sk-<your-key>

# 3. 启动（打印「启动 DeepSeek HARNESS」即集成成功）
python launcher.py
#    http://127.0.0.1:8787/control
```

接入后，直接对话：

- **控制系统**：发「1」触发自主推进（审计 / 修复 / 归档，系统自我进化）
- **挖因子**：说「研究散户情绪量化」——AI 拆假设 → 生成因子 → 九步验证 → 入池或证伪
- **审计**：让 AI 自查前视、覆盖率、共线性、过拟合
- **牛散**：7 位牛散人格对话选股，决策自动入远期池验证
- **魔改**：动态 Cordis 插件热更新，系统行为运行时改，不重编译

其余数据源（Tushare 等）接入后让 AI 指导完成。核心逻辑：先接 AI 的 API，AI 帮你接剩下的。

## 架构

| 模块 | 职责 |
|---|---|
| data/ | 数据获取 + 本地缓存 + Point-in-Time（cache.py 唯一读取接口） |
| factors/ | 因子引擎 + 机会扫描 + 远期池（五池 T+1/5/20/60 验证） |
| strategy/ | 决策链（L0 择时 → L2 Pitch 审批 → T+1 执行）+ 组合构建 |
| risk/ | 风控七道（数据审计 / 因子健康 / FRC 排雷 / Beneish / 竞价 / 单因子 / L0 门控） |
| backtest/ | 回测引擎（T+1 开盘 / 一字板过滤 / 成本模型 / 结论分级） |
| etf/ | ETF 映射（策略暴露 → 可交易配置） |
| deck/ ui_v2/ | Web 决策台（9 页，前端零硬编码） |
| harness/ | DeepSeek HARNESS 运行时（AI 控制台 / 牛散桥 / 动态插件） |
| config/ | 策略注册表 / ETF 池 / 阈值，全部配置化 + .example 模板 |

## 因子

业务因子目录由 `config/factors.yaml` 动态定义；数量、依赖、可用起点、方向先验和当前
准入状态不在 README 中写死。面板构建、证据评估、正式回测和 registry 逐级绑定各自的
run identity、源码指纹与输入数据指纹，任何一层变化都会使旧证据失效。

> **当前证据状态（2026-08-24）**：旧报告是在本轮 QFQ/PIT 合同加固前生成，不能作为
> 当前收益或准入结论。新的 canonical panel、T+1 正式回测和 evaluator archive 全部完成
> 且身份一致前，所有候选保持 research-only；本仓库不再展示或沿用旧报告中的分数、IC、
> ICIR、年化收益或“强有效”标签。

数据源覆盖采用显式状态：`complete`、`provisional`、`failed` 与未查询互不混淆；龙虎榜、
股东户数和社保持仓均由每日增量 DAG 更新。完整准入和重算顺序见
[因子接入策略与每日增量验收](docs/因子接入策略与每日增量验收.md)，运行后的当前状态由
因子页 API 展示，而不是由本文静态声明。

## Skill

12 个技能文件，封装方法论 + 资产 + 踩坑记录，AI 按需加载。

- 因子挖掘：factor-mining-workflow · alpha-gpt-factor-mining · alpha-gpt-researcher · backtest-acceptance
- 牛散蒸馏：niu-san-distillation + 林园 / 陈小群 / 章盟主 / 赵老哥 / 炒股养家 / 冯柳
- 系统维护：github-maintainer

## 安装与运行

```bash
# 源码
pip install -r requirements.txt
python data/demo/build_demo_db.py    # 生成演示数据
python launcher.py                   # deck:8787 + HARNESS:3080

# 单文件
QuantDeck.exe                        # 双击即用，自动开浏览器

# 完整包
DSHQuant-v1.0.9-Release.zip          # 解压即用，含 HARNESS 运行时
```

## 快速回归

提交前运行一条离线、无持久副作用的回归命令：

```bash
.venv/bin/python -B scripts/quick_regression.py
```

它覆盖 PIT 披露时点、共享市场生命周期、T+1 开盘撮合/一字板/成本、QFQ 重建/发布恢复、动态因子目录、
不可变面板与 WAL-aware 内容身份、三类披露增量/状态、页面与 API 200、HARNESS 唯一 home/派单
合同，以及 Python/JS/配置语法；子进程禁止 Python/Node 联网并拒绝外部 provider/shell，
执行前后会对依赖目录外全部工作区和运行文件做内容核对，确认没有遗留改写。
若 8787 与 3080 已经启动，可加 `--live` 做额外只读身份检查；命令不会代为启动服务。

> **下载注意**：从 Release 页 **Assets 区**下载 `DSHQuant-v1.0.9-Release.zip`（完整包，含 HARNESS 运行时）。
> **不要**下载页面底部的 "Source code (zip)" —— 那是源码包，harness/node_modules 被排除，没有 AI 控制台。
> 判断：解压后 `harness\node_modules` 文件夹存在 = 完整包；不存在 = 下错了。

运行要求：Python 3.10–3.12（源码）/ 无（exe）。HARNESS 控制台需 Node.js 22.19+ 或 24+（可选）。

## 数据

数据由用户自行获取。系统不分发数据。

- 行情来自第三方（Tushare 等）。仓库只含获取脚本 + 合成演示数据。
- 换手率 2019 年前缺失，2019 前换手类结论作废。
- 配置：`config/params.yaml.example`（Tushare token）。

## 计划任务（每日自动更新）

原 Windows 版用 `schtasks` 注册 8 个任务（TushareInc 17:30 / 盘后扫描 17:35 / 因子档案 17:40 / 每日全链 18:30 / 因子池 19:15 / dev_auto 每 4h / 突破监控与守护每 30min）。

- **macOS**：`.venv/bin/python -B scripts/setup_launchd.py` 一键安装为 LaunchAgent（`~/Library/LaunchAgents/com.lwquant.*.plist`，模板见 `scripts/launchd/`）；`--status` 查看、`--uninstall` 卸载。
- **Windows**：旧 `LWQuant-*` schtasks 仅属历史版本；本轮单一 DAG 尚未提供 Windows 安装/迁移器，禁止与旧多写入任务并行启用。
- 状态看板：当前单一 DAG 的安装与加载状态以 macOS launchd 为已验收平台；Windows 迁移完成前不宣称同等支持。

## 许可

MIT。仅供研究学习。不构成投资建议。

## 文档

[资产盘点](docs/资产盘点.md) · [架构说明](docs/架构.md) · [一键部署](docs/一键部署.md) · [快速开始](docs/快速开始.md) · [HARNESS 接入](docs/HARNESS接入.md) · [数据说明](docs/数据说明.md) · [分钟数据接入](docs/分钟数据接入说明.md)

更新机制：`scripts/update.py`（manifest 驱动，用户配置保护，应用前自动备份）。
