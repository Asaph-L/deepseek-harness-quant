/* deck/chain_status.js — 决策链状态条（2026-08-11 百轮#16）
 * 用法：<div class="chain-status" data-hide=""></div><script src="/chain_status.js"></script>
 * 一条链看全系统：观察池 → 择时 → 信号 → 机会池 → Pitch 长短线 → 持仓 → 止盈 → 风控 → 远期池
 * 每环节 chip：🟢 新鲜（<24h） / 🟡 旧（24-48h） / 🔴 缺失或 >48h
 */
(function () {
  var CN = {
    观察池: 'watch', 新择时: 'timing', 今日信号: 'signal', 竞价信号: 'auction', 机会池: 'opp',
    'Pitch 长线': 'pitch', 'Pitch 短线': 'tech', 持仓: 'hold', 止盈引擎: 'tp',
    风控: 'risk', 分钟数据: 'min', 远期池: 'track', 实盘裁决: 'verdict'
  };
  function mount() {
    var boxes = document.querySelectorAll('.chain-status');
    if (!boxes.length) return;
    fetch('/api/live/chain', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.chain) return;
        var html = '<div class="chain-label">🔗 决策链数据状态</div><div class="chain-nodes">';
        d.chain.forEach(function (n) {
          var cls = !n.ok ? 'ch-bad' : (n.age_h != null && n.age_h < 24 ? 'ch-ok' : 'ch-old');
          var age = n.age_h != null ? (n.age_h < 1 ? Math.round(n.age_h * 60) + '分' : n.age_h + 'h') : '缺失';
          var label = (CN[n.name] || n.name);
          html += '<span class="chain-chip ' + cls + '" title="' + (n.file || '无文件') + '">' +
            label + ' <b>' + age + '</b>' +
            (n.note ? '<span style="opacity:.85;font-weight:700;color:' + (n.down_n ? '#C0392B' : '#0F6E56') + '"> ' + n.note + '</span>' : '') +
            '</span>';
        });
        html += '</div>';
        boxes.forEach(function (b) { b.innerHTML = html; });
      })
      .catch(function () {});
  }
  // ★2026-08-11 百轮#41：全局预警横幅（止盈触发/止损临近/择时不适合/风控告警/数据断链）
  function mountAlerts() {
    var boxes = document.querySelectorAll('.alert-status');
    if (!boxes.length) return;
    fetch('/api/live/alerts', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var als = d.alerts || [];
        var html;
        if (!als.length) {
          html = '<div class="alert-ok">✅ 无预警——全系统运行正常</div>';
        } else {
          var high = d.n_high || 0;
          html = '<div class="alert-title ' + (high ? 'alert-title-high' : '') + '">🚨 预警 ' +
            (high ? '<b>' + high + '</b> 条高优先' : '') +
            (d.n_mid ? ' · ' + d.n_mid + ' 条提醒' : '') + '</div><div class="alert-items">';
          als.forEach(function (a) {
            html += '<span class="alert-item ' + (a.level === 'high' ? 'alert-high' : 'alert-mid') +
              '" title="' + a.msg + '">[' + a.cat + '] ' + a.msg + '</span>';
          });
          html += '</div>';
        }
        boxes.forEach(function (b) { b.innerHTML = html; });
      })
      .catch(function () {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { mount(); mountAlerts(); });
  } else {
    mount(); mountAlerts();
  }
  // 5 分钟刷新一次
  setInterval(mount, 600000);
  setInterval(mountAlerts, 300000);
})();
