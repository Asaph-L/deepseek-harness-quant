'use strict'

const crypto = require('crypto')
const fs = require('fs')
const path = require('path')

const FINAL_STATUSES = new Set(['succeeded', 'verified_succeeded', 'verification_failed', 'failed', 'blocked', 'protocol_error'])
const ACTIVE_STATUSES = new Set(['accepted', 'running'])
const AWAITING_VERIFICATION = new Set(['verification_required'])
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/

function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue)
  if (value && typeof value === 'object') {
    const out = {}
    for (const key of Object.keys(value).sort()) out[key] = stableValue(value[key])
    return out
  }
  return value
}

function payloadHash(value) {
  return crypto.createHash('sha256').update(JSON.stringify(stableValue(value))).digest('hex')
}

function validRelativePath(value) {
  if (typeof value !== 'string' || !value.trim() || value === '.') return false
  const normalized = value.replace(/\\/g, '/')
  if (normalized.startsWith('/') || /^[A-Za-z]:\//.test(normalized)) return false
  return !normalized.split('/').some(function (part) { return part === '..' })
}

function stringList(value, field, minItems) {
  if (!Array.isArray(value) || value.length < minItems || value.length > 100) {
    throw new Error(field + ' 必须是 ' + minItems + '..100 个字符串的数组')
  }
  const out = value.map(function (item) { return String(item || '').trim() })
  if (out.some(function (item) { return !item || item.length > 1000 })) {
    throw new Error(field + ' 含空值或超长值')
  }
  return out
}

function validateTask(raw, options) {
  const expected = options.protocol
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('任务必须是 JSON 对象')
  if (raw.protocol !== expected) throw new Error('protocol 必须是 ' + expected)
  const taskId = String(raw.task_id || '')
  if (!TASK_ID.test(taskId)) throw new Error('task_id 格式无效')
  const title = String(raw.title || '').trim()
  const objective = String(raw.objective || '').trim()
  if (!title || title.length > 120) throw new Error('title 必须为 1..120 字符')
  if (!objective || objective.length > 4000) throw new Error('objective 必须为 1..4000 字符')
  const orderedSteps = stringList(raw.ordered_steps, 'ordered_steps', 1)
  const allowedPaths = stringList(raw.allowed_paths, 'allowed_paths', 1)
  const forbiddenPaths = stringList(raw.forbidden_paths, 'forbidden_paths', 1)
  if (!allowedPaths.every(validRelativePath) || !forbiddenPaths.every(validRelativePath)) {
    throw new Error('allowed_paths/forbidden_paths 只能包含项目内相对路径，且不能是 .')
  }
  const constraints = stringList(raw.constraints, 'constraints', 1)
  const acceptance = stringList(raw.acceptance, 'acceptance', 1)
  const git = raw.git && typeof raw.git === 'object' ? raw.git : {}
  if (git.commit !== true && git.commit !== false) throw new Error('git.commit 必须显式为 boolean')
  if (git.push !== true && git.push !== false) throw new Error('git.push 必须显式为 boolean')
  if (git.push && !git.commit) throw new Error('git.push=true 时 git.commit 也必须为 true')
  const authorization = raw.authorization && typeof raw.authorization === 'object' ? raw.authorization : {}
  if (authorization.external_model_context !== true) {
    throw new Error('必须显式授权 authorization.external_model_context=true')
  }
  return {
    protocol: expected,
    task_id: taskId,
    title: title,
    objective: objective,
    ordered_steps: orderedSteps,
    allowed_paths: allowedPaths,
    forbidden_paths: forbiddenPaths,
    constraints: constraints,
    acceptance: acceptance,
    git: {
      branch: git.branch ? String(git.branch) : null,
      commit: git.commit,
      push: git.push
    },
    authorization: { external_model_context: true }
  }
}

function pathWithin(candidate, roots) {
  const normalized = String(candidate || '').replace(/\\/g, '/').replace(/^\.\//, '')
  return roots.some(function (root) {
    const base = String(root).replace(/\\/g, '/').replace(/\/$/, '')
    return normalized === base || normalized.startsWith(base + '/')
  })
}

function parseReceipt(text, taskOrId, receiptProtocol) {
  const task = taskOrId && typeof taskOrId === 'object' ? taskOrId : null
  const taskId = task ? task.task_id : taskOrId
  const source = String(text || '')
  const re = /<DSHQ_RECEIPT>([\s\S]*?)<\/DSHQ_RECEIPT>/g
  let match
  let candidate = null
  while ((match = re.exec(source)) !== null) candidate = match[1]
  if (candidate === null) throw new Error('模型回复缺少 DSHQ_RECEIPT')
  let receipt
  try { receipt = JSON.parse(candidate) } catch (error) { throw new Error('DSHQ_RECEIPT 不是合法 JSON') }
  if (receipt.protocol !== receiptProtocol) throw new Error('receipt protocol 不匹配')
  if (receipt.task_id !== taskId) throw new Error('receipt task_id 不匹配')
  if (!['succeeded', 'failed', 'blocked'].includes(receipt.status)) throw new Error('receipt status 无效')
  if (typeof receipt.summary !== 'string' || !receipt.summary.trim()) throw new Error('receipt summary 为空')
  if (!Array.isArray(receipt.checks) || !Array.isArray(receipt.changed_files)) {
    throw new Error('receipt checks/changed_files 必须是数组')
  }
  if (receipt.checks.length > 100 || receipt.changed_files.length > 100) {
    throw new Error('receipt checks/changed_files 超过 100 项')
  }
  for (const check of receipt.checks) {
    if (!check || typeof check !== 'object' || Array.isArray(check)) {
      throw new Error('receipt check 必须是对象')
    }
    if (typeof check.command !== 'string' || !check.command.trim()) {
      throw new Error('receipt check.command 为空')
    }
    if (!['passed', 'failed', 'not_run'].includes(check.status)) {
      throw new Error('receipt check.status 无效')
    }
    if (check.detail != null && typeof check.detail !== 'string') {
      throw new Error('receipt check.detail 必须是字符串')
    }
  }
  if (receipt.status === 'succeeded' &&
      (!receipt.checks.length || receipt.checks.some(function (check) { return check.status !== 'passed' }))) {
    throw new Error('succeeded receipt 必须至少有一项检查且全部 passed')
  }
  for (const changed of receipt.changed_files) {
    if (!validRelativePath(changed)) throw new Error('receipt changed_files 含非法路径')
    if (task && !pathWithin(changed, task.allowed_paths)) {
      throw new Error('receipt changed_files 超出 allowed_paths: ' + changed)
    }
    if (task && pathWithin(changed, task.forbidden_paths)) {
      throw new Error('receipt changed_files 命中 forbidden_paths: ' + changed)
    }
  }
  if (task) {
    if (task.git.commit) {
      if (typeof receipt.commit !== 'string' || !/^[0-9a-f]{7,64}$/i.test(receipt.commit)) {
        throw new Error('git.commit=true 但 receipt.commit 不是有效提交 id')
      }
    } else if (receipt.commit !== null) {
      throw new Error('git.commit=false 时 receipt.commit 必须为 null')
    }
    if (task.git.push) {
      if (!(receipt.push === true || (typeof receipt.push === 'string' && receipt.push.trim()))) {
        throw new Error('git.push=true 但 receipt.push 无有效回执')
      }
    } else if (receipt.push !== null) {
      throw new Error('git.push=false 时 receipt.push 必须为 null')
    }
  }
  return receipt
}

function sameStringSet(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false
  const a = left.map(String).sort()
  const b = right.map(String).sort()
  return a.every(function (value, index) { return value === b[index] })
}

function validateVerification(raw, state) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('verification 必须是 JSON 对象')
  }
  if (!state || state.status !== 'verification_required' || !state.receipt || !state.task) {
    throw new Error('任务当前不在 verification_required 状态')
  }
  if (state.receipt.status !== 'succeeded') {
    throw new Error('只有模型 reported succeeded 的任务可以独立验收')
  }
  if (!['verified_succeeded', 'verification_failed'].includes(raw.status)) {
    throw new Error('verification.status 无效')
  }
  const summary = String(raw.summary || '').trim()
  if (!summary || summary.length > 4000) throw new Error('verification.summary 必须为 1..4000 字符')
  if (!Array.isArray(raw.checks) || raw.checks.length < 1 || raw.checks.length > 100) {
    throw new Error('verification.checks 必须是 1..100 项数组')
  }
  const checks = raw.checks.map(function (check) {
    if (!check || typeof check !== 'object' || Array.isArray(check)) {
      throw new Error('verification check 必须是对象')
    }
    const command = String(check.command || '').trim()
    if (!command || command.length > 1000) throw new Error('verification check.command 无效')
    if (!['passed', 'failed', 'not_run'].includes(check.status)) {
      throw new Error('verification check.status 无效')
    }
    if (check.detail != null && typeof check.detail !== 'string') {
      throw new Error('verification check.detail 必须是字符串')
    }
    return { command: command, status: check.status, detail: check.detail == null ? '' : check.detail }
  })
  if (raw.status === 'verified_succeeded' && checks.some(function (check) { return check.status !== 'passed' })) {
    throw new Error('verified_succeeded 要求独立检查全部 passed')
  }
  if (!Array.isArray(raw.changed_files) || raw.changed_files.length > 100) {
    throw new Error('verification.changed_files 必须是数组')
  }
  const changedFiles = raw.changed_files.map(String)
  for (const changed of changedFiles) {
    if (!validRelativePath(changed)) throw new Error('verification.changed_files 含非法路径')
    if (!pathWithin(changed, state.task.allowed_paths)) {
      throw new Error('verification.changed_files 超出 allowed_paths: ' + changed)
    }
    if (pathWithin(changed, state.task.forbidden_paths)) {
      throw new Error('verification.changed_files 命中 forbidden_paths: ' + changed)
    }
  }
  if (!sameStringSet(changedFiles, state.receipt.changed_files)) {
    throw new Error('verification.changed_files 与模型 receipt 不一致')
  }
  if (state.task.git.commit) {
    if (typeof raw.commit !== 'string' || !/^[0-9a-f]{7,64}$/i.test(raw.commit)) {
      throw new Error('git.commit=true 但 verification.commit 无效')
    }
  } else if (raw.commit !== null) {
    throw new Error('git.commit=false 时 verification.commit 必须为 null')
  }
  if (state.task.git.push) {
    if (!(raw.push === true || (typeof raw.push === 'string' && raw.push.trim()))) {
      throw new Error('git.push=true 但 verification.push 无效')
    }
  } else if (raw.push !== null) {
    throw new Error('git.push=false 时 verification.push 必须为 null')
  }
  if (raw.commit !== state.receipt.commit || raw.push !== state.receipt.push) {
    throw new Error('verification commit/push 与模型 receipt 不一致')
  }
  return {
    status: raw.status,
    summary: summary,
    checks: checks,
    changed_files: changedFiles,
    commit: raw.commit,
    push: raw.push
  }
}

function install(options) {
  const ctx = options.ctx
  const agents = options.agents
  const sessionPersistence = options.sessionPersistence
  const apiProxy = options.apiProxy
  const protocol = options.protocol
  const receiptProtocol = options.receiptProtocol
  const projectRoot = path.resolve(options.projectRoot)
  const dshHome = path.resolve(options.dshHome)
  const taskLog = path.resolve(options.taskLog)
  const tokenFile = path.resolve(options.tokenFile)
  const webServer = options.webServer
  const maxActiveTasks = Math.max(1, Number(options.maxActiveTasks || 1))
  const identityOk = options.identityOk === true
  const identityError = String(options.identityError || '')
  const taskState = Object.create(null)

  if (!taskLog.startsWith(dshHome + path.sep) || !tokenFile.startsWith(dshHome + path.sep)) {
    throw new Error('task log/token 必须位于唯一 DSH_HOME 内')
  }

  function loadToken() {
    try {
      const token = fs.readFileSync(tokenFile, 'utf8').trim()
      return token.length >= 32 ? token : null
    } catch (error) { return null }
  }

  function authorized(req) {
    const expected = loadToken()
    const supplied = String((req.headers && req.headers['x-dshq-token']) || '')
    if (!expected || supplied.length !== expected.length) return false
    return crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))
  }

  function readLog() {
    if (!fs.existsSync(taskLog)) return
    const lines = fs.readFileSync(taskLog, 'utf8').split(/\r?\n/).filter(Boolean)
    for (const line of lines) {
      try {
        const record = JSON.parse(line)
        if (record && record.task_id && record.state) {
          const restored = record.state
          if (restored.status === 'succeeded' && restored.receipt) {
            restored.status = 'verification_required'
            restored.reported_status = 'succeeded'
          }
          taskState[record.task_id] = restored
        }
      } catch (error) {}
    }
  }

  function persist(state, event) {
    const next = Object.assign({}, state, { updated_at: new Date().toISOString() })
    taskState[next.task_id] = next
    fs.mkdirSync(path.dirname(taskLog), { recursive: true })
    fs.appendFileSync(taskLog, JSON.stringify({
      schema: 'dshq-task-log/v1',
      event: event,
      task_id: next.task_id,
      state: next
    }) + '\n', { encoding: 'utf8', mode: 0o600 })
    return next
  }

  function activeTasks(exceptId) {
    return Object.values(taskState).filter(function (state) {
      return state.task_id !== exceptId && ACTIVE_STATUSES.has(state.status)
    })
  }

  function taskSessionId(taskId) {
    return 'session-dshq-' + crypto.createHash('sha256').update(taskId).digest('hex').slice(0, 32)
  }

  async function ensureTaskAgent(state) {
    const sessionId = state.session_id
    const live = agents && agents.get(sessionId)
    if (live) return live
    if (!agents || !apiProxy || !apiProxy.sessions) throw new Error('agents/apiProxy.sessions 服务不可用')
    const response = await apiProxy.sessions.create({
      rpcId: crypto.randomUUID(),
      payload: { sessionId: sessionId, cwd: projectRoot }
    })
    if (!response || !response.result || !response.result.ok) {
      const detail = response && response.result && response.result.error
      throw new Error('task session create/resume failed: ' + String((detail && detail.message) || detail || 'unknown'))
    }
    const agent = agents.get(String(response.result.value.sessionId))
    if (!agent) throw new Error('task session accepted but live agent is unavailable')
    await agent.whenIdle()
    return agent
  }

  async function queuePrompt(sessionId, text) {
    const response = await apiProxy.sessions.prompt({
      rpcId: crypto.randomUUID(),
      payload: { sessionId: sessionId, mode: 'queue', content: [{ type: 'text', text: text }] }
    })
    if (!response || !response.result || !response.result.ok) {
      const detail = response && response.result && response.result.error
      throw new Error('task prompt rejected: ' + String((detail && detail.message) || detail || 'unknown'))
    }
  }

  function promptFor(task) {
    const receiptShape = {
      protocol: receiptProtocol,
      task_id: task.task_id,
      status: 'succeeded|failed|blocked',
      summary: '完成结果或阻塞原因',
      checks: [{ command: '实际执行的检查', status: 'passed|failed|not_run', detail: '摘要' }],
      changed_files: ['相对路径'],
      commit: null,
      push: null
    }
    return [
      '你收到的是 DSHQuant 的机器可验收任务合同。只执行合同范围，不扩张权限。',
      '必须按 ordered_steps 顺序工作；不得触碰 forbidden_paths；allowed_paths 之外只允许只读检查。',
      'git.commit/git.push 为 false 时严禁对应操作。验收失败不得宣称完成。',
      '任务合同：\n' + JSON.stringify(task, null, 2),
      '结束时在回复最后输出且只输出一份以下标签（标签内必须是严格 JSON）：',
      '<DSHQ_RECEIPT>' + JSON.stringify(receiptShape) + '</DSHQ_RECEIPT>'
    ].join('\n\n')
  }

  function receiptText(agent) {
    const events = agent && agent.session && Array.isArray(agent.session.events) ? agent.session.events : []
    const chunks = []
    for (const event of events) {
      if (event && event.surfaceOp && event.surfaceOp !== 'append') continue
      const data = event && event.data ? event.data : {}
      const value = data.message || data
      if (event && event.type === 'assistant/message') chunks.push(options.textOfContent(value.content))
    }
    return chunks.join('\n')
  }

  async function monitor(state, agent) {
    try {
      await agent.whenIdle()
      if (sessionPersistence && typeof sessionPersistence.flush === 'function') {
        await sessionPersistence.flush(agent.session)
      }
      const receipt = parseReceipt(receiptText(agent), state.task, receiptProtocol)
      const reportedSuccess = receipt.status === 'succeeded'
      persist(Object.assign({}, taskState[state.task_id], {
        status: reportedSuccess ? 'verification_required' : receipt.status,
        reported_status: receipt.status,
        receipt: receipt,
        finished_at: new Date().toISOString()
      }), reportedSuccess ? 'verification_required' : 'receipt')
    } catch (error) {
      persist(Object.assign({}, taskState[state.task_id], {
        status: 'protocol_error',
        error: String((error && error.message) || error).slice(0, 1000),
        finished_at: new Date().toISOString()
      }), 'protocol_error')
    }
  }

  async function startTask(state, prompt) {
    try {
      const agent = await ensureTaskAgent(state)
      state = persist(Object.assign({}, state, { status: 'running', started_at: new Date().toISOString() }), 'running')
      await queuePrompt(state.session_id, prompt)
      void monitor(state, agent)
    } catch (error) {
      persist(Object.assign({}, state, {
        status: 'failed',
        error: String((error && error.message) || error).slice(0, 1000),
        finished_at: new Date().toISOString()
      }), 'start_failed')
    }
  }

  function requireMutation(req, res) {
    if (!identityOk) {
      options.json(res, 503, { ok: false, error: 'HARNESS identity mismatch', identity_error: identityError })
      return false
    }
    if (authorized(req)) return true
    options.json(res, loadToken() ? 401 : 503, {
      ok: false,
      error: loadToken() ? 'invalid bridge token' : 'bridge token unavailable'
    })
    return false
  }

  if (identityOk) readLog()
  const routes = []
  routes.push(webServer.register({
    kind: 'exact', path: '/quant/health',
    handler: async function (req, res) {
      options.cors(res)
      if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
      options.json(res, 200, {
        ok: true,
        ready: Boolean(identityOk && loadToken() && agents && apiProxy && apiProxy.sessions),
        protocol: protocol,
        receipt_protocol: receiptProtocol,
        dsh_home: dshHome,
        project_root: projectRoot,
        home_fingerprint: options.homeFingerprint,
        home_matches_project: identityOk,
        identity_ok: identityOk,
        identity_error: identityOk ? null : identityError,
        active_tasks: activeTasks().length,
        max_active_tasks: maxActiveTasks,
        mutation_auth: 'local-token',
        ts: Date.now()
      })
    }
  }))
  routes.push(webServer.register({
    kind: 'exact', path: '/quant/tasks',
    handler: async function (req, res) {
      options.cors(res)
      if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
      if (req.method === 'GET') {
        const q = options.parseQuery(req.url)
        const limit = Math.max(1, Math.min(100, parseInt(q.limit || '20', 10) || 20))
        const tasks = Object.values(taskState).sort(function (a, b) {
          return String(b.updated_at).localeCompare(String(a.updated_at))
        }).slice(0, limit)
        options.json(res, 200, { ok: true, protocol: protocol, tasks: tasks })
        return
      }
      if (req.method !== 'POST') { options.json(res, 405, { ok: false, error: 'method not allowed' }); return }
      if (!requireMutation(req, res)) return
      try {
        const task = validateTask(await options.readJson(req), { protocol: protocol })
        const hash = payloadHash(task)
        const existing = taskState[task.task_id]
        if (existing) {
          if (existing.payload_hash !== hash) {
            options.json(res, 409, { ok: false, error: 'task_id payload conflict', task_id: task.task_id })
            return
          }
          options.json(res, 200, { ok: true, idempotent: true, task: existing })
          return
        }
        if (activeTasks().length >= maxActiveTasks) {
          options.json(res, 409, { ok: false, error: 'another task is active', active: activeTasks() })
          return
        }
        const now = new Date().toISOString()
        const state = persist({
          task_id: task.task_id,
          payload_hash: hash,
          status: 'accepted',
          task: task,
          session_id: taskSessionId(task.task_id),
          accepted_at: now,
          updated_at: now
        }, 'accepted')
        void startTask(state, promptFor(task))
        options.json(res, 202, { ok: true, accepted: true, task: state })
      } catch (error) {
        options.json(res, 400, { ok: false, error: String((error && error.message) || error).slice(0, 1000) })
      }
    }
  }))
  routes.push(webServer.register({
    // dsh-host-webserver prefix paths must not end in '/'. The exact route
    // above still owns /quant/tasks; this route owns /quant/tasks/<id>.
    kind: 'prefix', path: '/quant/tasks',
    handler: async function (req, res) {
      options.cors(res)
      if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
      const pathname = String(req.url || '').split('?')[0]
      const suffix = pathname.slice('/quant/tasks/'.length)
      const followup = suffix.endsWith('/followup')
      const verify = suffix.endsWith('/verify')
      const operationSuffix = followup ? '/followup' : (verify ? '/verify' : '')
      const taskId = decodeURIComponent(operationSuffix ? suffix.slice(0, -operationSuffix.length) : suffix)
      if (!TASK_ID.test(taskId)) { options.json(res, 400, { ok: false, error: 'invalid task id' }); return }
      if (operationSuffix && req.method === 'POST' && !requireMutation(req, res)) return
      const state = taskState[taskId]
      if (!state) { options.json(res, 404, { ok: false, error: 'task not found', task_id: taskId }); return }
      if (!operationSuffix && req.method === 'GET') { options.json(res, 200, { ok: true, task: state }); return }
      if (!operationSuffix || req.method !== 'POST') { options.json(res, 405, { ok: false, error: 'method not allowed' }); return }
      try {
        const body = await options.readJson(req)
        if (verify) {
          const verification = validateVerification(body, state)
          const verified = persist(Object.assign({}, state, {
            status: verification.status,
            verification: verification,
            verified_at: new Date().toISOString(),
            verifier: 'codex-local'
          }), verification.status)
          options.json(res, 200, { ok: true, verified: verification.status === 'verified_succeeded', task: verified })
          return
        }
        const text = String(body.text || '').trim()
        if (!text || text.length > 4000) throw new Error('followup text 必须为 1..4000 字符')
        if (AWAITING_VERIFICATION.has(state.status)) {
          options.json(res, 409, { ok: false, error: 'task requires independent verification', status: state.status })
          return
        }
        if (FINAL_STATUSES.has(state.status) && state.status !== 'blocked') {
          options.json(res, 409, { ok: false, error: 'task is final', status: state.status })
          return
        }
        if (state.status === 'blocked' && activeTasks(taskId).length >= maxActiveTasks) {
          options.json(res, 409, { ok: false, error: 'another task is active' })
          return
        }
        const agent = await ensureTaskAgent(state)
        const next = persist(Object.assign({}, state, {
          status: 'running',
          receipt: null,
          followups: (state.followups || []).concat([{ text: text, at: new Date().toISOString() }])
        }), 'followup')
        await queuePrompt(state.session_id, '任务 ' + taskId + ' 的受控补充说明：\n' + text + '\n仍须按原合同输出 DSHQ_RECEIPT。')
        if (state.status === 'blocked') void monitor(next, agent)
        options.json(res, 202, { ok: true, accepted: true, task: next })
      } catch (error) {
        options.json(res, 400, { ok: false, error: String((error && error.message) || error).slice(0, 1000) })
      }
    }
  }))

  return function dispose() {
    for (const route of routes.reverse()) {
      try { route() } catch (error) {}
    }
  }
}

module.exports = { install, parseReceipt, payloadHash, validateTask, validateVerification, validRelativePath }
