# LW量化 · UI 设计标准 v1（唯一权威 · 2026-08-13 定稿）

> 用户指示（07:26）："把设计定死成标准，不然越来越差，甚至得完全推倒重做"
> 本文件是**唯一设计标准**：所有页面生成器（report/*.py）必须遵守，改动前必读。
> 来源：UI整改计划_桌子式框架 / UI编写规范与资产处置 / UI架构修订v2（2026-08-12 三文档）+ StyleKit 动画库（#156）——浓缩定稿。
> **三铁律：①UI 是壳，数据是 API ②契约优先，兜底渲染 ③白名单制，枚举动态下发**

---

## 1. 设计令牌（Design Tokens）——唯一来源 deck/ui_common.css

所有颜色/字体/间距**必须**用 CSS 变量，禁止页面内硬编码色值。

| Token | 值 | 用途 |
|---|---|---|
| `--navy` | `#0A2540` | 主文字/深色底 |
| `--navy2` | `#185FA5` | 品牌/链接 |
| `--gold` | `#D4A843` | 强调/警示 |
| `--green` | `#0F6E56` | 安全/持仓（★跌）|
| `--red` | `#C0392B` | 危险/涨（★A股红涨）|
| `--bg` | `#F0F3F8` | 页面底 |
| `--muted` | `#8a94a6` | 次要文字 |
| `--line` | `#e5e9f0` | 边框/分隔 |

**★红涨绿跌是 A 股铁律**：收益为正 → 红色（--red），为负 → 绿色（--green）。禁止美式红跌绿涨。

## 2. 布局架构（3+1 页，专业命名，无"桌"字）

```
LW量化（品牌条：数据日 · 决策链 · 择时 小字）
├── /         门户：品牌条 + 今日摘要（择时融入）+ 6 入口卡
├── /pitch    决策：择时 Tab + 机会池 + Pitch长 + Pitch短 + 远期池 + 待处理
├── /holdings 持仓：绩效曲线 + 持仓卡（人工/程序分组）+ 风控
├── /factors  因子池：当前因子 Tab + 回测 + 因子健康 + 研究成果
└── /help     说明：Pitch原理 + 数据流通 + 术语 + 历史 + 竞价 + 工具（全折叠）
```

- 低价值/低频页（auction/history/glossary/dynamic/ranks/stockcheck）→ 收进 /help 折叠区或"更多页面"折叠
- **首屏 ≤1 屏**：门户入口卡 ≤9 张（核心 6 + 更多折叠）
- 次要入口用 `<details>` 折叠，默认收起——第一眼清爽，功能不丢

## 3. 组件规范（全站统一）

| 组件 | 规范 |
|---|---|
| 导航条 | `.lw-nav`（ui_common.css）+ `<div class="lw-nav" data-page="...">` + nav_common.js——**禁止自绘导航** |
| 卡片 | `.card`：白底 #fff、border 1px var(--line)、radius 14px、padding 18px 20px、hover 抬升（anim-hover-lift） |
| 空态 | `.lw-empty`：无数据时必显示（"暂无XX + 说明"），禁止空白页 |
| 徽章 | 状态徽章统一小圆角标签（badge）：✅有效/⚠️漂移/❌失效/🆕新登记/⚡强因子/📌建议 |
| Tab | 决策页 6 Tab / 因子池 4 Tab——每 Tab 独立 API 懒加载 |
| 折叠 | 低频内容一律 `<details>` + `.more-summary`（门户"更多页面"模式）|
| 表格 | 边框合并、表头 muted、数字 tabular-nums、正红负绿 |
| 状态点 | `.anim-pulse-ring`（绿=正常 / .amber=预警）|

## 4. 动画规范（StyleKit · deck/anim_common.css）

**所有动画必须用 anim_common.css 现成类，禁止自写 keyframes**：

| 场景 | 类 | 节奏 |
|---|---|---|
| 页面主区块入场 | `.anim-fade-in-up`（.d1/.d2/.d3 递进）| 600ms expo-out |
| 卡片网格错开入场 | `.anim-stagger` + `--stagger-index:N` | 500ms，75ms 间隔 |
| 卡片 hover | `.anim-hover-lift` | 200ms |
| KPI 数字滚动 | `counterRoll()`（timing_dash 模式）| 2s，仅首次加载 |
| 状态呼吸灯 | `.anim-pulse-ring` / `.amber` | 2s 循环 |
| 加载占位 | `.anim-shimmer` | 2s 循环 |
| 行情滚动条 | `.lt-bar` / `.lt-track` | 14s 线性 |

- 缓动统一：入场 `cubic-bezier(0.16,1,0.3,1)`，hover `cubic-bezier(0.33,1,0.68,1)`
- **动画只增强不干扰数据读取**（数字滚动仅首次）；必须保留 `prefers-reduced-motion` 降级（anim_common.css 已内置）

## 5. 数据契约铁律

1. **UI 是壳，数据是 API**：页面只消费 `/api/live/*`，永不直接读外包 CSV/JSON
2. **兜底渲染**：每个字段 `(d.xxx || '—')`——字段缺失不白屏
3. **schema 版本**：live_api 返回 `schema_version`，前端渲染前校验，不匹配显示"升级中"横幅
4. **枚举动态下发**：类型/家族/分类从 API overview 读，**不硬编码**——新因子自动出现
5. **校验工具**：改页面后跑 `deck/check_ui_fields.py`（13 页 0 异常）

## 6. 命名规范（专业化，全站统一）

- 页面：门户 / 决策 / 持仓 / 因子池 / 说明（无"桌"字、无"看板"堆砌）
- 术语：机会 / Pitch / 因子 / 持仓 / 远期池——禁止口语化（"搞"/"弄"/"东东"）
- 品牌条：顶部只写 **LW量化** + 右侧小字（数据日 · 决策链 · 择时）

## 7. 修改守则（冲突点 6 组——改动必查依赖）

| 冲突点 | 规则 |
|---|---|
| 1 页面写入方 | **改模板必须改 report/ 生成器**（真源），改后 `dev_auto.py --ui` 重建；直接改 deck/*.html 会被 8.56 覆盖（仅 pitch.html 静态可直改）|
| 2 路由表 | 删页/改名必须同步 deck_server.py 路由 + 门户卡 + health_scan PAGES |
| 3 检查器 | health_scan PAGES 硬编码页清单 + PAGES_301_OK；check_consistency 依赖 API 名（API 名不可改，只可加字段）|
| 4 门户卡 | portal 卡 href 与路由一一对应，删页同步否则死链 |
| 5 JS 注册 | live_patch.js `RENDER={opp,watch,tech,holdings,actions}` + 页面 `data-page` 属性匹配 |
| 6 写保护+守护 | 时间戳文件名规避只读；改 deck_server.py 后**全杀 8787 单实例重启** |

## 8. 验证铁律（每步改动后全绿才进下一步）

```
python dev_auto.py --ui            # 重建模板（改生成器后）
python deck/check_ui_fields.py     # 13 页 0 字段异常
python data/health_scan.py         # 31 页 31 API 全绿
python data/check_consistency.py   # 一致性全绿
# Deck 改动 → 全杀 8787 单实例重启 → netstat 确认单监听
```

## 9. 资产处置（废弃 = 粉碎；归档 = 桌面垃圾桶）

- 废弃：dashboard_pool/monitor/research（301 残影）、auction/history/ranks 僵尸页、旧时间戳 html（留最新 1）
- 保留：pitch.html（静态）、stockcheck（工具页）、全部 API 与数据层
- 旧页面归档 `桌面/垃圾桶/`，新框架 3 天无回归再清

---

**执行顺序（阶段 2 遗留）**：S1 ui_shell 骨架 → S2 门户 6 卡 → S3-S6 决策页 4 Tab → S7 持仓分组 → S8 因子池 4 Tab → S9 说明页折叠 → S10-S12 路由/检查器/清理。
**当前进度（2026-08-13 07:26）**：S1-S12 已在 #151-164 完成主体 + 门户减负（9 卡 + 更多折叠）；**待办：统一全站 CSS 引用（ui_common + anim_common 强制）→ 各页按标准校色/校动画**。

## ★设计语言 v2（2026-08-13 #289 总指挥英文规范——唯一设计依据，原文固化）

> 用户英文规范原文（勿再丢失）：
> "Design a finance dashboard with a dark sidebar (#0f172a) and a light content area (#f8fafc).
> Sidebar: 256px wide, nav labels in #94a3b8, active item with a #3b82f6 left border and #1e293b background.
> Content: 4 KPI cards showing balance, income, expenses, and savings rate, each with a 12-point sparkline.
> Numbers use tabular-nums at 28px semibold; positive deltas in #16a34a, negative in #dc2626,
> always paired with an arrow icon so color is not the only signal.
> Cards on white with 1px #e2e8f0 borders, 24px padding, 24px grid gap.
> Include a transactions table with zebra rows (#f1f5f9) and right-aligned amounts."

**落地映射（主系统 A 股语境）**：
| 规范 | 实现 |
|---|---|
| dark sidebar #0f172a / 256px / nav #94a3b8 / active #3b82f6 左边框 + #1e293b | components/sidebar.js + app.css .sb 系列 |
| light content #f8fafc | body 背景 + body{padding-left:256px} |
| KPI 卡 12 点 sparkline | R.spark(series) + portal_dash.kpi_series（历史文件聚合 12 点）|
| tabular-nums 28px semibold | .kp .vl + .td-num |
| delta 正负配箭头（color 非唯一信号）| .d-up▲/.d-down▼/.d-flat▬（**涨跌色保留 A 股红涨绿跌**——系统铁律，箭头保证非颜色信号）|
| 卡片 #fff + 1px #e2e8f0 + 24px padding | .v2-card/.kp |
| 24px grid gap | .v2-kpi gap:24px |
| 斑马纹 #f1f5f9 + 金额右对齐 | .v2-card table tr:nth-child(even) + .td-num/.td-num-r |

**注意**：规范色 #16a34a/#dc2626 为欧美习惯（绿正红负）；主系统沿用 A 股红涨绿跌（--red/--green 反转）——如需切规范色，改 app.css .d-up/.d-down 两行即可。
