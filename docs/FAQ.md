# FAQ

## 启动与依赖

**Q1: 启动 deck_server.py 报 ImportError？**
先确认依赖装齐：`python -m pip install -r requirements.txt`。若 Python 3.13+ 装不上 vectorbt/quantstats，可先安装最小集（pandas numpy scipy PyYAML akshare requests tqdm）——Web 系统与因子引擎不需要回测增强库。

**Q2: 8787 端口被占用？**
`netstat -ano | findstr 8787` 找到占用进程，结束它或改端口：`python deck/deck_server.py --port 8888`。

**Q3: 页面能开但数据全空？**
数据目录未就绪。检查：`config/params.yaml` 的 `data.cache_dir` 或环境变量 `LWQUANT_CACHE_DIR` 是否指向含 `bars.db` 的目录；或先用演示数据模式（见快速开始 §5）。

**Q4: EXE 双击后浏览器没自动打开？**
手动打开 http://127.0.0.1:8787。若服务也未启动，检查 exe 同目录是否有 `data/`（或系统环境变量 `LWQUANT_CACHE_DIR` 指向数据目录）。

## 数据

**Q5: fetch_data.py 需要什么？**
Tushare token（`config/params.yaml` → `data.tushare_token`）。120 积分可拉日线+股票列表；财报/龙虎榜等需更高积分。akshare 免费接口免 token。

**Q6: 为什么仓库里没有数据？**
Tushare 等数据源协议禁止再分发（详见 docs/数据说明.md）。请自行获取；`data/demo/` 提供合成演示数据。

**Q7: 换手率（turn）2019 年前为什么缺失？**
历史换手率数据源覆盖有限，2019 年起才 90%+ 覆盖。**请勿用 2019 前数据做换手类因子结论**（项目内所有 turn_low 结论均以 2019-2026 为验证区间）。

## 策略与结果

**Q8: Pitch 高分集中持仓为什么不推荐？**
项目实证：pitch 高分集中持仓回撤达 -41%~-60%（`因子池/研究_pitch信号分组合层验证` 同款结论）。因此主仓位走 turn_low 低换手分散，pitch 高分仅作卫星仓/排雷提示。

**Q9: 牛散主观池是什么？**
控制页「主观·牛散」分类下的 7 位牛散人格（林园/冯柳/炒股养家/陈小群/章盟主/赵老哥/方法论）基于量化 Pitch 快照给出选股意见，意见自动结构化入远期池（按决策者分组），与 A/B/C/D 四池并列做远期收益验证。**牛散为人格模拟（公开资料蒸馏），不构成投资建议，也不是买入指令来源。**

**Q10: ETF 映射的"复制度 76%"是什么意思？**
ETF 组合日收益与策略（turn_low）日收益的相关性 ≈ 0.76。ETF 是风格/行业暴露代理，只能部分复制策略暴露（抓不到 20 只低换手个股的选股 alpha）。定位：小资金表达/防御风格参考。

**Q11: 回测与实盘差异？**
策略引擎为收盘价近似（T+1 对齐），未完整模拟一字板成交与历史印花税分段——回测结果偏乐观，实盘请按文档披露口径打折看待。

## 其他

**Q12: 控制页显示"HARNESS 未连接"？**
控制页的「控制/对话」区可选接入 DeepSeek HARNESS（外部运行时，127.0.0.1:3080）。未安装时其余系统功能不受影响。
