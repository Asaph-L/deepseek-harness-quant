---
name: github-maintainer
description: GitHub 维护专员档案（主系统信息库 + 开源仓库维护手册）。内含 LWQuant/Deepseek HARNESS Quant 主系统全景（架构/决策链/铁律/数据边界/实证结论/当前状态）、全量文件索引（主系统代码/数据/知识库/归档/设计书/因子池/技能），以及 deepseek-harness-quant 开源仓库的发布/更新/敏感扫描/版本规范操作手册。当用户要求"维护 GitHub 仓库/开源包/发布版本/更新机制/检查仓库状态"，或需要主系统上下文（架构/文件在哪/当前状态/待办）时使用。
whenToUse: 用户提到 GitHub、开源仓库、发布 Release、开源包、打包、仓库维护、更新机制、敏感信息扫描；或询问主系统架构、文件索引、当前状态、决策链、铁律。
metadata:
  version: v1.0
  created: 2026-08-16
  scope: 主系统 DSHQuant + 开源仓库 deepseek-harness-quant
  role: GitHub 维护专员（主系统架构师降级角色）
---

# GitHub 维护专员档案（v1.0）

> 角色：主系统架构师 → **GitHub 维护专员**。职责：维护开源仓库 deepseek-harness-quant（发布/更新/安全检查），
> 并作为主系统信息库——任何会话需要主系统上下文时先读本文件 + 索引中的关键文件。
> 本文件是记忆恢复入口：`09_总结归档/记忆恢复_主系统架构师_20260816.md` 与 `09_总结归档/GitHub开源发布完成记录_20260816.md` 为最新归档。

> ⚠️ **项目定位（2026-08-16 二次修正）**：品牌 = **DeepSeek HARNESS Quant**（已彻底去除 LWQuant / 劳威派 旧品牌，展示层 22 文件已 rebrand 推送）。
> 核心定位：**自然语言驱动的 AI 量化系统**——HARNESS 深度嵌入（AI 控制台对话/自主推进/动态插件）、自动挖掘因子（蒸馏 skill）、全模块化、信息 API 化、动态更新。详见 `09_总结归档/核心优势总结_20260816.md`（对外口径）。
> `canslim-quant` 仅为历史目录名（**2026-08-16 已更名为 `DSHQuant`**），**与威廉·欧奈尔/CANSLIM 无继承关系**。严禁出现"欧奈尔/CANSLIM 纪律骨架"等旧表述；代码注释残留（`data/finance_calc.py`、`scripts/build_update.py` 的路径脱敏规则——后者为功能性，勿删）。保留的工程标识：`LWQUANT_CACHE_DIR` 环境变量、`LWQuant-*` Windows 计划任务名（改名破坏兼容，勿动）。

---

## 一、系统拓扑与分工

| 层 | 位置 | 说明 |
|---|---|---|
| 主系统 | `DSHQuant（本地主系统）`（2026-08-16 由 canslim-quant 改名；标准分层 scripts/docs/build/config） | venv=`.venv/Scripts/python.exe`；deck :8787；代码=deck/data/factors/strategy/risk/backtest/etf/ui_v2/validation |
| 数据 | `D:/劳威派量化数据/cache/` + `minute/` | bars.db(18.7M 行 qfq 2010-2026)/finance.db/finance_ts.db/hist_mv.db/gdhs_full.db/lhb_full.db/rzrq_full.db/consensus_snap.db/shebao.db |
| 知识库 | `Desktop/量化代码库思路库`（工作区） | 01_研究/02_Pitch包/04_复刻/05_复现/06_数据存档/07-08_UI/09_总结归档(记忆)/10_历史任务包/11_设计书/AI协作 |
| 因子池 | `劳威派量化/DSHQuant/factor_pool/` | core(panel_v1/factors/combo_backtest/run_pool)/scripts/pitch/output(combo_reports 123+ 份) |
| **开源仓库** | **`D:\deepseek-harness-quant`** | 见第五节；GitHub：https://github.com/yuanwang589-dev/deepseek-harness-quant |
| HARNESS 桥 | 本机 :3080 | dsq-quant-bridge 插件：/quant/chat2 /quant/sessions /quant/niu/sessions；控制页 = 量化系统 /control |

分工：主系统架构师=生产执行+裁决 ｜ 研究员=策略/论文（01_，R 系列）｜ 因子池=实证（B/C/E/F/G/O 系列）｜ AlphaGPT 研究员=科研通道（产出走 9 步入池流程，不直接进决策链）。

## 二、决策链与铁律（定稿）

```
L0 择时 → 满仓主义大波段参考（红绿灯不接线）；防御期(择时<40) revalue/tech_sentiment 硬拒绝 403
L2 主观精选 → Pitch 长线(pitch_v2) + 短线(tech_pitch) → 审批 buy/drop → 今日买入指令 = 卡片同源聚合
L1 因子参考 → 机会池/机器池(machine_pool)/竞价/turn_low 防守参考（不产生指令）
执行 → T+1 开盘、一字板过滤、≤5 持仓先卖后买、宁缺毋滥
每日链 → 16:30 竞价 / 17:30 Tushare / 18:30 主系统全链 / 19:15 消费因子池 / 19:45 FactorDaily / 周末跳过
```

铁律 12 条：①T+1 无前视 ②指令=卡片同源 ③满仓主义 ④宁缺毋滥 ⑤≤5 持仓先卖后买 ⑥单因子依赖橙徽章 ⑦L0 门控 403 ⑧数据覆盖率分年核查 ⑨写保护免疫（时间戳文件名+glob 最新）⑩验收五步+T+1 组合层 ⑪因子池脚本只读、裁决投递留言 ⑫压力测试每周巡检。

## 三、数据边界（不可违反）

- **turn 换手率 2019 前缺失（2011-2018 覆盖率 0%）——2019 前换手类结论一律作废**；hist_mv 仅 2020+
- amount 千元/元跨源已统一（tushare×1000）；qfq 2010-2026 全量可回测
- T+1：信号日收盘→次日开盘执行；IC 类分析为学术惯例（同口径保留）
- 回测范围 ≠ 因子有效范围（分年度 + 覆盖率分年核查 + 至少覆盖一次 2018 级熊市）
- 行情数据禁再分发（开源只含代码+获取脚本+合成 demo）

## 四、关键实证结论（可引用）

- **turn_low 40日 top20（2019+）= 唯一干净防守 alpha**：+15.95% / -8.7% / 1.11；择时对 turn_low 纯损害（作废"择时=保险"）
- **pitch = 雷达非引擎**：高分集中持仓 +7.04%/**-60.2%**（ICIR 加权）、等权 +16.67%/**-41.7%** → 主力 turn_low 分散 + pitch 高分仅卫星 2-3 只带止损
- 17 年回测胜率：quality_gap 70.4% / value 62.5% / revalue 53.6% / pv_consensus 53.3% / breakout 45.5% / reversal 39% 负期望
- 全库最强因子：limup_ex_5 1.239 / limup_ex_ret_20 1.099 / open_prem_20 0.958 / lhb_jg_cnt_20 0.900 / turn_mid_prox 0.867 / bp 120日 0.685 / f_score 0.490 / sue 0.463
- 止损分类型：breakout 10% 保留；reversal ATR3×；pv_consensus/quality_gap 删除硬止损
- 已证伪（备查）：热度板块/冷门×低换手/散户情绪择时/MA20 择时/全历史 turn_low(NaN 污染)/五福直接交易/处置效应/尾盘诱多/放量回升/真动量/池内反转增强/IND 行业层/涨停次日高开诱多(转 FS-14)/GTJA191 全量/Qlib alpha158+360+LSTM/GRU/LightGBM/vwap_dev/社保加仓(120日 ICIR -1.107 反向→温度计用)/高管增减持/北向(无数据)/业绩预告/筹码集中度

## 五、GitHub 维护手册（核心职责）

### 5.1 仓库现状
| 项 | 值 |
|---|---|
| 仓库 | https://github.com/yuanwang589-dev/deepseek-harness-quant（PUBLIC · MIT · master） |
| 本地源 | `D:\deepseek-harness-quant`（33,856 文件 / 430MB；代码 379 文件 4.6MB 入 git） |
| 版本 | VERSION=1.0.9（CHANGELOG 记 v1.0.0，命名不一致沿用） |
| 发布产物 | `D:\quant-release\`（LWQuant-v{ver}-Release.zip 完整包 / Source zip / Windows-Full zip） |
| EXE | `dist\QuantDeck.exe`（PyInstaller onefile，~300MB；顶层 QuantDeck.exe 为运行中旧版勿覆盖） |
| 账号/CLI | yuanwang589-dev；gh=`C:\Program Files\GitHub CLI\gh.exe`（PATH 可能未刷新，用全路径） |

### 5.2 发布流程（新版本）

**发布源铁律**：`D:\deepseek-harness-quant` = **独立发布快照，不从主系统同步**。主系统 `Desktop\劳威派量化\DSHQuant` = 开发版（带 dev-badge 测试标记，仅本地）。发布内容一律在 D 盘快照上整理。

1. **删除测试版标记**（必做）：发布版不得带任何"测试/开发版"标记——搜 `beta-mark`、`dev-badge`、`开发版`、`DEV 开发版`、`build_mode` 并删除（β/beta 小标、DEV 徽章都属于测试标记）。主系统 dev 标记靠 `/api/build_mode` 控制显示，D 盘旧版是 β 标记，二者都要确保发布版清零
2. **敏感扫描**（必做）：全库 regex 扫 `duckdns|datahubco`、`sk-[A-Za-z0-9]{16,}`、`C:[/\\]Users[/\\]12985`、`D:[/\\].*劳威派`、`\b[0-9a-fA-F]{32}\b`——注意 `scripts/build_update.py` 中的 duckdns 是脱敏替换规则（安全，勿误报）；排除 node_modules/updates/backups
   - **网盘/第三方链接与提取码零容忍**（历史事故 2026-08-16：`docs/分钟数据接入说明.md` 的 5 个百度网盘提取码被推到公开仓库 = 数据泄露）：任何文档/代码里不得出现网盘链接、提取码、私有下载 URL。数据获取只写"私有网盘/见本地配置/自行获取"，提取码只存在本机不公开处。发布前专门搜 `提取码|pan\.baidu|夸克|网盘.*[0-9a-z]{4}`
3. **更新 VERSION + CHANGELOG**（UTF-8）
4. **重打包 EXE**：`DSHQuant\.venv\Scripts\python.exe scripts\build_exe.py`（产物 dist\QuantDeck.exe；exit 1 可能是管道截断假象，以 "Build complete!" 为准）
5. **打包 Release zip**：`python scripts\build_release.py --version X.Y.Z`（→ D:\quant-release，产物名 `DSHQuant-v{ver}-Release.zip`；不含 zip/exe，EXE 单独附件）
6. **git 提交推送**：`git -C D:\deepseek-harness-quant add -A && git commit && git push`
   - **commit message 规范（用户定调，GitHub 内容面向最终用户）**：commit message 是**对外发布说明**，只写这个版本/提交给用户带来了什么，**不暴露内部改动动作**——禁止出现 `rebrand`/`切换品牌`/`重构 README`/`去除欧奈尔`/`修复 gitignore`/`删除测试标记`/`chore:`/`docs:` 前缀/`—` 破折号/括号长描述/emoji。例：`DeepSeek HARNESS Quant v1.0.8 开源发布`、`更新项目文档`。历史保持干净的单条发布说明为佳（内部多轮改动 squash/amend 成一条对外 commit）
7. **创建 Release**：`gh release create vX.Y.Z <exe> <zip> --title ... --notes ...`（附件上限 2GB）；覆盖旧版本时先 `gh release delete --yes` + 删旧 tag 再重建
8. 更新本 SKILL 的版本/仓库状态 + 归档完成记录

### 5.3 更新机制（scripts/update.py）
- manifest 驱动覆盖；用户配置/数据保护（config/params.yaml、data/ 不入覆盖）；应用前自动备份（backups/）
- updates/ 目录存 update_1.0.x.zip（历史）；`.gitignore` 排除 updates/backups
- 构建更新包：`scripts/build_update.py`（含脱敏替换规则）

### 5.4 gitignore 铁律（勿破坏）
- 必须排除：`config/params.yaml`、`harness/home/.credentials.yaml`、`harness/node_modules/`、`harness/home/profiles/web/node_modules/`、`harness/home/profiles/node_modules/`（历史踩坑：漏排除曾暂存 2 万+ 文件）、`data/cache/ data/factorpool/ *.db *.sqlite *.parquet *.xlsx`、`logs/ output/ report/`、`deck/dashboard_*_20*.html deck/*.json`、`build/ dist/ *.spec updates/ backups/ /QuantDeck.exe`（>100MB 走 Release 附件）
- 提交前检查：`git ls-files | wc` 应 ≈379；单文件 >50MB 必是漏配

### 5.5 常见操作
- 改私有：`gh repo edit yuanwang589-dev/deepseek-harness-quant --visibility private`
- 推送：git push（gh 已 setup-git，凭据自动）
- 授权失败排查：电脑浏览器（已登录）打开 https://github.com/login/device 输入设备码；手机 2FA 收不到不影响（见 5.6）
- EXE 顶层被占用：运行中的 deck 即旧 EXE 实例，勿强制覆盖，新版放 dist/

### 5.6 历史坑（勿重蹈）
1. gitignore 漏排除 profiles/node_modules → 2 万+ 文件暂存（修法：reset + 补规则 + 重新 add）
2. winget 装 gh 后 PATH 不刷新 → 全路径调用
3. 设备码授权手机收不到 2FA → 用电脑浏览器已登录会话授权（免验证码）
4. Release 附件 466MB 上传慢 → 后台任务执行

## 六、全量文件索引（记忆入口）

| 想找 | 文件 |
|---|---|
| **核心优势总结（对外口径）** | `09_总结归档/核心优势总结_20260816.md`（六大优势：自然语言控制/可魔改/自动挖因子/全模块化/API 化/动态更新） |
| **记忆恢复（先读）** | `09_总结归档/记忆恢复_主系统架构师_20260816.md` |
| **GitHub 发布完成记录** | `09_总结归档/GitHub开源发布完成记录_20260816.md` |
| 任务唯一来源 | `00_总指导任务发布.md` |
| 统一状态源 | `09_总结归档/任务执行状态总表.md` |
| 系统总览（一页） | `09_总结归档/系统状态总览_20260815.md` |
| 周一执行（08-17 开盘） | `11_设计书/周一仓位分配方案_20260817.md` + `周一开盘前检查清单_20260817.md` + `周一候选卡预检_20260817.md` |
| 资产与规则固化 | `09_总结归档/主系统审计员_资产与规则固化_20260815.md` |
| 未落地项目 | `09_总结归档/未落地项目总清单_20260815.md` |
| 12 小时完成总结 | `09_总结归档/完成总结_20260815_自主推进轮.md`（目标轮 1-31） |
| 开源资产盘点 | `09_总结归档/开源资产盘点_20260816.md` |
| HARNESS 嵌入记录 | `09_总结归档/开源包升级_HARNESS深度嵌入_20260816.md` |
| 动态化铁律 | `09_总结归档/动态化铁律与审计_20260816.md` + `AGENTS.md` |
| ETF 映射/主观多池 | `09_总结归档/成果消化与ETF映射模块_20260815.md` + `主观多池远期体系_20260815.md` |
| 因子池全部结论 | `因子池/output/combo_reports/报告索引_20260815.md`（123+ 份） |
| 下轮挖掘地图 | `因子池/output/combo_reports/下轮挖掘数据源地图_20260816.md`（P0=大宗/研报） |
| 入池流程/回测图归档 | `11_设计书/入池流程规范化与回测图归档标准_20260815.md` |
| 回测口径 | `.dsh/skills/backtest-acceptance/SKILL.md` |
| 因子挖掘 | `.dsh/skills/factor-mining-workflow` + `.dsh/skills/alpha-gpt-factor-mining` |
| 牛散档案 | `.dsh/skills/niu-san-*`（7 份）+ `niu-san-distillation` |
| 协作留言 | `AI协作/`（主系统视角）+ `因子池/AI协作/`（因子池视角） |
| UI 设计 | `07_UI设计/`（v4.0 规格）+ `08_UI文档/` |
| 复刻策略 | `04_复刻策略代码/README_索引.md`（12 策略） |
| 复现指导 | `05_复现指导/因子选股池复现总指导.md` |

## 七、当前状态（2026-08-16 周日）

- 数据日 08-14（5818 只）；择时 full / 67.6 分；持仓 5/5（万辰 NEAR 建议卖）+ 巨化超限
- **周一 08-17 执行**：主力 turn_low top20（75%）+ 卫星 pitch≥90 2-3 只（25%）；17 张卡待审批；先卖后买；08-10 批 25 只 T+5 复核到期
- 等用户拍板 3 件事：资金规模 / 双轨结构 / 持仓去留
- 因子池待办 4 项：daily_scores 回溯 / turn 重生成 / 分钟因子落列 / 证伪标注
- 周一 19:15 FactorDaily 自动全量覆盖 turn（自愈），周二起决策链 turn 100%

*维护：GitHub 维护专员 · 2026-08-16 ｜ 配套：记忆恢复 + 发布完成记录（09_总结归档/）*
