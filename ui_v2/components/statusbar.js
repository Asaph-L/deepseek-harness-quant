/* ui_v2/components/statusbar.js — 顶栏状态条（UI v2 标准 #20 逻辑层级）
   品牌条：Deepseek HARNESS Quant + 数据日 + 决策链 + 择时（架构修订v2 意见7：品牌条极简）
   数据源：/api/live/portal_dash（门户 1 API 聚合——#163）或逐 API 兜底
   所有 v2 页面共用——逻辑层级第一级（状态条  摘要 KPI  主内容  折叠详情）*/
(function (win) {
  'use strict';

  function renderInto(containerId) {
    var box = document.getElementById(containerId);
    if (!box) return;
    box.innerHTML =
      '<div class="v2-statusbar" style="display:flex;align-items:center;gap:14px;padding:10px 18px;' +
      'background:linear-gradient(120deg,#4c5bd6,#7c5cf0);color:#ffffff;border-radius:0 0 12px 12px;font-size:13px">' +
      '<b style="font-size:16px;font-weight:800;letter-spacing:.5px;white-space:nowrap">Deepseek HARNESS <span style="opacity:.82">Quant</span></b>' +
      '<span class="mini" id="sb-date" style="opacity:.75">数据日 …</span>' +
      '<span class="mini" id="sb-chain" style="opacity:.75">决策链 …</span>' +
      '<span class="mini" id="sb-timing" style="opacity:.75">择时 …</span>' +
      '<span style="flex:1"></span>' +
      '<span class="anim-pulse-ring" title="实时刷新中"></span>' +
      '<span class="mini" style="opacity:.7">实时</span>' +
      '<a href="/" style="color:#ffffff;opacity:.8;text-decoration:none;margin-left:6px">门户</a>' +
      '</div>';
    // 拉聚合数据（门户 1 API）
    if (win.LW && win.LW.api) {
      win.LW.api.get('/api/live/portal_dash').then(function (d) {
        var s = document.getElementById('sb-date'), c = document.getElementById('sb-chain'), t = document.getElementById('sb-timing');
        if (!s) return;
        var day = d && (d.data_date || d.date || d.ts);
        // ★2026-08-14 数据即时性：数据日带语义（data_semantic：盘中/盘后/周末），
        //   title 悬浮说明"日线每日 18:30 更新"，解决"看到 13 号以为是卡住"
        if (day) {
          var sem = d.data_semantic || ('数据日 ' + String(day).slice(0, 10));
          s.textContent = '数据日 ' + String(day).slice(0, 10);
          s.title = sem + '（日线每日 18:30 收盘后更新）';
          s.style.color = sem.indexOf('已更新') >= 0 ? '#4ade80' : (sem.indexOf('盘') >= 0 ? '#93c5fd' : '#f2c94c');
        }
        var chain = d && d.chain;
        if (chain) {
          var ok = chain.ok !== undefined ? chain.ok : (chain.done === chain.total);
          c.textContent = '决策链 ' + (ok ? '' : '') + ' ' + (chain.done !== undefined ? (chain.done + '/' + chain.total) : '');
        }
        // ★2026-08-14 审计修复：portal_dash.timing 是完整 timing_dash 结构（含 cal_month/score_hist 等），
        //   主数据在 .timing 子对象（与 sidebar.js 一致）；原直接取 d.timing 导致 level/score/emoji 全 miss
        //   （顶栏"择时"恒空）。且 statusbar 不渲染 emoji（UI 标准 v1：全站不用 emoji）。
        var tm = d && d.timing && d.timing.timing;
        if (tm) t.textContent = '择时 ' + win.LW.render._f(tm.level, '') + ' ' + win.LW.render._f(tm.score, '') + '分';
      }).catch(function () { /* 降级：保持占位文案 */ });
    }
  }

  win.LW = win.LW || {};
  win.LW.statusbar = { render: renderInto };
})(window);
