'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const bridge = require('../harness/home/profiles/web/plugins/dshq-task-protocol.js')
const quantBridge = require('../harness/home/profiles/web/plugins/dsq-quant-bridge.js')

const task = {
  protocol: 'dshq-task/v1',
  task_id: 'bridge-contract-001',
  title: '桥接合同测试',
  objective: '验证严格任务合同，不启动任何模型。',
  ordered_steps: ['读取合同', '返回 receipt'],
  allowed_paths: ['docs/CODEX_HANDOFF.md'],
  forbidden_paths: ['.git', 'data/cache'],
  constraints: ['禁止联网'],
  acceptance: ['合同通过校验'],
  git: { branch: null, commit: false, push: false },
  authorization: { external_model_context: true }
}

const normalized = bridge.validateTask(task, { protocol: 'dshq-task/v1' })
assert.strictEqual(normalized.task_id, task.task_id)
assert.strictEqual(bridge.payloadHash(task), bridge.payloadHash(JSON.parse(JSON.stringify(task))))
assert.strictEqual(bridge.validRelativePath('docs/file.md'), true)
assert.strictEqual(bridge.validRelativePath('../secret'), false)
assert.throws(function () {
  bridge.validateTask(Object.assign({}, task, { authorization: { external_model_context: false } }),
    { protocol: 'dshq-task/v1' })
}, /显式授权/)
assert.throws(function () {
  bridge.validateTask(Object.assign({}, task, { allowed_paths: ['.'] }), { protocol: 'dshq-task/v1' })
}, /相对路径/)

const receipt = bridge.parseReceipt(
  '完成\n<DSHQ_RECEIPT>{"protocol":"dshq-task-receipt/v1","task_id":"bridge-contract-001","status":"succeeded","summary":"ok","checks":[{"command":"node validation/test_harness_bridge_contract.js","status":"passed","detail":"ok"}],"changed_files":["docs/CODEX_HANDOFF.md"],"commit":null,"push":null}</DSHQ_RECEIPT>',
  task,
  'dshq-task-receipt/v1'
)
assert.strictEqual(receipt.status, 'succeeded')
assert.throws(function () {
  bridge.parseReceipt(
    '<DSHQ_RECEIPT>{"protocol":"dshq-task-receipt/v1","task_id":"bridge-contract-001","status":"succeeded","summary":"false green","checks":[{"command":"test","status":"failed"}],"changed_files":[],"commit":null,"push":null}</DSHQ_RECEIPT>',
    task, 'dshq-task-receipt/v1')
}, /all|passed|全部/)
assert.throws(function () {
  bridge.parseReceipt(
    '<DSHQ_RECEIPT>{"protocol":"dshq-task-receipt/v1","task_id":"bridge-contract-001","status":"succeeded","summary":"scope escape","checks":[{"command":"test","status":"passed"}],"changed_files":["data/cache/bars.db"],"commit":null,"push":null}</DSHQ_RECEIPT>',
    task, 'dshq-task-receipt/v1')
}, /allowed_paths|forbidden_paths/)
assert.throws(function () {
  bridge.parseReceipt('没有回执', task.task_id, 'dshq-task-receipt/v1')
}, /缺少/)

function response() {
  return {
    code: null, body: null, req: { headers: {} }, setHeader: function () {},
    writeHead: function (code) { this.code = code },
    end: function (body) { if (body) this.body = JSON.parse(body) }
  }
}

function installHarness(root, identityOk) {
  const routes = []
  const tokenFile = path.join(root, 'token')
  const taskLog = path.join(root, 'tasks.jsonl')
  fs.writeFileSync(tokenFile, 'x'.repeat(40), { mode: 0o600 })
  const receiptText = '<DSHQ_RECEIPT>' + JSON.stringify({
    protocol: 'dshq-task-receipt/v1', task_id: task.task_id, status: 'succeeded', summary: 'reported',
    checks: [{ command: 'offline-contract', status: 'passed', detail: 'model claim' }],
    changed_files: ['docs/CODEX_HANDOFF.md'], commit: null, push: null
  }) + '</DSHQ_RECEIPT>'
  const agent = {
    id: 'agent',
    session: { events: [{ type: 'assistant/message', data: { message: { content: receiptText } } }] },
    whenIdle: async function () {}
  }
  const apiProxy = { sessions: {
    create: async function (request) { return { result: { ok: true, value: { sessionId: request.payload.sessionId } } } },
    prompt: async function () { return { result: { ok: true } } }
  } }
  bridge.install({
    ctx: {}, agents: { get: function () { return agent } }, sessionPersistence: { flush: async function () {} },
    apiProxy: apiProxy, protocol: 'dshq-task/v1', receiptProtocol: 'dshq-task-receipt/v1',
    projectRoot: root, dshHome: root, taskLog: taskLog, tokenFile: tokenFile,
    webServer: { register: function (route) { routes.push(route); return function () {} } },
    maxActiveTasks: 1, homeFingerprint: 'test', identityOk: identityOk,
    identityError: identityOk ? '' : 'wrong home', cors: function () {},
    json: function (res, code, body) { res.code = code; res.body = body },
    parseQuery: function () { return {} }, readJson: async function (req) { return req.body },
    textOfContent: function (content) { return typeof content === 'string' ? content : '' }
  })
  return routes
}

async function integrationContracts() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dshq-bridge-contract-'))
  try {
    let routes = installHarness(root, false)
    let tasksRoute = routes.find(function (route) { return route.path === '/quant/tasks' })
    let res = response()
    await tasksRoute.handler({ method: 'POST', headers: { 'x-dshq-token': 'x'.repeat(40) }, body: task }, res)
    assert.strictEqual(res.code, 503)
    assert.match(res.body.error, /identity mismatch/)
    const guardedTaskRoute = routes.find(function (route) {
      return route.kind === 'prefix' && route.path === '/quant/tasks'
    })
    res = response()
    await guardedTaskRoute.handler({
      method: 'POST', url: '/quant/tasks/' + task.task_id + '/verify',
      headers: { 'x-dshq-token': 'x'.repeat(40) }, body: {}
    }, res)
    assert.strictEqual(res.code, 503)

    routes = installHarness(root, true)
    tasksRoute = routes.find(function (route) { return route.path === '/quant/tasks' })
    const taskRoute = routes.find(function (route) {
      return route.kind === 'prefix' && route.path === '/quant/tasks'
    })
    res = response()
    await tasksRoute.handler({ method: 'POST', headers: { 'x-dshq-token': 'x'.repeat(40) }, body: task }, res)
    assert.strictEqual(res.code, 202)
    await new Promise(function (resolve) { setImmediate(resolve) })
    await new Promise(function (resolve) { setImmediate(resolve) })

    res = response()
    await taskRoute.handler({ method: 'GET', url: '/quant/tasks/' + task.task_id, headers: {} }, res)
    assert.strictEqual(res.body.task.status, 'verification_required')
    assert.notStrictEqual(res.body.task.status, 'succeeded')

    res = response()
    await taskRoute.handler({
      method: 'POST', url: '/quant/tasks/' + task.task_id + '/verify',
      headers: { 'x-dshq-token': 'x'.repeat(40) },
      body: { status: 'verified_succeeded', summary: 'missing checks', checks: [], changed_files: ['docs/CODEX_HANDOFF.md'], commit: null, push: null }
    }, res)
    assert.strictEqual(res.code, 400)

    res = response()
    await taskRoute.handler({
      method: 'POST', url: '/quant/tasks/' + task.task_id + '/verify',
      headers: { 'x-dshq-token': 'x'.repeat(40) },
      body: {
        status: 'verified_succeeded', summary: 'independently verified',
        checks: [{ command: 'node validation/test_harness_bridge_contract.js', status: 'passed', detail: 'local' }],
        changed_files: ['docs/CODEX_HANDOFF.md'], commit: null, push: null
      }
    }, res)
    assert.strictEqual(res.code, 200)
    assert.strictEqual(res.body.task.status, 'verified_succeeded')
    assert.strictEqual(res.body.task.verifier, 'codex-local')

    const previousRoot = process.env.DSHQ_PROJECT_ROOT
    const previousHome = process.env.DSH_HOME
    const previousTokenFile = process.env.DSHQ_BRIDGE_TOKEN_FILE
    const previousTaskLog = process.env.DSHQ_BRIDGE_TASK_LOG
    const wrongRoot = path.join(root, 'wrong-project')
    const wrongHome = path.join(root, 'wrong-home')
    fs.mkdirSync(wrongRoot)
    fs.mkdirSync(wrongHome)
    process.env.DSHQ_PROJECT_ROOT = wrongRoot
    process.env.DSH_HOME = wrongHome
    // This branch tests identity mismatch handling, not inherited production
    // path overrides. Keep it hermetic when run inside the HARNESS service.
    delete process.env.DSHQ_BRIDGE_TOKEN_FILE
    delete process.env.DSHQ_BRIDGE_TASK_LOG
    const guardedRoutes = []
    const webServer = { register: function (route) { guardedRoutes.push(route); return function () {} } }
    const services = {
      webServer: webServer,
      sessions: { get: function () { return null } },
      agents: { roots: function () { return [] }, get: function () { return null } },
      subagents: {}, sessionTitle: {}, sessionPersistence: {}, apiProxy: { sessions: {} }
    }
    const ctx = {
      get: function (name) { return services[name] },
      effect: function (callback) { return callback() }
    }
    try {
      quantBridge.apply(ctx)
    } finally {
      if (previousRoot == null) delete process.env.DSHQ_PROJECT_ROOT
      else process.env.DSHQ_PROJECT_ROOT = previousRoot
      if (previousHome == null) delete process.env.DSH_HOME
      else process.env.DSH_HOME = previousHome
      if (previousTokenFile == null) delete process.env.DSHQ_BRIDGE_TOKEN_FILE
      else process.env.DSHQ_BRIDGE_TOKEN_FILE = previousTokenFile
      if (previousTaskLog == null) delete process.env.DSHQ_BRIDGE_TASK_LOG
      else process.env.DSHQ_BRIDGE_TASK_LOG = previousTaskLog
    }
    const healthRoute = guardedRoutes.find(function (route) { return route.path === '/quant/health' })
    res = response()
    await healthRoute.handler({ method: 'GET', headers: {} }, res)
    assert.strictEqual(res.body.identity_ok, false)
    assert.strictEqual(res.body.ready, false)
    const chatRoute = guardedRoutes.find(function (route) { return route.path === '/quant/chat2' })
    res = response()
    await chatRoute.handler({ method: 'POST', headers: {} }, res)
    assert.strictEqual(res.code, 503)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

integrationContracts().then(function () {
  console.log('HARNESS bridge contract: 22 assertions passed (no model, temporary files only)')
}).catch(function (error) {
  console.error(error)
  process.exitCode = 1
})
