# 上传 GitHub 指南（含项目描述建议）

## 一、准备（一次即可）

```bash
# 1. 安装 Git：https://git-scm.com/download/win（一路默认）
# 2. 注册 GitHub：https://github.com 新建仓库（Public）
#    仓库名建议：lwquant（或 deepseek-harness-quant）
#    描述（About）建议：
#      A股低频主观量化研究体系 —— Pitch决策链 · 因子引擎 · 五池远期验证 · ETF映射 · HARNESS 深度嵌入
#    Topics 建议：quantitative-finance a-share factor-investing etf-rotation backtest
#      deepseek quantitative-trading stock-market pandas

# 3. 配置身份（一次即可）
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的GitHub邮箱"
```

## 二、初始化并上传（在 D:\deepseek-harness-quant-open 目录）

```bash
cd /d D:\deepseek-harness-quant-open

git init
git add .
git commit -m "v1.0.6: 首个开源发布（Pitch决策链/五池远期/ETF映射/HARNESS嵌入/更新机制）"

git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> 提示：
> - `.gitignore` 已配置好（node_modules/用户配置/数据/运行时数据 全部排除）——上传的是**纯净源码**（约 2.5MB）
> - 若 push 报错，检查 remote 地址与仓库名是否一致；SSH 方式可用 `git@github.com:<用户名>/<仓库名>.git`
> - 大文件（harness 运行时）不进 git，走 Release 附件（见下）

## 三、发布 Release（下载即用包）

1. GitHub 仓库页 → Releases → **Create a new release**
2. Tag：`v1.0.6`（与 `VERSION` 文件一致）
3. 标题：`v1.0.6`；正文：粘贴 CHANGELOG.md 的对应条目
4. 附件上传：
   - `D:\deepseek-harness-quant-open\DSHQuant-v1.0.6-Release.zip`（**全量含 HARNESS 运行时 + 演示数据，解压即用**）
   - `D:\deepseek-harness-quant-open\dist\QuantDeck.exe`（Windows 一键启动器）
5. 点 **Publish release**

## 四、未来更新（增量发布）

主系统改动后（工作区流程）：

```bash
python scripts/build_update.py --from <主系统根> --version 1.0.7
# 产物 updates/update_1.0.7.zip → 作为 Release 附件发布
```

用户应用：`python scripts/update.py update_1.0.7.zip`（自动备份，详见 docs/更新与发布.md）

## 五、仓库展示建议

- **README.md 已就绪**（首页即完整介绍：功能表/快速开始/文档/数据边界/许可）
- 建议在仓库 Settings → General → Description 填：
  `A股低频主观量化研究体系：Pitch 决策链 + 因子引擎 + 五池远期验证 + ETF 映射 + DeepSeek HARNESS 深度嵌入（MIT）`
- Topics 选 6-8 个（见上）提升搜索曝光
- 可加一张截图（门户页）到 README 顶部增强展示
