# Codex → DeepSeek HARNESS 派单协议

项目只使用 `<repo>/harness/home`。`launcher.py` 会覆盖外部 `DSH_HOME`，并用
`/quant/health` 的项目路径、协议版本和 home 指纹校验现有 3080 服务；不匹配时
拒绝接管，避免两个进程继续写不同会话库。

## 任务合同

复制 `config/harness_task.example.json` 后填写任务。合同必须明确：

- 唯一 `task_id`、标题和目标终点；
- 严格有序的执行步骤；
- 可修改路径、禁止路径和约束；
- 可机器核对的验收条件；
- 是否允许 commit/push（模板默认都禁止）；
- `authorization.external_model_context=true`，表示用户明确允许本任务所需仓库内容
  发送给外部模型。

服务端对 task ID 做内容哈希幂等：相同合同重复提交会返回已有任务，不同合同复用
同一 ID 返回 409。默认一次只运行一个任务。模型必须在结束时返回
`dshq-task-receipt/v1` 结构化回执；缺失或字段不符会标记 `protocol_error`，不能冒充完成。

## 命令

以下两条只读或仅本地解析，不调用模型：

```bash
.venv/bin/python -B scripts/harness_dispatch.py health
.venv/bin/python -B scripts/harness_dispatch.py list --limit 20
.venv/bin/python -B scripts/harness_dispatch.py validate path/to/task.json
```

真正提交前，任务 JSON 和命令行都要明确授权：

```bash
.venv/bin/python -B scripts/harness_dispatch.py submit path/to/task.json \
  --allow-external-model-context
```

状态与补充说明：

```bash
.venv/bin/python -B scripts/harness_dispatch.py status TASK_ID
.venv/bin/python -B scripts/harness_dispatch.py followup TASK_ID \
  --text "只补充必要信息" --allow-external-model-context
```

HTTP 协议为：`GET /quant/health`、`POST/GET /quant/tasks`、
`GET /quant/tasks/<id>`、`POST /quant/tasks/<id>/followup`。所有修改型请求必须带
本机 `X-DSHQ-Token`；Deck 的同源代理会从唯一 home 读取令牌，浏览器拿不到令牌文件。
