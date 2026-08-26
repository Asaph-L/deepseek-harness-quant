# DSHQuant · 项目交接对齐文档

> **2026-08-24 Codex 修订提示**：本文第 6 节的旧分数/收益、第 9 节优先级和
> 第 11 节部分抓取命令属于上一轮历史快照，不能作为当前准入证据或操作手册。
> 当前唯一执行基线见 [`因子接入策略与每日增量验收.md`](因子接入策略与每日增量验收.md)，
> 当前代码回归入口为 `python -B scripts/quick_regression.py`。新的 canonical QFQ、
> PIT 面板、T+1 正式回测和 evaluator archive 完成并相互绑定前，旧因子结论全部
> research-only。

> 版本：v2.0 ｜ 更新：2026-08-23 ｜ 本文件只陈述事实（现状/资产/规则/踩坑），不含任务分工。
> 阅读顺序：本文 → `AGENTS.md`（铁律）→ `config/params.yaml`（全参数）→ `README.md`
> 事实基线：**README 中的外包声称数值（ICIR 0.9x、123+ 因子等）不存在于本仓库**；一切结论以本地实证脚本输出为准。

---

## 1. 环境与验证命令

| 项 | 事实 |
|---|---|
| 工作区 | 仓库根目录（下文命令均从此处执行） |
| Python | `.venv/bin/python`（3.12.13，pandas 3.0.5 / numpy 2.5.2） |
| 服务 | 量化门户 deck **:8787**（http.server 单进程）｜ HARNESS :3080 |
| 调度 | macOS launchd 配置驱动任务（当前 3 个，由 `scripts/setup_launchd.py` 管理） |
| 数据 | `data/cache/`（SQLite）+ `data/real/bars.db`（主库 1.7GB，cache/bars.db 是 symlink） |
| Git | origin=Asaph-L/deepseek-harness-quant；提交身份 Asaph-L \<liangjunshen0@gmail.com\>；GitHub 网络间歇不稳定（push 需重试） |

```bash
.venv/bin/python data/health_check.py        # 系统巡检
curl -s http://127.0.0.1:8787/api/system_live | python -m json.tool
.venv/bin/python -m py_compile <改动文件>
.venv/bin/python scripts/evaluate_all_factors.py   # 全套因子实证（含面板缓存，~3 分钟）
```

## 2. 架构地图（模块 → 职责 → 关键文件）

| 层 | 模块 | 职责 | 关键文件 |
|---|---|---|---|
| 数据 | `data/` | 唯一读取接口 + 各数据源拉取 | `cache.py`（唯一读口/双库合并）、`fetcher_tushare.py`、`fetcher_baostock.py`、`incremental_daily_tushare.py`、`backfill_daily_tushare.py`、`fetcher_lhb.py`（龙虎榜）、`fetcher_shebao.py`（社保）、`fetcher_gdhs.py`（股东户数）、`fetcher_minute.py`（分钟线·限频）、`build_finance_ts.py` |
| 因子 | `factors/` | 因子计算/挖掘/择时/机会扫描 | **`alpha_panel.py`（37 因子引擎+缓存）**、`alphagpt/`（vocab/vm/generator/miner）、`pool/`（registry/lifecycle）、`policy/timing_system.py`、`opportunities/`（scan/pitch_v2/tech_pitch）、`minute_factors.py`、`signal_family.py`（120+ 因子名→信号族映射表） |
| 策略 | `strategy/` | 决策链 + 组合 | `equal_weight_timing.py`（主策略 v3：等权+Regime+硬过滤）、`ranking_v2.py`、`pool_layers.py`、`paper_tracker.py`、`base_pool.py` |
| 风控 | `risk/` | 七道风控 | `data_audit.py`、`stop_monitor.py`、`position_stop_check.py`、`beneish.py`、`stock_risk.py` |
| 回测 | `backtest/` | T+1 回测 + 归档 | `bt_runner.py`、`bt_engine.py`、`backtest_all_factors.py`、`bt_report.py` |
| 展示 | `deck/` `ui_v2/` | 门户 + 前端 | `deck_server.py`、`live_api.py`、`system_live.py`、`ui_v2/pages/*.html` |
| 调度 | 根目录 | 巡检/日报/守护 | `dev_auto.py`（4h）、`data/daily_pipeline.py`（18:30）、`data/after_close_scan.py`（17:35）、`data/daily_report_auto.py`（20:00）、`deck/ensure_deck.py` |
| 技能 | `assets/skills/` | 方法论 | factor-mining-workflow、backtest-acceptance、github-maintainer、alpha-gpt-factor-mining、niu-san-*（14 个） |

## 3. 铁律（规则本身，详见 AGENTS.md）

1. 全动态化：数据/列表/阈值一律配置或数据库驱动；前端禁止硬编码业务数据；新增配置同时提供 `.example` 模板。
2. 行情数据来自第三方（Tushare 等），禁止再分发；仓库只含获取脚本+合成演示数据。
3. 换手率 2019 前缺失 → 2019 前换手类结论作废；因子实证起点 2019-01-01。
4. 回测口径：T+1 开盘执行 / 一字板过滤 / 成本模型（万3+万1.3+印花税+滑点，0.4%/期近似）——见 `assets/skills/backtest-acceptance/SKILL.md`。
5. PIT 纪律：财报只用 ann_date 已披露数据；市值用 hist_mv 历史口径（非快照）。
6. 改动后回归：py_compile + health_check + 相关 API 200。
7. AI 不预测个股；L2 决策卡片聚合是唯一买入指令来源。

## 4. 已完成资产（2026-08-21~23，已推送 origin，commit 见 git log）

- **平台迁移（dce1d2d）**：schtasks→launchd 9 任务（`scripts/setup_launchd.py`+plist 模板）；netstat→lsof、wmic→pkill、`<home>` 占位符修复；health_check/health_scan/dev_auto/ensure_deck 跨平台；日线补拉 08-18→08-21；沪深300 指数全量回补（Tushare 备源）；板块研究台 500 修复；主策略持仓恒 0 修复（市值映射改读 hist_mv.db）；日报 20:00 任务；`data/daily_signal.py` 重建。
- **第一优先（dfbfc0b/5d11d3c/20c9290）**：`factors/alpha_panel.py`（37 因子 12 族，面板缓存 `output/alpha_panels/`）；`factors/alphagpt/`（12 特征+16 算子/StackVM/随机+LLM 生成器/ICIR 奖励挖掘）；`scripts/evaluate_all_factors.py`（8 维体检+年度 IC）。
- **九步入池+数据源（5150c05/8d8a496/5d63b4d）**：`scripts/pool_candidates.py`（P3-P10 验证链）；1 因子入池 `agp_amihud_max3_abs_csrank`（registry id=13）；龙虎榜/社保/股东户数/finance_ts 数据源；`live_factor_dash`/`live_factor_ui_pack` 本地 fallback；`scripts/rebuild_factor_reports.py`（5 报告）；`scripts/build_factor_corr.py`（36×36 相关矩阵）；牛散 skill 完整版覆盖空模板。

## 5. 数据资产清单（data/cache/）

| 库 | 内容 | 规模 | 拉取脚本 |
|---|---|---|---|
| bars.db | 日线 OHLCV+turn+amount，2019-01-02 起 | 5784 只/881 万行 | `incremental_daily_tushare.py` |
| finance.db | finance_report（ROE/单季同比，小数口径） | 17.7 万行 | `build_finance_report.py` |
| finance_ts.db | financials_ts（equity/income，ann_date PIT） | 5788 只/17.8 万行 | `build_finance_ts.py` |
| hist_mv.db | 月度流通市值（亿元，code 带后缀） | 39.3 万行 | `fetcher_hist_mv.py` |
| lhb.db | 龙虎榜 top_list+top_inst 遗留数据；新 coverage 尚未查询，完整性待合同复核 | 10.4 万+86.4 万行 | `fetcher_lhb.py`（不得据此声称 2020 起全量） |
| shebao.db | 社保组合持仓（单期 2026-06-30） | 191 只/243 行 | `fetcher_shebao.py` |
| gdhs_full.db | 股东户数+chg_pct（2020 起多期） | 5541 只/25.7 万行 | `fetcher_gdhs.py` |
| minute.db | 5min 线 | 空（限频，见 §7） | `fetcher_minute.py` |
| stock_basic.db | 代码/名称/行业（中文 110 类） | 5883 只 | `fetch_stock_basic.py` |

单位约定：tushare amount=千元/volume=手；hist_mv circ_mv=亿元；finance ROE=小数（0.09=9%）；turn=%。

## 6. 因子实证结果（本地口径，2020-2025，报告 `report/因子评估报告_全量.md`）

- 强有效（≥70 分）：sue 95.1 / accruals 92.6 / amihud 90.8 / o2c_sum_20 80.5 / open_prem_20 76.2 / limit_up_cnt_20 74.4（反）/ turn_mean20 73.5（反）/ turn_mid_prox 72.4（反）/ turn_std20 71.9（反）/ lowvol_60 71.8（反）/ lhb_cnt_20 73.4（反）/ bp 73.5（反）/ alpha003 75.2 / alpha006 78.4 / alpha015 71.9
- 弱有效（50-69）：fscore 69 / max_ret20 68 / skew20 62 / reversal20 60 / lhb_jg_cnt_20 58 / roe 56 / amp20 69 / 涨跌停族 / asset_growth 68（反）/ consec_limit_* 51-55 / downside_vol 51
- 无效/边缘（<50）：gdhs_chg_pct 24（披露稀疏）/ shebao_hold·chg 32（单期数据）/ ind_crowd_60 44 / ind_rs_20 41 / alpha050 41 / rmax 49
- **方向事实**：换手/波动/涨停热度/龙虎榜上榜 = 后续 20 日跑输（负 IC）；sue/accruals/amihud/o2c/open_prem = 正 IC。反向因子按 `DIRECTION` 反用。
- **入池因子**：`agp_amihud_max3_abs_csrank`（公式 AMIHUD MAX3 ABS CSRANK TSMEAN20 MAX3；组合层 T+1 市值中性年化超额 +15.1pp、夏普 2.19、holdout 保持、turn_low 相关 0.17）——**未接入策略权重**。
- AlphaGPT 挖掘候选 top（未入池）：AMIHUD CSRANK（ICIR +1.589 胜率 94%，与 amihud 相关 0.46 警示）、TURN RET60 SUB JUMP JUMP（ICIR -1.545）。

## 7. 已知问题与受限项（事实）

1. 分钟因子受限：tushare stk_mins 5000 积分档限频 1 次/分钟，全量分钟线不可行（5783 只=数天）；`fetcher_minute.py`/`minute_factors.py` 已就绪未全量。
2. gdhs_chg_pct 实证无效（23.9 分）：股东户数每季披露一次，截面稀疏。
3. 社保因子单期无效：shebao.db 只有 2026-06-30 一期，hold_change 无历史对比。
4. finance_report 无 ann_date → PIT 用 period 月末近似。
5. report/dashboard.py 缺失（原作者外包）：dev_auto 调用有 try 兜底，仅日志报错。
6. 外包因子池 `data/factorpool/` 不存在：消费点已本地 fallback（factor_dash/ui_pack/live_factors）。
7. `待办队列.md` 是 dev_auto 产物（未跟踪，勿提交）。
8. GitHub 网络不稳定：push 间歇失败/超时，重试可通；token 用完即撤销。
9. bars.db 有 1 行 baostock 混源（2026-08-20），按 amount 的因子需注意 source。
10. launchd 当前 3 个配置驱动任务已加载；改任务用 `scripts/setup_launchd.py`（--uninstall/--status）。

## 8. 踩坑速查（完整版见 `docs/PITFALLS.md`，50 项）

**数据/口径**：turn 2019 前缺失；amount 千元/元双轨（2019+ 主库 99.99% tushare）；finance_report 无 ann_date；sq_net_yoy ±10 截断；ROE 单季年化 vs 小数双口径差 4 倍；hist_mv 单位亿元且 drop_duplicates 会毁 merge_asof；top10_holders 的 holder_name 在列 3；stk_mins 限频 1 次/分钟且重试耗尽额度；trade_cal is_open 是 int；新浪快照 code 格式 sh600519、涨停需按板块上限算价；industry 纯中文不能 [:3] 切分。
**回测/评估**：组合层双重年化（×12 再 ×12 → +331% 假象）；市值中性缺失（Top10% 全微盘 → +283% 假象）；build_forward_returns/decay_curve 逐股查库卡死数小时（改面板 shift 向量化）；月末锚点字符串 vs Timestamp 键不匹配；VM nan_to_num 填 0 使覆盖率审计失真；2026 数据不足致 holdout 全失败；corr=nan 误判淘汰。
**挖掘**：random_formula while 恒假 → 只产单特征 → generate_batch 无限循环（曾空转 14 小时）；LLM 编造词表外 token（CSMIX）整条被拒；TS 算子逐日循环慢；并行任务 CPU 竞争使 18s 加载变 400s+（用 faulthandler 定位）。
**平台/运维**：schtasks/netstat/wmic 在 macOS 不存在且 health_check 无兜底直接崩；launchctl list 不显示 gui 域任务（用 print gui/uid/label）；launchd plist 参数拼接把 --sched 拼成路径；`<home>` 占位符未替换导致归档进工作区；job_kill 只杀 shell 管道 python 子进程残留；HARNESS 重启 EPERM（桌面版 profiles symlink，入口 lib/bin.js）；GitHub 网络用 GIT_TERMINAL_PROMPT=0 区分认证/网络。
**工程**：`(df.get("代码") or [])` 对 Series 触发 bool ambiguous；pandas 3.0.5 移除 rolling(axis=)、qcut 需 rank(method="first")；.gitignore 父目录忽略后无法 re-include 子文件；deck 改代码需重启进程。

## 9. 待办事项（事实清单：现状 + 依赖，不含分工）

| 优先级 | 事项 | 现状 | 依赖 |
|---|---|---|---|
| P0 | 入池因子接入策略权重 | `agp_amihud_max3_abs_csrank` 已入池未接入；`config/params.yaml` weights/factor_pool 未含它 | 需回测验证（bt_runner）后写入权重 |
| P0 | 龙虎榜/股东户数每日增量 | `fetcher_lhb.py`/`fetcher_gdhs.py` 支持断点续传，未挂 launchd/daily_pipeline | launchd 任务或 daily_pipeline 步骤 |
| P1 | 社保多期重评 | shebao.db 单期（2026-06-30），hold_change 无对比 | `fetcher_shebao.py --period` 增量拉 4 期 → 重跑 evaluate |
| P1 | 策略组合层验证 | 主策略等权+Regime（夏普 1.07）与因子增强组合未做对比回测 | 回测口径（backtest-acceptance skill） |
| P2 | 分钟因子 | `fetcher_minute.py`/`minute_factors.py` 就绪；数据空（限频） | 更高积分（8000+）或第三方分钟源 |
| P2 | 因子页面回测档案 Tab | 外包 backtest 档案缺失 | 本地回测产物归档（bt_report）接入 |
| P2 | 牛散 skill 深化 | assets/skills v1.0/v1.1 已挂载 | 调研证据（公开资料） |

## 10. 操作规范（事实）

1. 提交：`git -c user.name="Asaph-L" -c user.email="liangjunshen0@gmail.com" commit`；中文提交信息；`待办队列.md`/logs/output/report 不入库（gitignore 已配）。
2. 回归门槛：任何改动 → py_compile + health_check + 相关 API 200。
3. 回测结论一律按 `assets/skills/backtest-acceptance/SKILL.md` 口径，归档 `output/backtest_archive/`。
4. 并发：不并发写 bars.db（主库写锁）；launchd 任务已在跑时不手动重复启动；deck 重启：`kill $(lsof -ti tcp:8787)` → `nohup .venv/bin/python -u deck/deck_server.py &`。
5. 外包数据红线：README/外包声称的因子数值必须本地重跑实证后才可用；本地实证入口 = `scripts/evaluate_all_factors.py`。

## 11. 常用命令速查

```bash
# 数据
python data/incremental_daily_tushare.py        # 日线增量（幂等）
python data/fetcher_lhb.py --days 30            # 龙虎榜增量
python data/fetcher_gdhs.py                     # 股东户数增量（断点续传）
python data/build_finance_ts.py --only-missing  # 财报时序补缺
# 因子
python scripts/evaluate_all_factors.py          # 全因子实证（37 因子）
python factors/alphagpt/miner.py --n 300 --top 20
python scripts/pool_candidates.py --top 10
python scripts/rebuild_factor_reports.py        # 5 份报告
python scripts/build_factor_corr.py             # 相关矩阵/生命周期
# 策略/回测
python strategy/equal_weight_timing.py          # 主策略持仓
python backtest/backtest_all_factors.py         # 14 因子回测
python strategy/ranking_v2.py --n 30
# 调度/运维
python scripts/setup_launchd.py --status
python data/health_check.py
python dev_auto.py --status
```
