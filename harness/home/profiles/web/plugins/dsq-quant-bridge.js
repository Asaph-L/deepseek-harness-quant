// DSHQuant ↔ DeepSeek HARNESS 唯一桥接插件。
// /quant/* 同时承载受控派单（含 task 子路由）、会话与配置驱动的牛散 persona API。
// 部署：由 DSH_HOME/profiles/web/cordis.patch.yml 挂载为宿主组合插件行
// inject：声明硬依赖，cordis 在 webServer 等服务就绪后才 apply（避免提前 return 导致路由不注册）
module.exports = {
  inject: ['webServer', 'sessions', 'agents', 'subagents', 'sessionTitle', 'sessionPersistence', 'apiProxy'],
  apply(ctx) {
    const crypto = require('crypto')
    const fs = require('fs')
    const path = require('path')
    const yaml = require('yaml')
    // The host HMR watches this composition plugin, while Node otherwise keeps
    // its protocol child in require.cache. Reload the child on every apply so
    // protocol fixes become live without restarting the QuantDeck launcher.
    const taskProtocolPath = require.resolve('./dshq-task-protocol.js')
    delete require.cache[taskProtocolPath]
    const taskProtocol = require(taskProtocolPath)
    const webServer = ctx.get('webServer')
    const sessions = ctx.get('sessions')
    const agents = ctx.get('agents')
    const sessionTitle = ctx.get('sessionTitle')
    const subagents = ctx.get('subagents')
    const sessionPersistence = ctx.get('sessionPersistence')
    const apiProxy = ctx.get('apiProxy')
    if (webServer === undefined) return

    const projectRootEnv = String(process.env.DSHQ_PROJECT_ROOT || '').trim()
    const dshHomeEnv = String(process.env.DSH_HOME || '').trim()
    const projectRoot = path.resolve(projectRootEnv || process.cwd())
    const dshHome = path.resolve(dshHomeEnv || path.join(projectRoot, 'harness', 'home'))
    let identityOk = false
    let identityError = ''
    try {
      if (!projectRootEnv) throw new Error('DSHQ_PROJECT_ROOT is required')
      if (!dshHomeEnv) throw new Error('DSH_HOME is required')
      const realpath = fs.realpathSync.native || fs.realpathSync
      const realProjectRoot = realpath(projectRoot)
      const realDshHome = realpath(dshHome)
      const expectedHome = realpath(path.join(realProjectRoot, 'harness', 'home'))
      if (realDshHome !== expectedHome) throw new Error('DSH_HOME must equal <DSHQ_PROJECT_ROOT>/harness/home')
      identityOk = true
    } catch (error) {
      identityError = String((error && error.message) || error).slice(0, 500)
    }
    const protocol = process.env.DSHQ_BRIDGE_PROTOCOL || 'dshq-task/v1'
    const receiptProtocol = process.env.DSHQ_BRIDGE_RECEIPT_PROTOCOL || 'dshq-task-receipt/v1'
    const tokenFile = path.resolve(process.env.DSHQ_BRIDGE_TOKEN_FILE || path.join(dshHome, 'quant-bridge', 'token'))
    const taskLog = path.resolve(process.env.DSHQ_BRIDGE_TASK_LOG || path.join(dshHome, 'quant-bridge', 'tasks.jsonl'))
    const maxBodyBytes = Math.max(1024, Number(process.env.DSHQ_BRIDGE_MAX_BODY_BYTES || 262144))
    const maxActiveTasks = Math.max(1, Number(process.env.DSHQ_BRIDGE_MAX_ACTIVE_TASKS || 1))
    const allowedOrigins = new Set(String(process.env.DSHQ_BRIDGE_ALLOWED_ORIGINS || 'http://127.0.0.1:8787,http://localhost:8787')
      .split(',').map(function (value) { return value.trim().replace(/\/$/, '') }).filter(Boolean))
    const homeFingerprint = process.env.DSHQ_HOME_FINGERPRINT || crypto.createHash('sha256')
      .update(projectRoot + '\0' + dshHome + '\0' + protocol).digest('hex').slice(0, 16)

    function cors(res) {
      const origin = res.req && res.req.headers ? String(res.req.headers.origin || '').replace(/\/$/, '') : ''
      if (allowedOrigins.has(origin)) res.setHeader('Access-Control-Allow-Origin', origin)
      res.setHeader('Vary', 'Origin')
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-DSHQ-Token')
      res.setHeader('Access-Control-Max-Age', '600')
    }
    function json(res, code, obj) {
      res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify(obj))
    }
    async function readBody(req) {
      const chunks = []
      let size = 0
      for await (const chunk of req) {
        const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
        size += value.length
        if (size > maxBodyBytes) throw new Error('request body too large')
        chunks.push(value)
      }
      return Buffer.concat(chunks).toString('utf8')
    }
    async function readJson(req) {
      return JSON.parse((await readBody(req)) || '{}')
    }
    function bridgeToken() {
      try {
        const token = fs.readFileSync(tokenFile, 'utf8').trim()
        return token.length >= 32 ? token : null
      } catch (e) { return null }
    }
    function authorizeMutation(req, res) {
      if (!identityOk) {
        json(res, 503, { ok: false, error: 'HARNESS identity mismatch', identity_error: identityError })
        return false
      }
      const expected = bridgeToken()
      const supplied = String((req.headers && req.headers['x-dshq-token']) || '')
      const ok = expected && supplied.length === expected.length &&
        crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))
      if (ok) return true
      json(res, expected ? 401 : 503, { ok: false, error: expected ? 'invalid bridge token' : 'bridge token unavailable' })
      return false
    }
    function parseQuery(url) {
      const out = {}
      const qs = String(url || '').split('?')[1]
      if (!qs) return out
      for (const pair of qs.split('&')) {
        const i = pair.indexOf('=')
        if (i < 0) continue
        try { out[decodeURIComponent(pair.slice(0, i))] = decodeURIComponent(pair.slice(i + 1)) } catch (e) {}
      }
      return out
    }
    function defaultAgentId() {
      if (!agents) return null
      const roots = agents.roots()
      return roots[0] ? String(roots[0].id) : null
    }
    function resolveAgent(sessionId) {
      if (!agents) return null
      const id = sessionId || defaultAgentId()
      if (!id) return null
      return agents.get(id) || agents.roots().find(function (a) { return String(a.id) === id }) || null
    }
    function resolveSession(id) {
      if (sessions) {
        const s = sessions.get(id)
        if (s) return s
      }
      const agent = resolveAgent(id)
      if (agent && agent.session) return agent.session
      return null
    }
    function textOfContent(content) {
      if (typeof content === 'string') return content
      if (!Array.isArray(content)) return ''
      return content.map(function (p) {
        if (p == null) return ''
        if (typeof p === 'string') return p
        if (p.type === 'text') return String(p.text || '')
        return ''
      }).filter(Boolean).join('\n')
    }
    function isNoise(text) {
      const t = String(text || '').trim()
      if (!t) return true
      if (t.indexOf('Current runtime context. This snapshot supersedes') === 0) return true
      if (t.indexOf('【任务】你是「') === 0) return true
      return false
    }
    function extractMessages(eventsArr, limit) {
      const out = []
      const arr = Array.isArray(eventsArr) ? eventsArr : []
      for (let i = arr.length - 1; i >= 0 && out.length < (limit || 20); i--) {
        const ev = arr[i]
        if (!ev) continue
        let role = null, content = null, ts = null
        if (ev.type === 'user/message' || ev.type === 'assistant/message') {
          const d = ev.data || {}
          const m = d.message && d.message.role ? d.message : d
          role = ev.type === 'user/message' ? 'user' : 'assistant'
          content = m.content
          ts = ev.time != null ? ev.time : (d.ts != null ? d.ts : null)
        } else {
          const msg = ev.message || ev
          role = msg && msg.role
          content = msg && msg.content
          ts = (ev.ts != null ? ev.ts : (msg && msg.ts != null ? msg.ts : null))
        }
        if (role !== 'user' && role !== 'assistant') continue
        const text = textOfContent(content)
        if (isNoise(text)) continue
        out.unshift({ role: role, text: text.slice(0, 4000), ts: ts })
      }
      return out
    }
    function liveMessages(session, limit) {
      let messages = []
      if (typeof session.deriveMessages === 'function') messages = session.deriveMessages()
      else if (session.events) messages = session.events
      return extractMessages(messages, limit)
    }
    async function coldMessages(id, limit) {
      const debug = { tried: false, events: 0, error: null }
      if (!sessionPersistence || typeof sessionPersistence.readFrom !== 'function') {
        debug.error = 'no sessionPersistence'
        return { messages: null, debug: debug }
      }
      try {
        debug.tried = true
        const r = await sessionPersistence.readFrom(id, 0, fakeSignal())
        if (r && Array.isArray(r.events)) {
          debug.events = r.events.length
          return { messages: extractMessages(r.events, limit), debug: debug }
        }
        debug.error = 'no events array'
        return { messages: null, debug: debug }
      } catch (e) {
        debug.error = String((e && e.message) || e).slice(0, 200)
        return { messages: null, debug: debug }
      }
    }
    async function messagesOf(id, limit) {
      const session = resolveSession(id)
      if (session) {
        const live = liveMessages(session, limit)
        if (live.length) return { messages: live, debug: { live: true, count: live.length } }
      }
      const c = await coldMessages(id, limit)
      return { messages: c.messages || [], debug: c.debug }
    }
    function recentTextOf(list) {
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i] && list[i].text) return { text: list[i].text.slice(0, 120), role: list[i].role }
      }
      return null
    }
    function sessionTitleOf(id) {
      try {
        const session = resolveSession(id)
        if (!session) return null
        if (sessionTitle && typeof sessionTitle.get === 'function') {
          const st = sessionTitle.get(session)
          if (st && st.title) return st.title
        }
        const meta = session.meta || session.header || {}
        if (meta.title) return meta.title
        const cwd = meta.cwd
        if (cwd) {
          const parts = String(cwd).replace(/\\/g, '/').split('/').filter(Boolean)
          return parts[parts.length - 1] || null
        }
      } catch (e) {}
      return null
    }

    function loadPersonaConfig() {
      const local = path.join(projectRoot, 'config', 'niu_personas.yaml')
      const example = path.join(projectRoot, 'config', 'niu_personas.yaml.example')
      const configPath = fs.existsSync(local) ? local : example
      const raw = yaml.parse(fs.readFileSync(configPath, 'utf8')) || {}
      const cfg = raw.personas || {}
      if (!cfg.prompt_template || !cfg.entries || typeof cfg.entries !== 'object') {
        throw new Error('niu_personas 配置缺少 prompt_template/entries')
      }
      const entries = {}
      for (const key of Object.keys(cfg.entries)) {
        const value = cfg.entries[key] || {}
        if (!value.name || !value.tag || !value.skill || !value.style) throw new Error('persona 配置不完整: ' + key)
        entries[key] = Object.assign({ key: key }, value)
      }
      return { promptTemplate: String(cfg.prompt_template), entries: entries }
    }
    // Identity failures must still expose /quant/health and fail POST with 503;
    // do not let a bogus project root crash before the guarded routes register.
    const personaConfig = identityOk ? loadPersonaConfig() : { promptTemplate: '', entries: {} }
    const PERSONAS = personaConfig.entries
    function buildPersona(p) {
      return personaConfig.promptTemplate
        .split('{name}').join(String(p.name))
        .split('{skill}').join(String(p.skill))
        .split('{style}').join(String(p.style))
    }

    const personaChild = {}
    const personaSnap = {}
    const personaHinted = {}
    const niuBootLock = {}

    function fakeSignal() {
      return { aborted: false, reason: undefined, onabort: null, addEventListener: function () {}, removeEventListener: function () {}, dispatchEvent: function () { return true }, throwIfAborted: function () {} }
    }

    ctx.effect(() => taskProtocol.install({
      ctx: ctx,
      webServer: webServer,
      agents: agents,
      sessionPersistence: sessionPersistence,
      apiProxy: apiProxy,
      protocol: protocol,
      receiptProtocol: receiptProtocol,
      projectRoot: projectRoot,
      dshHome: dshHome,
      taskLog: taskLog,
      tokenFile: tokenFile,
      maxActiveTasks: maxActiveTasks,
      homeFingerprint: homeFingerprint,
      identityOk: identityOk,
      identityError: identityError,
      cors: cors,
      json: json,
      parseQuery: parseQuery,
      readJson: readJson,
      textOfContent: textOfContent
    }))
    async function discoverNiuChildren() {
      const out = {}
      if (!subagents || !agents) return out
      const root = defaultAgentId()
      if (!root) return out
      let entries = []
      try { entries = await subagents.listChildren(root) } catch (e) { entries = [] }
      for (const e of entries) {
        if (!e || e.kind !== 'child') continue
        const id = e.id ? String(e.id) : null
        const label = e.label || ''
        if (!id || label.indexOf('牛散·') !== 0) continue
        out[label.slice(3)] = id
      }
      return out
    }
    function ensureNiuChild(key, snapshot) {
      if (personaChild[key]) return Promise.resolve(personaChild[key])
      if (niuBootLock[key]) return niuBootLock[key]
      niuBootLock[key] = (async function () {
        try {
          const found = await discoverNiuChildren()
          if (found[key]) {
            personaChild[key] = found[key]
            if (snapshot) personaSnap[key] = snapshot
            return personaChild[key]
          }
          const p = PERSONAS[key]
          const rootId = defaultAgentId()
          const rootAgent = rootId ? agents.get(rootId) : null
          if (!subagents || !rootAgent) return null
          const firstMsg = '【任务】你是「' + p.name + '」主观选股顾问（身份与选股规则已注入你的系统设定）。'
            + (snapshot ? '\n\n当前量化 Pitch 快照：\n' + snapshot + '\n' : '')
            + '\n\n请先一句话点出你的选股风格要点（确认就位），然后直接给出第一轮选股意见。'
          const spec = {
            provider: 'spawn',
            label: '牛散·' + key,
            request: {
              prompt: [{ type: 'text', text: firstMsg }],
              parent: rootAgent,
              persona: buildPersona(p),
              toolFilter: { allow: [] }
            },
            signal: fakeSignal()
          }
          const out = await subagents.startContinuable(spec)
          personaChild[key] = String(out && out.childId)
          personaHinted[key] = true
          if (snapshot) personaSnap[key] = snapshot
          return personaChild[key]
        } catch (e) {
          console.error('niu boot ' + key + ': ' + String((e && e.message) || e))
          return null
        } finally {
          delete niuBootLock[key]
        }
      })()
      return niuBootLock[key]
    }

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/sessions',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        try {
          const roots = agents ? agents.roots() : []
          const list = []
          for (let idx = 0; idx < roots.length; idx++) {
            const a = roots[idx]
            const id = a && a.id ? String(a.id) : null
            if (!id) continue
            const msgs = await messagesOf(id, 3)
            const last = recentTextOf(msgs.messages)
            list.push({
              id: id,
              title: sessionTitleOf(id) || ('会话 ' + (idx + 1)),
              preview: last ? last.text : '',
              role: last ? last.role : '',
              ts: Date.now(),
              current: idx === 0
            })
          }
          json(res, 200, { ok: true, sessions: list })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/niu/sessions',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        try {
          const found = await discoverNiuChildren()
          const catalog = []
          for (const k of Object.keys(PERSONAS)) {
            const p = PERSONAS[k]
            const childId = personaChild[k] || found[k] || null
            let preview = ''
            if (childId) {
              const msgs = await messagesOf(childId, 3)
              const last = recentTextOf(msgs.messages)
              preview = last ? last.text : ''
            }
            catalog.push({ id: k, name: p.name, tag: p.tag, skill: p.skill, childId: childId, preview: preview })
          }
          json(res, 200, { ok: true, personas: catalog })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/niu/chat',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        if (req.method !== 'POST') { json(res, 405, { ok: false, error: 'method not allowed' }); return }
        if (!authorizeMutation(req, res)) return
        let key = '', text = '', snapshot = ''
        try {
          const payload = JSON.parse((await readBody(req)) || '{}')
          key = String(payload.persona || '').trim()
          text = String(payload.text || '').trim()
          if (payload.snapshot) snapshot = String(payload.snapshot)
        } catch (e) { json(res, 400, { ok: false, error: 'bad json' }); return }
        const p = PERSONAS[key]
        if (!p) { json(res, 404, { ok: false, error: 'unknown persona: ' + key }); return }
        if (!text) { json(res, 400, { ok: false, error: 'empty text' }); return }
        try {
          const existed = !!personaChild[key]
          const childId = await ensureNiuChild(key, snapshot || personaSnap[key] || '')
          if (!childId) { json(res, 500, { ok: false, error: '牛散子代理创建失败（subagents 不可用？）', persona: key }); return }
          const rootId = defaultAgentId()
          const rootAgent = rootId ? agents.get(rootId) : null
          if (!rootAgent || !subagents) { json(res, 500, { ok: false, error: 'agents/subagents 不可用' }); return }
          let content = text
          if (existed) {
            if (snapshot) personaSnap[key] = snapshot
            const extra = personaSnap[key] ? '\n\n【量化 Pitch 快照（最新）】\n' + personaSnap[key] : ''
            if (!personaHinted[key]) {
              personaHinted[key] = true
              extra += '\n\n（系统提醒：请在你的回复最后一行输出纯 JSON 决策对象 {"niu_decisions":[...]}，action 限 buy/hold/sell/watch）'
            }
            content = text + extra
          }
          await subagents.followup(rootAgent, childId, [{ type: 'text', text: content }],
            { source: { kind: 'user' }, signal: fakeSignal() })
          json(res, 200, { ok: true, accepted: true, persona: key, childId: childId })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/niu/recent',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        try {
          const q = parseQuery(req.url)
          const key = String(q.persona || '').trim()
          if (!PERSONAS[key]) { json(res, 404, { ok: false, error: 'unknown persona: ' + key }); return }
          let childId = personaChild[key]
          if (!childId) {
            const found = await discoverNiuChildren()
            childId = found[key] || null
            if (childId) personaChild[key] = childId
          }
          if (!childId) { json(res, 200, { ok: true, persona: key, childId: null, messages: [], debug: { child: null } }); return }
          const limit = Math.max(1, Math.min(50, parseInt(q.limit || '20', 10) || 20))
          const r = await messagesOf(childId, limit)
          json(res, 200, { ok: true, persona: key, childId: childId, messages: r.messages, debug: r.debug })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/chat2',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        if (req.method !== 'POST') { json(res, 405, { ok: false, error: 'method not allowed' }); return }
        if (!authorizeMutation(req, res)) return
        let text = '', sessionId = null
        try {
          const payload = JSON.parse((await readBody(req)) || '{}')
          text = String(payload.text || '').trim()
          if (payload.sessionId) sessionId = String(payload.sessionId)
        } catch (e) { json(res, 400, { ok: false, error: 'bad json' }); return }
        if (!text) { json(res, 400, { ok: false, error: 'empty text' }); return }
        try {
          const agent = resolveAgent(sessionId)
          if (!agent) { json(res, 404, { ok: false, error: 'no agent', sessionId: sessionId || defaultAgentId() }); return }
          const message = {
            id: require('crypto').randomUUID(),
            role: 'user',
            content: [{ type: 'text', text }],
            source: { kind: 'user' }
          }
          if (typeof agent.followup === 'function') agent.followup(message)
          else if (typeof agent.steer === 'function') agent.steer(message)
          else { json(res, 500, { ok: false, error: 'no followup/steer' }); return }
          json(res, 200, { ok: true, accepted: true, sessionId: String(agent.id) })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/recent',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        try {
          const q = parseQuery(req.url)
          const sessionId = q.sessionId || defaultAgentId()
          const limit = Math.max(1, Math.min(50, parseInt(q.limit || '20', 10) || 20))
          const r = await messagesOf(sessionId, limit)
          json(res, 200, { ok: true, sessionId: sessionId, messages: r.messages })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))

    ctx.effect(() => webServer.register({
      kind: 'exact', path: '/quant/agents',
      handler: async (req, res) => {
        cors(res)
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return }
        try {
          const roots = agents ? agents.roots() : []
          json(res, 200, { ok: true, rootIds: roots.map(function (a) { return a && a.id ? String(a.id) : null }).filter(Boolean), ts: Date.now() })
        } catch (e) { json(res, 500, { ok: false, error: String((e && e.message) || e).slice(0, 200) }) }
      }
    }))
  },
}
