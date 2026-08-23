# DSHQuant · Codex 交接对齐文档

> 版本：v1.0 ｜ 生成：2026-08-23 ｜ 供 Codex / 其他协作代理快速对齐本量化系统
> 阅读顺序：本文 → `AGENTS.md`（铁律）→ `config/params.yaml`（全参数）→ `README.md`
> 协作原则：**所有结论必须本地实证，禁止引用 README 外包声称作为事实**（外包数据不存在于本仓库）。

---

## 0. 一句话定位

自然语言驱动的 A 股低频量化系统：**AI 不预测个股**；确定性 Python 引擎（评分/回测/风控/扫描）+ 数据裁决（PIT/T+1/可证伪）。主策略 = 等权 + Regime 择时 + 硬过滤（实测夏普 1.07，系统自己的实证结论）。

---

## 1. 环境与快速验证

| 项 | 值 |
|---|---|
| 工作区 | `/Users/asaphliang/Documents/Codex/projects/github.com/Asaph-L/deepseek-harness-quant` |
| Python | `.venv/bin/python`（3.12.13，pandas 3.0.5 / numpy 2.5.2） |
| 服务 | 量化门户 deck **:8787**（http.server 单文件）｜ HARNESS :3080 |
| 调度 | macOS launchd 9 任务（`scripts/setup_launchd.py` 管理） |
| 数据 | `data/cache/`（SQLite）+ `data/real/bars.db`（主库 1.7GB，symlink） |
| Git | origin=Asaph-L/deepseek-harness-quant；身份 Asaph-L <liangjunshen0@gmail.com>；**网络到 GitHub 不稳定，push 需重试** |

```bash
# 必会验证命令（改动后必跑）
.venv/bin/python data/health_check.py        # 系统巡检（链时效/任务/DB）
curl -s http://127.0.0.1:8787/api/system_live | python -m json.tool
.venv/bin/python -m py_compile <改动的文件>   # 语法
# 全套因子实证（~3 分钟，含面板缓存）
.venv/bin/python scripts/evaluate_all_factors.py
```

---

## 2. 架构地图（模块 → 职责 → 关键文件）

| 层 | 模块 | 职责 | 关键文件 |
|---|---|---|---|
| 数据 | `data/` | 唯一读取接口 DailyCache + 各数据源拉取 | `cache.py`（唯一读口/双库合并）、`fetcher_tushare.py`、`fetcher_baostock.py`、`incremental_daily_tushare.py`（日线增量）、`backfill_daily_tushare.py`、`fetcher_lhb.py`（龙虎榜）、`fetcher_shebao.py`（社保）、`fetcher_gdhs.py`（股东户数）、`fetcher_minute.py`（分钟线·限频）、`build_finance_ts.py`（财报时序） |
| 因子 | `factors/` | 因子计算/挖掘/择时/机会扫描 | **`alpha_panel.py`（37 因子引擎+缓存）**、`alphagpt/`（公式挖掘：vocab/vm/generator/miner）、`pool/`（生命周期 registry/lifecycle）、`policy/`（timing_system 择时）、`opportunities/`（scan/pitch_v2/tech_pitch）、`minute_factors.py`（分钟因子·数据受限）、`signal_family.py`（120+ 因子名→信号族映射） |
| 策略 | `strategy/` | 决策链 + 组合 | `equal_weight_timing.py`（主策略 v3：等权+Regime+硬过滤）、`ranking_v2.py`（精选排名）、`pool_layers.py`（三层池）、`paper_tracker.py`（模拟盘）、`base_pool.py` |
| 风控 | `risk/` | 七道风控 | `data_audit.py`、`stop_monitor.py`、`position_stop_check.py`、`beneish.py`、`stock_risk.py` |
| 回测 | `backtest/` | T+1 回测 + 归档 | `bt_runner.py`、`bt_engine.py`、`backtest_all_factors.py`（14 因子回测）、`bt_report.py`（归档） |
| 展示 | `deck/` `ui_v2/` | 门户 + 前端 | `deck_server.py`（:8787 路由）、`live_api.py`（聚合 API）、`system_live.py`（实时状态）、`ui_v2/pages/*.html`（9 页，前端零硬编码） |
| 调度 | 根目录 | 巡检/日报/守护 | `dev_auto.py`（每 4h）、`data/daily_pipeline.py`（18:30 全链）、`data/after_close_scan.py`（17:35）、`data/daily_report_auto.py`（20:00 日报）、`deck/ensure_deck.py`（守护） |
| 技能 | `assets/skills/` | 方法论（Codex 开工前必读相关 skill） | factor-mining-workflow（九步入池）、backtest-acceptance（回测口径）、github-maintainer、alpha-gpt-factor-mining、niu-san-* |

---

## 3. 铁律（不可违反，详见 AGENTS.md）

1. **全动态化**：数据/列表/阈值一律配置或数据库驱动，禁止 HTML/JS/Python 硬编码业务数据；新增配置同时提供 `.example` 模板。
2. **数据边界**：行情来自第三方（Tushare 等），禁止再分发；只开源代码+获取脚本+合成演示数据。
3. **换手率 2019 前缺失** → 2019 前换手类结论一律作废（因子实证起点 2019-01-01）。
4. **回测口径**：T+1 开盘执行 / 一字板过滤 / 成本模型（万3+万1.3+印花税+滑点 0.4%/期近似）/ 结论分级——见 `assets/skills/backtest-acceptance/SKILL.md`。
5. **PIT 纪律**：财报只用 ann_date 已披露数据，禁止 look-ahead（市值用 hist_mv 历史口径，不用快照）。
6. **改动后回归**：页面 200 / API 200 / 语法检查 / health_check。
7. **AI 不预测个股**：L2 决策卡片聚合是唯一买入指令来源，无第二套推荐逻辑。

---

## 4. 已完成资产（2026-08-21 ~ 08-23，均已推送 origin）

### 4.1 平台迁移与数据修复（commit dce1d2d）
- Windows→macOS 全量迁移：schtasks→**launchd 9 任务**（`scripts/setup_launchd.py` + `scripts/launchd/*.plist` 模板）、netstat→lsof、wmic→pkill、`<home>` 占位符修复、health_check/health_scan/dev_auto/ensure_deck 跨平台。
- 日线补拉 08-18→08-21；沪深300 指数全量回补（Tushare 备源）；板块研究台 500 修复（industry 解析 + daily_close fallback + gdhs 降级）；主策略持仓恒 0 修复（市值映射改读 hist_mv.db）；日报 20:00 任务；`data/daily_signal.py` 重建（signal 链）。

### 4.2 第一优先：因子体系本地重建（commit dfbfc0b / 5d11d3c / 20c9290）
- **`factors/alpha_panel.py`：37 因子 12 族**（换手4/低波3/反转2/流动性1/彩票3/振幅2/涨跌停4/行业2/**机构行为5**/Alpha101×5/基本面5），本地 bars/finance/hist_mv 向量化，面板缓存 `output/alpha_panels/`（1.6GB parquet，`load_panels` 增量补算）。
- **`factors/alphagpt/`**：AlphaGPT 公式引擎 A 股蒸馏版（12 特征+16 算子词表 / numpy StackVM / 递归随机生成器+LLM 生成器 / ICIR 奖励挖掘）。用法：`python factors/alphagpt/miner.py --n 300`。
- **`scripts/evaluate_all_factors.py`**：全向量化 8 维体检（IC/ICIR/单调/多空/换手/衰减/时序/年度IC+评分卡）。实测 **29/36 ≥50 分弱有效以上**。

### 4.3 九步入池 + 数据源（commit 5150c05 / 8d8a496 / 5d63b4d）
- **`scripts/pool_candidates.py`**：候选九步入池链（P3 覆盖率/P4 ICIR+去重闸门/P6 组合层 T+1 市值中性/P7 holdout/P9 turn_low 正交/P10 归档）。**1 个因子已入池**：`agp_amihud_max3_abs_csrank`（组合层 +15.1pp、夏普 2.19、registry id=13 active 82 分）。
- 数据源：龙虎榜 `lhb.db`（16.2 万行）→ `lhb_cnt_20` 反向强有效 73.4（热度反指标）；社保 `shebao.db`（191 只）；股东户数 `gdhs_full.db`（5541 只 25.7 万行）；finance_ts 全市场补全（5788 只 17.8 万行）→ bp 73.5 分。
- 页面打通：`live_factor_dash`/`live_factor_ui_pack` 本地 fallback（36 因子+风控层+拥挤度+生命周期时序+36×36 相关矩阵）；`scripts/rebuild_factor_reports.py`（5 份报告重建）；`scripts/build_factor_corr.py`。
- 牛散 skill 完整版覆盖空模板（HARNESS 已重启生效）。

---

## 5. 数据资产清单（data/cache/）

| 库 | 内容 | 规模 | 维护方式 |
|---|---|---|---|
| bars.db（symlink→real/） | 日线 OHLCV+turn+amount（qfq/复权），2019-01-02 起 | 5784 只 / 881 万行 / 1.7GB | `incremental_daily_tushare.py`（17:30 任务） |
| finance.db | finance_report（ROE/单季同比，**口径=小数**） | 17.7 万行 | `build_finance_report.py` |
| finance_ts.db | financials_ts（equity/income，ann_date PIT） | 5788 只 / 17.8 万行 | `build_finance_ts.py` |
| hist_mv.db | 月度流通市值（**亿元**，code 带后缀） | 39.3 万行 | `fetcher_hist_mv.py` |
| lhb.db | 龙虎榜 top_list+top_inst | 10.4 万 + 86.4 万行 | `fetcher_lhb.py`（2020 起已全） |
| shebao.db | 社保组合持仓（top10_holders 过滤） | 191 只 / 243 行（单期） | `fetcher_shebao.py`（多期增量待跑） |
| gdhs_full.db | 股东户数 + chg_pct（2020 起多期） | 5541 只 / 25.7 万行 | `fetcher_gdhs.py` |
| minute.db | 5min 线 | **空（限频，见 §7）** | `fetcher_minute.py` |
| stock_basic.db | 代码/名称/行业（纯中文 110 类）/上市退市 | 5883 只 | `fetch_stock_basic.py` |

**单位约定**：tushare amount=千元 / volume=手 / hist_mv circ_mv=亿元 / finance ROE=小数（0.09=9%）/ turn=%。

---

## 6. 因子实证结果亮点（本地口径，2020-2025，报告 `report/因子评估报告_全量.md`）

| 组 | 因子（评分） | 方向 |
|---|---|---|
| 强有效 | sue 95.1 / accruals 92.6 / amihud 90.8 / o2c_sum_20 80.5 / open_prem_20 76.2 / alpha003/006/015 71-78 / bp 73.5 | sue/accruals/amihud/o2c/open_prem 正向；bp 反向（低 PB 好） |
| 反向强有效 | turn_mean20 73.5 / turn_mid_prox 72.4 / turn_std20 71.9 / lowvol_60 71.8 / limit_up_cnt_20 74.4 / lhb_cnt_20 73.4 | 换手/波动/涨停热度/龙虎榜上榜 = 后续跑输（A 股反转市） |
| 弱有效 | fscore 69 / max_ret20 68 / skew20 62 / reversal20 60 / lhb_jg_cnt_20 58 / roe 56 / 涨跌停族 | — |
| 无效/边缘 | gdhs_chg_pct 24（披露稀疏）/ shebao 32（单期）/ ind_* 41-44 / alpha050 41 / bp 边缘旧口径 | 记录在案，勿用于选股 |

**入池因子**：`agp_amihud_max3_abs_csrank`（AlphaGPT 挖掘，组合层 T+1 市值中性 +15.1pp/年、夏普 2.19、holdout 保持、turn_low 正交 0.17）——**尚未接入策略权重，是下一步核心工作**。

---

## 7. 已知问题与受限项（诚实清单）

1. **分钟因子族受限**：tushare stk_mins 在 5000 积分档**限频 1 次/分钟**（今日额度已耗尽）；全量分钟线不可行（5783 只=数天）。`fetcher_minute.py`/`minute_factors.py` 代码就绪，需更高积分（8000+）或第三方分钟源后启用。
2. **gdhs_chg_pct 因子实证无效**（23.9 分）：股东户数披露稀疏（每季），横截面预测力不足。数据在，口径可再探索（如月度变化/分市值）。
3. **社保因子单期无效**：`shebao.db` 只拉了 2026-06-30 一期，hold_change 无历史对比；需多期增量（`fetcher_shebao.py --period`）后重评。
4. **finance_report 无 ann_date**：PIT 用 period 月末近似（精度限制）；精确 PIT 走 finance_ts。
5. **report/dashboard.py 缺失**（原作者外包）：dev_auto 调用有 try 兜底，仅日志报错不致命。
6. **外包因子池 `data/factorpool/` 不存在**：所有消费点已本地 fallback（factor_dash/ui_pack/live_factors）；发现新消费点需同样处理。
7. **`待办队列.md`** 为 dev_auto 运行产物（未跟踪，勿提交）。
8. **网络到 GitHub 不稳定**：push 间歇失败（Authentication failed 误报/超时），重试即可；token 用完即撤销。
9. **bars.db 少量混源行**（2026-08-20 有 1 行 baostock）：amihud 等按 amount 的因子在 2019+ 主库基本无污染（99.99% tushare）。
10. **launchd 任务**：`com.lwquant.dailyreport` 等 9 个已加载；改任务用 `scripts/setup_launchd.py`（--uninstall/--status），勿手工乱改 plist。

---

## 8. 下一步路线图（建议分工）

| 优先级 | 任务 | 说明 | 适合谁 |
|---|---|---|---|
| P0 | **入池因子接入策略权重** | `config/params.yaml` factor_pool/weights：把 agp_amihud 与实证强因子（sue/o2c/open_prem/低波）接入 equal_weight_timing 或 ranking_v2 打分；回测验证（bt_runner） | 协作核心 |
| P0 | **数据保鲜闭环** | 龙虎榜/股东户数/社保加入每日增量（launchd 任务或 daily_pipeline 步骤） | 确定性工程（Codex 友好） |
| P1 | **策略组合层验证** | 等权+Regime 主策略 vs 因子增强组合：T+1 回测、分年度、成本敏感性 | 协作 |
| P1 | **社保多期重评** | fetcher_shebao 增量拉 4 期 → 重跑 evaluate → shebao_chg 重新入评 | 批量工程 |
| P2 | **分钟因子** | 需先解决数据源（更高积分/第三方），再全量拉取+实证 | 受限待定 |
| P2 | **因子页面剩余 Tab** | 回测档案 Tab 等（外包数据 fallback 补齐） | 前端工程 |
| P2 | **牛散蒸馏深化** | niu-san skill 调研证据挂载（assets/skills v1.1 已有部分） | 研究型 |

---

## 9. 协作规范

1. **分工原则**：批量/确定性/数据工程（拉数据、跑实证、写测试、修 bug）→ Codex 可独立完成；涉及决策链/风控阈值/策略口径的改动 → 主代理确认（AGENTS.md：低频主观量化定位，改阈值=改投资行为）。
2. **改动提交**：`git -c user.name="Asaph-L" -c user.email="liangjunshen0@gmail.com" commit`；中文提交信息，按主题分提交；`待办队列.md`/`logs/`/`output/`/`report/` 不入库（gitignore 已配）。
3. **回归门槛**：任何改动 → py_compile + health_check + 相关 API 200。
4. **回测口径**：一律走 `assets/skills/backtest-acceptance/SKILL.md`（T+1/一字板/成本/结论分级），新结论写 `output/backtest_archive/`。
5. **并发安全**：不要并发写 bars.db（主库写锁）；launchd 任务已在跑时不要手动重复启动同一任务（有锁机制但以 launchd 为准）；deck_server 改动后重启：`kill $(lsof -ti tcp:8787)` 再 `nohup .venv/bin/python -u deck/deck_server.py &`。
6. **外包数据红线**：任何"因子池/60 因子/ICIR 0.9x"等 README 数值，必须本地重跑实证后才可用；本地实证入口 = `scripts/evaluate_all_factors.py`。
7. **与主代理协作**：Codex 产出 → 主代理验收（实证数据/口径/回归）；大任务先出计划再动手。

---

## 10. 常用命令速查

```bash
# 数据
python data/incremental_daily_tushare.py          # 日线增量（幂等）
python data/fetcher_lhb.py --days 30              # 龙虎榜增量
python data/fetcher_gdhs.py                       # 股东户数增量（断点续传）
python data/build_finance_ts.py --only-missing    # 财报时序补缺
# 因子
python scripts/evaluate_all_factors.py            # 全因子实证（37 因子，~3 分钟）
python factors/alphagpt/miner.py --n 300 --top 20 # AlphaGPT 挖掘
python scripts/pool_candidates.py --top 10        # 候选九步入池
python scripts/rebuild_factor_reports.py          # 5 份报告
python scripts/build_factor_corr.py               # 相关矩阵/生命周期
# 策略/回测
python strategy/equal_weight_timing.py            # 主策略持仓
python backtest/backtest_all_factors.py           # 14 因子回测
python strategy/ranking_v2.py --n 30              # 精选排名
# 调度/运维
python scripts/setup_launchd.py --status          # launchd 状态
python data/health_check.py                       # 系统巡检
python dev_auto.py --status                       # 巡检/熔断状态
```

> **最后提醒**：本系统的价值主张是"用自己的数据重新验证一切"（外包数据缺失是常态）。Codex 的第一课：任何看起来"已经实证"的结论，先跑 `evaluate_all_factors.py` 或对应验证脚本确认，再引用。
