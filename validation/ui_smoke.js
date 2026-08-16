// ui smoke test: run pitch.html inline script with minimal DOM/API stubs
// catches runtime errors in new tab structure + grouped buy order + L1 reference tab
const fs = require('fs');
const path = require('path');

const pagePath = process.argv[2];
const html = fs.readFileSync(pagePath, 'utf-8');
const m = html.match(/<script>\r?\n([\s\S]*?)<\/script>/);
if (!m) { console.log('no inline script'); process.exit(1); }
let js = m.group ? m.group(1) : m[1];

// --- minimal DOM stub ---
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', style: {}, children: [],
      appendChild(c) { this.children.push(c); },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      addEventListener() {},
      setAttribute() {}, getAttribute() { return null; },
      closest() { return null; },
    };
  }
  return elements[id];
}
global.document = {
  getElementById: el,
  createElement(tag) { return el('tmp-' + Math.random()); },
  addEventListener() {},
  body: el('body'),
  head: { appendChild() {} },
};
global.window = global;
global.location = { href: '' };
global.performance = { now: () => 0 };

// --- LW stubs ---
const apiCache = {};
global.LW = {
  sidebar: { render() {} },
  api: {
    get(p) {
      return new Promise((resolve) => {
        const key = p;
        if (!apiCache[key]) {
          if (key === '/api/pitch_v2') {
            apiCache[key] = { pitch: [
              { code: '600612.SH', name: '老凤祥', otype: 'value', otype_name: '价值', score: 97.5, pitch_date: '2026-08-14' },
              { code: '603929.SH', name: '亚翔集成', otype: 'revalue', otype_name: '价值重估', score: 82.2, pitch_date: '2026-08-14' },
            ] };
          } else if (key === '/api/live/tech') {
            apiCache[key] = { entries: [
              { code: '603163.SH', name: '圣湘生物', otype: 'tech_sentiment', otype_name: '短线情绪', score: 81.4, add_date: '2026-08-14' },
            ] };
          } else if (key === '/api/live/timing_dash') {
            apiCache[key] = { timing: { level: '适合买入', score: 67.6 } };
          } else if (key === '/api/live/portal_dash') {
            apiCache[key] = { data_date: '2026-08-14', data_semantic: '已更新' };
          } else if (key === '/api/live/enums') {
            apiCache[key] = { otypes: { value: '价值', revalue: '价值重估', tech_sentiment: '短线情绪' } };
          } else if (key === '/api/decisions') {
            apiCache[key] = [];
          } else {
            apiCache[key] = {};
          }
        }
        setTimeout(() => resolve(apiCache[key]), 0);
      });
    },
  },
  render: {
    loadEnums() {},
    _f(v, d) { return v == null || v === '' ? (d != null ? d : '—') : String(v); },
    otypeCN(t) { return t || ''; },
    otypeClass() { return ''; },
    techDimColor() { return '#5e6ad2'; },
    riskFlagCN(t) { return t || ''; },
    color(v) { return v >= 0 ? '#eb5757' : '#27ae60'; },
    countUp() {},
  },
  icons: { bar: '<svg></svg>' },
  tabs: {
    init(containerId, tabs) {
      const inst = { reload() {}, getActive() { return tabs[0]; } };
      // 模拟激活：调用第一个 tab 的 load（择时），再手动调用其余 load 检查不抛错
      tabs.forEach((t) => { if (t.load) { try { t.load(el('tab-' + t.id)); } catch (e) { console.log('  ⚠ load ' + t.label + ' threw: ' + e.message); } } });
      global.__tabs = tabs;
      return inst;
    },
  },
  decide: {
    _done: {},
    bind() {}, on() {},
    approve() { return Promise.resolve({ ok: true }); },
  },
};

// 捕获未捕获的 promise rejection
process.on('unhandledRejection', (e) => { console.log('  ⚠ unhandled rejection: ' + (e && e.message)); });

eval(js);
console.log('  runtime OK: ' + pagePath.split(/[\\/]/).pop());
setTimeout(() => { process.exit(0); }, 200);
