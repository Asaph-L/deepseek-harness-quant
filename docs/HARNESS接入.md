# DeepSeek HARNESS 深度嵌入说明

本项目将 **DeepSeek HARNESS（DSH）** 深度嵌入：控制页的「控制/对话」区、牛散主观选股桥接、AI 协作能力开箱即用（MIT 许可，随包分发）。

## 一、架构

```
QuantDeck（本项目）
├── deck/deck_server.py       量化 Web 系统（:8787）
└── harness/                  DeepSeek HARNESS 运行时（:3080）
    ├── node_modules/          DSH 及全部依赖（npm 安装，随包自带）
    ├── home/                  DSH_HOME（用户数据根）
    │   ├── .credentials.yaml  ★ API Key 接入点（见下）
    │   ├── settings.yaml      模型路由（默认 deepseek-official）
    │   ├── profiles/web/      宿主组合（cordis.yml + cordis.patch.yml）
    │   │   └── plugins/       ★ 量化桥 + dshq-task/v1 派单协议
    │   └── skills/            ★ 预置空白 skill（牛散 7 位模板）
```

- **桥接插件**由 `profiles/web/cordis.patch.yml` 挂载。旧控制台/牛散路由继续保留；
  Codex 派单只走带本机令牌、身份核验和结构化 receipt 的 `/quant/tasks` 合同，
  详见 [`HARNESS派单协议.md`](HARNESS派单协议.md)。
- 量化系统与 HARNESS 独立进程、独立端口，通过 HTTP 桥接；任何一方缺失另一方照常运行。
- 项目只允许 `<repo>/harness/home` 这一套 `DSH_HOME`；外部环境变量或桌面版 home
  不得改变量化项目的会话/任务存储位置。

## 二、第一步：接入 DeepSeek API Key（关键）

> **核心思路：先接 AI 的 API（DeepSeek），AI 就能帮你接入剩下的 API（Tushare 等）。**

1. 注册 DeepSeek 开放平台并创建 API Key：https://platform.deepseek.com/api_keys
2. 复制模板为正式凭据文件：
   ```bash
   # Windows
   copy harness\home\.credentials.yaml.example harness\home\.credentials.yaml
   # Linux / macOS
   cp harness/home/.credentials.yaml.example harness/home/.credentials.yaml
   ```
3. 用编辑器打开 `harness/home/.credentials.yaml`，把 `<your-deepseek-api-key>` 替换为你的真实 Key
4. 从仓库根目录运行 `python launcher.py`（Windows 也可双击 `启动.cmd`）。启动器会强制注入并核验唯一的 `<repo>/harness/home`；不要绕过启动器直接执行 HARNESS 的 Node 入口。
5. 打开 http://127.0.0.1:3080（HARNESS GUI）或量化控制页（:8787/control）——即可与 AI 对话

## 三、让 AI 帮你接入其余 API

接入 DeepSeek API Key 后，直接在对话中要求，例如：

> 「请帮我完成 Tushare 数据源接入：把 config/params.yaml 中 data.tushare_token 的配置流程走一遍，并告诉我需要在哪里注册、token 填哪里。」

AI 会指导/协助你完成：
- **Tushare token**（行情/财报数据，https://tushare.pro 注册）
- 其他可选数据源（akshare 免费接口无需 token）
- 系统参数调优、skill 填充等

## 四、预置 Skill（打开即自带）

`harness/home/skills/` 已包含 7 个牛散研究 skill；内容是研究人格与风险约束，
不构成投资建议，其输出仍需进入远期池验证：

| Skill | 主题 |
|---|---|
| `niu-san-linyuan` | 林园（价值投资派） |
| `niu-san-fengliu` | 冯柳（逆向/弱者体系） |
| `niu-san-chaoguyangjia` | 炒股养家（情绪周期） |
| `niu-san-chenxiaoqun` | 陈小群（游资席位/排雷） |
| `niu-san-zhangmengzhu` | 章盟主（大资金） |
| `niu-san-zhaolaoge` | 赵老哥（打板/妖股） |
| `niu-san-distillation` | 牛散蒸馏方法论（总览框架） |

如需更新内容，必须标注来源与可信度，并保留风险批判；控制页输出自动进入远期池，
不能绕过 L2 决策卡片成为第二套推荐逻辑。

## 五、故障排查

| 现象 | 处理 |
|---|---|
| 控制页显示 HARNESS 未连接 | Node.js 未安装 / harness 运行时缺失（先跑 `harness/install.cmd`）/ 3080 端口被占 |
| 对话报鉴权错误 | `.credentials.yaml` 未配置或 Key 无效（检查格式：`DEEPSEEK_API_KEY: sk-...`） |
| 模型报错/限流 | DeepSeek 账户余额不足；或改 `settings.yaml` 的 model |
| 牛散对话无反应 | 需 Node + API Key；桥接插件日志见 HARNESS 控制台输出 |

## 六、重新安装 harness 运行时（可选）

```bash
cd harness
npm install --no-audit --no-fund
```

## 七、从桌面版 home 安全迁移（一次性）

迁移前先退出 DeepSeek Desktop，并停止由 `launcher.py` 启动的项目 HARNESS。默认命令只生成只读计划，不会复制、删除或改写任何文件：

```bash
.venv/bin/python -B scripts/migrate_harness_home.py --json
```

确认计划后才显式执行：

```bash
.venv/bin/python -B scripts/migrate_harness_home.py --apply
```

应用模式会同时锁住源/目标 home 的迁移事务、占住本机 HARNESS 端口并检查运行进程；无法确认两侧都已停止时直接拒绝。它会先把全部结果和原文件备份到 `harness/home/migration-backups/<timestamp>/`，再逐文件原子提交；任一步失败都会逆序回滚，并在 `manifest.json` 中记录状态。源 home 永不删除，`profiles/` 永不迁移，JSON 存储采用并集合并且同一 session row 取最高 `seq`。

若机器在提交过程中异常退出，可在两侧 HARNESS 仍保持停止时按失败信息给出的绝对路径恢复：

```bash
.venv/bin/python -B scripts/migrate_harness_home.py --recover /absolute/path/to/manifest.json
```

不要手工删除 `migration-backups` 中状态为 `committing`、`rollback_failed` 或 `recovery_failed` 的目录。
