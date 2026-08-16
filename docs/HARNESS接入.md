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
    │   │   └── plugins/dsq-quant-bridge.js   ★ 量化桥接插件（/quant/* 路由）
    │   └── skills/            ★ 预置空白 skill（牛散 7 位模板）
```

- **桥接插件**由 `profiles/web/cordis.patch.yml` 挂载，提供 `/quant/sessions`、`/quant/chat2`、`/quant/niu/*` 等路由——量化控制页原生对话、牛散主观选股自动入远期池全部走它。
- 量化系统与 HARNESS 独立进程、独立端口，通过 HTTP 桥接；任何一方缺失另一方照常运行。

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
4. 启动（launcher 或 `python deck/deck_server.py` + `node harness/node_modules/@deepseek-ai/dsh/lib/bin.js web`）
5. 打开 http://127.0.0.1:3080（HARNESS GUI）或量化控制页（:8787/control）——即可与 AI 对话

## 三、让 AI 帮你接入其余 API

接入 DeepSeek API Key 后，直接在对话中要求，例如：

> 「请帮我完成 Tushare 数据源接入：把 config/params.yaml 中 data.tushare_token 的配置流程走一遍，并告诉我需要在哪里注册、token 填哪里。」

AI 会指导/协助你完成：
- **Tushare token**（行情/财报数据，https://tushare.pro 注册）
- 其他可选数据源（akshare 免费接口无需 token）
- 系统参数调优、skill 填充等

## 四、预置空白 Skill（打开即自带）

`harness/home/skills/` 已原生注入 7 个空白 skill 模板：

| Skill | 主题 |
|---|---|
| `niu-san-linyuan` | 林园（价值投资派） |
| `niu-san-fengliu` | 冯柳（逆向/弱者体系） |
| `niu-san-chaoguyangjia` | 炒股养家（情绪周期） |
| `niu-san-chenxiaoqun` | 陈小群（游资席位/排雷） |
| `niu-san-zhangmengzhu` | 章盟主（大资金） |
| `niu-san-zhaolaoge` | 赵老哥（打板/妖股） |
| `niu-san-distillation` | 牛散蒸馏方法论（总览框架） |

每个模板含骨架（人物画像/理念/风险批判/因子假设/参考）。**三种填充方式**：
1. 让 AI 填充：对话中要求「请基于公开资料完善 niu-san-linyuan skill」
2. 参考 `niu-san-distillation` 框架自行编辑
3. 导入自己的研究资料（标注来源与可信度）

> 牛散技能填充后，控制页「主观·牛散」分类即出现对应可对话的选股人格（其决策自动入远期池验证）。

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
