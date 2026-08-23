# DSHQuant · 踩坑记录（实测事实清单）

> 版本：v1.0 ｜ 2026-08-23 ｜ 全部为开发过程中实际发生并验证过的事实（现象→根因→修复）。
> 用途：避免重复踩坑；引用时以本清单为准，与 README 声称冲突时以实测为准。

---

## A. 数据与口径

| # | 事实 |
|---|---|
| A1 | **换手率 turn 2019 前缺失**（bars.db 2019-01-02 起才有 turn）。2019 前换手类因子/结论全部作废；因子实证起点统一 2019-01-01。 |
| A2 | **amount 单位按数据源不同**：tushare=千元，baostock=元。2019+ 主库 99.99% 是 tushare 源（实测 8,835,709/8,835,710 行），amihud 等按 amount 的因子无单位污染；但有 1 行 baostock 混入（2026-08-20），增量库混源时需按 source 过滤。 |
| A3 | **finance_report 无 ann_date 字段** → PIT 只能用 period 月末近似；精确 PIT（ann_date）走 finance_ts。 |
| A4 | **finance_report 的 sq_net_yoy 有 ±10 截断**（数据中大量 10.0/-10.0 为 clamp 值），非真实增速。 |
| A5 | **ROE/nyoy 口径曾双轨**：旧代码把 finance_report 的 roe 当单季按季度年化（Q1×4），新口径为年化小数（0.09=9%）；两套口径混用会差 4 倍，消费端必须确认版本。 |
| A6 | **finance_ts 只建了 300 只股票**（build_finance_ts 未跑全）→ bp/asset_growth 覆盖率 300/5784，实证代表性不足；已补拉至 5788 只（17.8 万行）。 |
| A7 | **hist_mv.circ_mv 单位=亿元**（×1e8 才是元）；且曾因 `drop_duplicates(code6)` 只留最新月 → merge_asof backward 早期月份全 NaN → bp 因子全空（修复：保留全部月份）。 |
| A8 | **top10_holders 字段顺序**：holder_name 在列索引 3（非 2）；取错列导致"社保 0 命中"假象（实测市值前 100 取对列后 10 次命中）。 |
| A9 | **tushare stk_mins（分钟线）5000 积分档限频 1 次/分钟**；失败重试会耗尽当日额度（实测 6 只×3 重试 406s 全失败、sleep 70s 后仍限频）→ 全量分钟线不可行。 |
| A10 | **tushare trade_cal 的 is_open 是 int 1 而非字符串 "1"**（`x[2]=="1"` 恒 False → 0 个交易日）。 |
| A11 | **新浪实时快照（akshare stock_zh_a_spot）**：代码格式 `sh600519/sz300750/bj920000`；涨停不能用 `chg>=9.8` 一刀切（ST 5%/创业板科创 20%/北交所 30%），需按昨收×板块上限算涨停价；"成交额"单位=元。 |
| A12 | **stock_basic.industry 是纯中文行业名（110 类）**，无"代码+名称"结构；`ind[:3]/ind[3:]` 切分会把"IT设备"切成 code="IT设"/name="备"。 |

## B. 回测与评估

| # | 事实 |
|---|---|
| B1 | **组合层双重年化**：单期超额×12 存入序列后又 ×12 年化 → 12 倍虚高（曾出现年化超额 +331%pp 假象）。修复：序列存单期值，汇总处 ×12 一次。 |
| B2 | **市值中性缺失**：Top10% 选股不按市值分层 → 全是微盘股 → 超额虚高（+283%pp）；修复：月末按市值 5 层内 z-score 后再取 Top10%。 |
| B3 | **build_forward_returns 逐股 get_daily**（5789 次库查询）→ 评估卡死数小时；修复：close 面板 `shift(-h)` 向量化。 |
| B4 | **decay_curve 每 horizon 调 build_forward_returns**（每因子 4×5789 次查询）→ 32 因子 74 万次查询；同样改为面板 shift 向量化。 |
| B5 | **月末锚点键类型**：month_ends 是字符串、面板索引是 Timestamp，`m in dpos` 恒 False → 组合层"样本不足"假象；修复：`pd.Timestamp(m)` 后再查。 |
| B6 | **StackVM 的 nan_to_num 填 0** → 因子面板无 NaN → 覆盖率审计失真（分年覆盖率恒 100% 假象），覆盖率审计对含算子的公式无区分力。 |
| B7 | **holdout 判定**：2026 年只有 8 个月数据，`len(hold)>=2` 恒失败 → 全部"holdout 不足"；修复：2026 不足时以 2025 为准。 |
| B8 | **去重闸门相关 nan**：常数面板 vs 锚点 corr 为 nan，`nan < 0.3` 全 False → 误判淘汰；修复：nan 视为"无锚点可比"。 |
| B9 | **停牌/新股 close NaN**：组合收益除法产生 NaN，必须 dropna 后 mean，否则 NaN 月份混入序列。 |

## C. AlphaGPT 与挖掘

| # | 事实 |
|---|---|
| C1 | **random_formula 死循环**：`while stack > 1` 初始 stack=1 恒假 → 只产 12 种单特征公式 → generate_batch 去重后 `len(seen)<n*30` 恒真 → 无限循环（一个 miner 任务空转 14 小时）。修复：递归后缀表达式生成器。 |
| C2 | **LLM 生成器编造词表外 token**（实测产出 "CSMIX" 等不存在特征）→ 整条被语法校验拒绝 → 0 条返回；修复：词表白名单过滤 + 多次请求。 |
| C3 | **TS 算子逐日 Python 循环**（1853 天×5789 列）→ 每公式数秒；修复：TSMEAN/TSSTD 用 pandas rolling，CSRANK 转置后 rank。 |
| C4 | **并行任务资源竞争**：单测 18s 的面板加载在评估任务（compute_all+评估）并行时 400s+ 超时 → 误判为"卡死"；实际是 CPU/内存竞争。排查手段：faulthandler.dump_traceback_later。 |
| C5 | **生成器输出与验证链 code 口径**：引擎面板列带后缀（000001.SZ），验证链需统一 `str.split('.').str[0]`。 |

## D. 平台与运维（Windows→macOS）

| # | 事实 |
|---|---|
| D1 | **schtasks / netstat -ano / wmic / taskkill 在 macOS 不存在**；health_check.py 曾因此 FileNotFoundError 直接崩溃（无 try 兜底）。 |
| D2 | **launchctl list 不显示 gui 域任务**；loaded 判定必须用 `launchctl print gui/<uid>/<label>`（rc==0）。 |
| D3 | **launchd plist 参数拼接**：`str(base / c)` 会把 `--sched` 拼成 `base/--sched`（只有首个参数是脚本路径）。 |
| D4 | **`<home>` 硬编码占位符**：原作者用 `<home>` 占 Windows 用户目录（发布脚本替换）；macOS 未替换 → logs_archive 归档到工作区字面量 `<home>/Desktop/垃圾桶` 目录。 |
| D5 | **job_kill 只杀 shell 管道**：`cmd | tail` 的 python 子进程可能残留；kill 后必须 pgrep 确认。 |
| D6 | **HARNESS 重启坑**：桌面版 home 的 profiles/node_modules/@deepseek-ai/dsh 是 symlink（指向桌面版 runtime），profile-boot unlink 时 EPERM → 内嵌 HARNESS 用 DSH_HOME=harness/home 启动可绕开。dsh 入口是 `lib/bin.js`（非 bin/dsh.js）。 |
| D7 | **GitHub 网络不稳定**：curl/API 通（200），git smart HTTP 间歇超时/误报 Authentication failed；区分认证 vs 网络用 `GIT_TERMINAL_PROMPT=0`；推送需重试循环。 |
| D8 | **launchd StartInterval 首次触发从 load 起算**；测试用 `launchctl kickstart gui/<uid>/<label>`。 |

## E. 工程与代码

| # | 事实 |
|---|---|
| E1 | **`(df.get("代码") or [])` 对 Series 触发 pandas `bool ambiguous`**（pandas 3.0 严格）；改 `df["代码"].tolist()`。 |
| E2 | **pandas 3.0.5 兼容**：`rolling(axis=)` 已移除；月频用 "ME"；qcut 需 `rank(method="first")` 防重复标签。 |
| E3 | **.gitignore 否定规则**：父目录被忽略（`report/`）后 `!report/daily_signal.py` 不生效（实测 report/* + ! 也不生效）→ 最终把脚本移到 data/ 目录解决。 |
| E4 | **output/ 与 report/ 整体被 gitignore**：评估 JSON/报告/面板缓存是本地产物不入库；页面 fallback 读取这些本地文件。 |
| E5 | **deck 是单文件 http.server**：改 live_api.py 等需重启 deck 才生效；重启：`kill $(lsof -ti tcp:8787)` → `nohup .venv/bin/python -u deck/deck_server.py &`。 |
| E6 | **git 工作流事实**：本地身份用 `-c user.name="Asaph-L" -c user.email="liangjunshen0@gmail.com"`；提交信息中文；`待办队列.md` 是 dev_auto 产物勿提交。 |

---

## 附：排查手段速查（均为实测有效）

- 卡死定位：`faulthandler.dump_traceback_later(60, exit=True)` + runpy 包装
- 进程残留确认：`pgrep -fl <pattern>`（沙箱内 ps 受限）
- 认证 vs 网络：`GIT_TERMINAL_PROMPT=0 git push ...`
- 面板加载 vs 计算耗时：分步 `time.time()` + flush print
- 单位/口径验证：取单只已知标的（茅台/平安）核对量级
