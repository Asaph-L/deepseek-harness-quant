/* deck/live_patch.js — F5 页面 API 化统一实时层（总指导 · 2026-08-10）
 *
 * 用法：页面 </body> 前注入
 *   <script src="/live_patch.js" data-page="opp|watch|holdings|tech"></script>
 *
 * 行为：页面首屏 = 生成器静态快照（无 JS 也完整可用）；
 *       本脚本加载后 fetch /api/live/<page> 重建数据区，每 60s 轮询。
 * 验收：任一页面无需重新生成即可反映最新数据（F5 质量红线）。
 */
(function () {
  'use strict';
  var page = (document.currentScript && document.currentScript.dataset.page) || 'opp';
  var INTERVAL = 60000;
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var fmt = function (v, d) { return (v == null || v === '') ? (d == null ? '—' : d) : v; };
  var num = function (v, d) {
    if (v == null || isNaN(v)) return (d == null ? '—' : d);
    return (+v).toFixed(1);
  };

  /* ================= 机会池 ================= */
  var TYPE_CN = {
    value: '价值低估', revalue: '价值重估', event: '事件驱动', quality_gap: '质量折价',
    pv_consensus: '量价共识', breakout: '突破启动', reversal: '超跌反转'
  };
  var TYPE_COLOR = { value: '#0F6E56', revalue: '#185FA5', event: '#7A5FD0', quality_gap: '#C0392B',
    pv_consensus: '#D4A843', breakout: '#E4572E', reversal: '#8a94a6'
  };
  // ★2026-08-12 百轮#72：审批状态缓存（观察池/机会池回显"已审批/已放弃"——联动闭环）
  var DECIDED = {};
  var _lastData = null;
  function loadDecided(cb) {
    fetch('/api/decisions', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (ds) {
        if (!Array.isArray(ds)) return;
        DECIDED = {};
        ds.forEach(function (r) { if (r && r.code && r.action) DECIDED[r.code] = r.action; });
        if (cb) cb();
      })
      .catch(function () {});
  }
  // ★2026-08-11 信号族（用户指示：分类基于因子属于什么信号）——与 dashboard_opp 页签同源
  var FAM_CN = { 价值: '💹价值', 成长: '📈成长', 质量: '🏛️质量', 量价: '📊量价', 情绪: '🔥情绪',
    反转动量: '🔄反转/动量', 资金: '💰资金', 政策: '🏛️政策', 其他: '📦其他' };
  var FAM_COLOR = { 价值: '#0F6E56', 成长: '#2F6FED', 质量: '#0E8A7E', 量价: '#D4A843',
    情绪: '#C0392B', 反转动量: '#7A5FD0', 资金: '#B07CC6', 政策: '#185FA5', 其他: '#8a94a6' };
  function oppRows(ops, thr, cards) {
    var rows = '';
    ops.forEach(function (o) {
      var code = esc(o.code), score = +o.score || 0;
      var codeHtml = '<b class="code-link" data-code="' + code + '" title="点击查看全板块联动">' + code + '</b>';
      var card = cards[o.otype] || {};
      var cardHtml = card.why_works ? (
        '<div class="why-box"><div class="why-title">💡 为什么这个策略有效（' + esc(card.name || o.otype) + '）</div>' +
        '<div class="why-line"><b>市场逻辑：</b>' + esc(card.why_works) + '</div>' +
        '<div class="why-line"><b>实证：</b>' + esc(card.evidence) + '</div>' +
        '<div class="why-line"><b>何时失效：</b>' + esc(card.when_fails) + '</div>' +
        '<div class="why-line"><b>怎么读：</b>' + esc(card.how_to_read) + '</div></div>') : '';
      var badge = '';
      if (score >= (thr.pitch_same_type || 80)) badge = '<span class="badge-strong">极强</span>';
      else if (score >= (thr.pitch_global || 70)) badge = '<span class="badge-pitch">可 Pitch</span>';
      var track = o.track === '科技'
        ? '<span class="track-tech">科技</span>'
        : '<span class="track-value">价值</span>';
      // ★2026-08-11 信号族徽章 + 信号分（有效程度×强度加权）+ 无效因子标注
      var fam = o.signal_family || '其他';
      var famBadge = '<span class="sig-chip" style="background:' + (FAM_COLOR[fam] || '#8a94a6') + '22;color:' + (FAM_COLOR[fam] || '#8a94a6') + '">' + (FAM_CN[fam] || fam) + '</span>';
      var sigScore = o.signal_score;
      var sigHtml = sigScore != null
        ? '<b style="font-size:15px;color:' + (sigScore >= 60 ? '#0F6E56' : (sigScore >= 35 ? '#D4A843' : '#C0392B')) + '">' + (+sigScore).toFixed(0) + '</b>'
        : '<span class="mini">—</span>';
      var sigBadge = '';
      if (o.n_invalid > 0) sigBadge = '<span class="badge b-invalid" title="触发因子含失效/无效因子（不计信号权重）">⚠️' + o.n_invalid + '无效</span>';
      // 因子有效性展开（factor_eff：family/icir120/status/rank）
      var fe = o.factor_eff || {};
      var feHtml = Object.keys(fe).map(function (k) {
        var e = fe[k] || {};
        var stCls = (e.status || '').indexOf('✅') >= 0 ? 'c-green' : ((e.status || '').indexOf('❌') >= 0 || (e.status || '').indexOf('⚠️') >= 0 ? 'c-red' : '');
        return '<tr><td>' + esc(k) + '</td><td>' + esc(e.family || '—') + '</td>' +
          '<td>' + (e.icir120 != null ? (+e.icir120).toFixed(2) : '—') + '</td>' +
          '<td class="' + stCls + '">' + esc(e.status || '未登记') + '</td>' +
          '<td>' + (e.rank != null ? (+e.rank).toFixed(2) : '—') + '</td></tr>';
      }).join('');
      var fac = '';
      var fk = o.factors || {};
      Object.keys(fk).forEach(function (k) {
        fac += '<tr><td>' + esc(k) + '</td><td>' + esc(fk[k]) + '</td></tr>';
      });
      var extra = o.note ? '<span class="mini">· ' + esc(o.note) + '</span>' : '';
      var otc = TYPE_COLOR[o.otype] || '#888';
      rows += '<tr class="opp-row" onclick="toggle(this)">' +
        '<td>' + (o.rank_signal || '—') + '</td>' +
        '<td>' + codeHtml + track + '</td><td>' + esc(o.name || code) + '</td>' +
        '<td><a class="btn-link" href="/dashboard_stockcheck.html?code=' + String(code).split('.')[0] + '" title="个股检测">🔍</a>' +
        // ★2026-08-11 百轮#45：机会池 → Pitch 审批入口（带 code 定位；命中 Pitch 标黄）
        // ★2026-08-12 百轮#74：审批状态回显（DECIDED #72 全局缓存：已买入/已放弃/待处理）
        (function () {
          var _d = DECIDED[code];
          if (_d === 'buy') return ' <span style="color:var(--green,#0F6E56);font-weight:700" title="已审批买入">✅ 已买入</span>';
          if (_d === 'drop') return ' <span style="color:var(--red,#C0392B)" title="已审批放弃">🚫 已放弃</span>';
          return ' <a class="btn-link" href="/pitch.html?code=' + String(code).split('.')[0] + '" title="进 Pitch 决策台审批" style="color:#0F6E56;font-weight:700">' +
            (o.in_pitch ? '✅ 审批' : '→ 审批') + '</a>';
        })() + '</td>' +
        '<td>' + famBadge + '</td>' +
        '<td><span class="type-chip" style="background:' + otc + '22;color:' + otc + '">' + esc(o.otype_name || o.otype) + '</span>' +
        // ★2026-08-12 百轮#103：降权类型标记（实盘裁决体系落池——浏览即知哪些类型降权）
        (o.down_type ? '<span style="color:#b0774a" title="该类型实盘偏弱降权提示中，审批从严（Pitch 卡片带 🔻）"> 🔻降权</span>' : '') +
        '</td>' +
        '<td class="mini">' + esc(o.trigger) + '</td>' +
        '<td>' + sigHtml + sigBadge + '</td>' +
        '<td><b style="font-size:15px">' + score.toFixed(1) + '</b> ' + badge + '</td>' +
        '<td>' + num(o.gains) + '</td><td>' + num(o.prob) + '</td><td>' + num(o.risk) + '</td>' +
        '<td class="mini">' + esc(o.winrate_est) + '</td>' + extra + '</tr>' +
        '<tr class="opp-detail" style="display:none"><td colspan="13"><div class="detail-box">' + cardHtml +
        '<div class="mini"><b>触发条件：</b>' + esc(o.trigger) + '</div>' +
        '<div class="mini" style="margin-top:4px"><b>证据链：</b>' + esc(o.evidence) + '</div>' +
        (o.eff_note ? '<div class="mini" style="color:#C0392B;margin-top:4px"><b>' + esc(o.eff_note) + '</b></div>' : '') +
        (feHtml ? '<table class="fac-table"><thead><tr><th>触发因子</th><th>信号族</th><th>ICIR120</th><th>有效性</th><th>个股rank</th></tr></thead><tbody>' + feHtml + '</tbody></table>' : '') +
        '<table class="fac-table"><thead><tr><th>因子</th><th>当前值</th></tr></thead><tbody>' +
        (fac || '<tr><td colspan=2>无因子数据</td></tr>') + '</tbody></table>' +
        '</div></td></tr>';
    });
    return rows;
  }
  function renderOpp(d, root) {
    if (!d.ok) return;
    root = root || document;
    var q = function (sel) { return root.querySelector(sel); };
    var qa = function (sel) { return root.querySelectorAll(sel); };
    var thr = d.thresholds || {};
    // header KPI（按序：大池子/Pitch门槛/极强门槛/Pitch候选）
    var kpis = qa('.header .kpi b');
    if (kpis.length >= 4) {
      kpis[0].textContent = d.n; kpis[1].textContent = thr.pitch_global || 70;
      kpis[2].textContent = thr.pitch_same_type || 80; kpis[3].textContent = d.pitch.length;
    }
    var sub = q('.header .sub');
    if (sub) sub.textContent = '数据日期 ' + (d.date || '—') + ' ｜ 实时刷新 ' + d.ts + ' ｜ ' + (d.file || '');
    // Pitch 徽章
    var pl = q('.pitch-line');
    if (pl) {
      pl.innerHTML = 'Pitch：' + (d.pitch.map(function (p) {
        return '<span class="pitch-badge">' + esc(p.name) + ' <b>' + (+p.score).toFixed(0) + '</b></span>';
      }).join('') || '<span class="mini">暂无</span>');
    }
    // 双轨计数角标
    var badge = q('.track-summary');
    if (badge) {
      badge.innerHTML = '<span class="track-tech">科技 ' + d.n_tech + '</span> <span class="track-value">价值 ' + d.n_value + '</span>';
    }
    // 总览 overview（gate/审计/择时）
    var ov = d.overview || {};
    var ovEl = q('.overview');
    if (ovEl && ov.regime_cn) {
      var gateHtml = (!ov.gate_ok) ? '<div class="gate-banner">⛔ 信号闸门阻断：' + esc(ov.gate_reason) + '</div>' : '';
      var audHtml = (ov.audit_blocked) ? '<div class="audit-block">🔴 数据审计阻断：' + esc(ov.audit_block_reason) + '</div>' : '';
      ovEl.innerHTML = gateHtml + audHtml +
        '<div class="ov-grid">' +
        '<div class="ov-card"><div class="mini">择时档位</div><b>' + esc(ov.regime_cn) + '</b><div class="mini">现金 ' + Math.round((ov.regime_cash || 0) * 100) + '% · ' + esc(ov.regime_level) + '</div></div>' +
        '<div class="ov-card"><div class="mini">数据审计</div><b style="color:' + (ov.audit_fail > 0 ? '#C0392B' : '#0F6E56') + '">' + num(ov.audit_health, 0) + '/100</b><div class="mini">PASS ' + fmt(ov.audit_pass, 0) + ' · FAIL ' + fmt(ov.audit_fail, 0) + '</div></div>' +
        '<div class="ov-card"><div class="mini">通过股票数</div><b>' + esc(ov.n_passed) + '</b><div class="mini">今日信号</div></div>' +
        '<div class="ov-card"><div class="mini">今日建议</div><b class="mini">' + esc(ov.advice) + '</b></div></div>';
    }
    // ★2026-08-11 信号族页签计数 + 各行（按 signal_family 分组；排序 = rank_signal 信号加权排名）
    var FAM_ORDER = ['价值', '成长', '质量', '量价', '情绪', '反转动量', '资金', '政策', '其他'];
    // ★2026-08-11 百轮#45：Pitch code 集合（机会池行标记"✅ 已在 Pitch"）
    var pitchSet = {};
    (d.pitch || []).forEach(function (p) { pitchSet[p.code] = 1; });
    d.opportunities.forEach(function (o) { if (pitchSet[o.code]) o.in_pitch = 1; });
    FAM_ORDER.forEach(function (fam) {
      var tab = q('.tab[data-fam="' + fam + '"]');
      var sub_ = d.opportunities.filter(function (o) { return (o.signal_family || '其他') === fam; });
      if (tab) { var c = tab.querySelector('.cnt'); if (c) c.textContent = sub_.length; }
      var pane = q('#pane-fam-' + fam);
      if (!pane) return;
      // ★2026-08-13 #210：📌 系统建议置顶（suggested 优先，再看 signal_score）
      sub_.sort(function (a, b) {
        var sa = a.suggested ? 1 : 0, sb = b.suggested ? 1 : 0;
        if (sa !== sb) return sb - sa;
        return ((b.signal_score != null ? b.signal_score : -1) - (a.signal_score != null ? a.signal_score : -1));
      });
      var tb = pane.querySelector('tbody');
      if (tb) tb.innerHTML = oppRows(sub_, thr, d.cards) || '<tr><td colspan=13 class="empty">该信号族无机会</td></tr>';
    });
    // ★2026-08-10 合并页 pane-all（全部机会汇总 + 类型徽章）——原单页无此 tab，pool 总览页专用
    var allPane = q('#pane-all');
    if (allPane) {
      var atb = allPane.querySelector('tbody');
      if (atb) {
        var allSorted = d.opportunities.slice().sort(function (a, b) {
          var sa = a.suggested ? 1 : 0, sb = b.suggested ? 1 : 0;
          if (sa !== sb) return sb - sa;
          return ((b.signal_score != null ? b.signal_score : -1) - (a.signal_score != null ? a.signal_score : -1));
        });
        var rows = allSorted.map(function (o) {
          var sug = o.suggested ? '<span style="background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;border-radius:8px;padding:0 5px;font-size:10px;font-weight:600;margin-left:4px" title="系统建议买入（策略决策池）">📌 建议</span>' : '';
          return '<tr><td>' + o.rank_global + '</td><td><b class="code-link" data-code="' + esc(o.code) + '" title="点击查看全板块联动">' + esc(o.code) + '</b>' + sug + '<span class="mini"> ' + esc(o.name) + '</span></td>' +
            '<td class="mini">' + esc(String(o.industry || '—').slice(0, 14)) + '</td>' +
            '<td><span class="t-green">' + (TYPE_CN[o.otype] || o.otype) + '</span></td>' +
            '<td><b>' + (+o.score).toFixed(0) + '</b></td>' +
            '<td>' + (o.upside_est != null ? (+o.upside_est).toFixed(0) + '%' : '—') + '</td>' +
            '<td class="mini">' + (o.winrate_est != null ? (+o.winrate_est).toFixed(0) + '%' : '—') + '</td>' +
            '<td class="mini">' + esc(String(o.note || '').slice(0, 36)) + '</td></tr>';
        }).join('');
        atb.innerHTML = rows || '<tr><td colspan=8 class="empty">暂无机会</td></tr>';
      }
    }
  }

  /* ================= 观察池 ================= */
  function techBadge(t) {
    var a50 = t.above_ma50, a200 = t.above_ma200;
    if (a50 && a200) return '<span class="t-green">🟢 双均线上</span>';
    if (a50) return '<span class="t-yellow">🟡 仅MA50上</span>';
    if (a200) return '<span class="t-blue">🔵 仅MA200上</span>';
    return '<span class="t-gray">⚪ 均线下</span>';
  }
  function watchRows(items, layer) {
    var rows = '';
    items.forEach(function (it) {
      var t = it.tech || {};
      var d52 = t.dist_high52_pct;
      var d52Cls = (d52 != null && d52 < 0) ? 'neg' : '';
      var f = it.factors || {};
      var fac = '<span class="f-box"><i>低波</i><b>' + fmt(f.lowvol_rank) + '</b></span>' +
        '<span class="f-box"><i>反转</i><b>' + fmt(f.reversal_rank) + '</b></span>' +
        '<span class="f-box"><i>质量</i><b>' + fmt(f.quality_rank) + '</b></span>' +
        '<span class="f-box"><i>成长</i><b>' + fmt(f.growth_rank) + '</b></span>';
      // ★2026-08-11 百轮#15 决策联动：decision 层加"审批"入口（观察池 → Pitch 决策台）
      // ★2026-08-11 百轮#43：入口带 code 参数 → Pitch 页自动定位高亮该股票
      // ★2026-08-12 百轮#72：审批状态回显（联动闭环——审批后回来显示"已审批/已放弃"）
      var act;
      if (layer === 'decision') {
        var _dec = DECIDED[it.code];
        if (_dec === 'buy') {
          act = '<span style="color:var(--green,#0F6E56);font-weight:700">✅ 已审批买入</span>';
        } else if (_dec === 'drop') {
          act = '<span style="color:var(--red,#C0392B)">🚫 已放弃</span>';
        } else {
          act = '<a class="btn-link" href="/pitch.html?code=' + String(it.code || '').split('.')[0] + '" title="进 Pitch 决策台审批" style="color:#0F6E56;font-weight:700">→ 审批</a>';
        }
      } else {
        act = '<a class="btn-link" href="/dashboard_stockcheck.html?code=' + String(it.code || '').split('.')[0] + '" title="个股检测">🔍</a>';
      }
      // ★2026-08-11 百轮#43：观察理由展示（pool_layers reason + 评分依据）
      var why = it.reason ? '<div style="margin-top:6px">💡 <b>观察理由：</b>' + esc(String(it.reason).slice(0, 80)) + '</div>' : '';
      rows += '<tr class="w-row" onclick="toggle(this)">' +
        '<td>' + fmt(it.rank) + '</td>' +
        '<td><b>' + esc(it.code) + '</b><span class="mini"> ' + esc(it.name) + '</span>' +
        // ★2026-08-12 百轮#103：降权类型标记（观察池同标）
        (it.down_type ? ' <span style="color:#b0774a" title="该类型实盘偏弱降权提示中，审批从严">🔻' + esc(it.down_type) + '</span>' : '') +
        '</td>' +
        '<td class="mini">' + esc(String(it.industry || '—').slice(0, 18)) + '</td>' +
        '<td>' + fmt(it.price) + '</td><td>' + fmt(it.mv_yi) + '</td>' +
        '<td><b>' + fmt(it.score) + '</b></td>' +
        '<td>' + techBadge(t) + '</td>' +
        '<td class="' + d52Cls + '">' + (d52 == null ? '—' : d52 + '%') + '</td>' +
        '<td>' + fmt(t.dist_low52_pct) + '%</td>' +
        '<td>' + fmt(t.vol_ratio_20_60) + '</td>' +
        '<td>' + act + '</td></tr>' +
        '<tr class="w-detail" style="display:none"><td colspan="11"><div class="d-box">' +
        'MA50 ' + fmt(t.ma50) + ' · MA200 ' + fmt(t.ma200) + ' · 距52周高 ' + (d52 == null ? '—' : d52 + '%') +
        ' · 距52周低 ' + fmt(t.dist_low52_pct) + '%' +
        '<div style="margin-top:6px">' + fac + '</div>' + why + '</div></td></tr>';
    });
    return rows;
  }
  function renderWatch(d, root) {
    if (!d.ok) return;
    root = root || document;
    var q = function (sel) { return root.querySelector(sel); };
    var sub = q('.header .sub');
    if (sub) sub.textContent = '数据 ' + (d.date || '—') + ' ｜ 仓位 ' + fmt(d.capital, '—') + ' ｜ 防守现金 ' + fmt(d.regime_cash, '—') + ' ｜ 实时刷新 ' + d.ts;
    var kpis = root.querySelectorAll('.header .kpi b');
    if (kpis.length >= 3) {
      kpis[0].textContent = d.n_watch; kpis[1].textContent = d.n_candidate; kpis[2].textContent = d.n_decision;
    }
    [['watch', d.watch], ['candidate', d.candidate], ['decision', d.decision]].forEach(function (kv) {
      var el = q('#layer-' + kv[0]);
      if (!el) return;
      var tb = el.querySelector('tbody');
      if (tb) tb.innerHTML = watchRows(kv[1], kv[0]) || '<tr><td colspan=10 class="mini">无数据</td></tr>';
    });
    // ★2026-08-11 百轮#64：三层漏斗 + 晋级率（watch→candidate→decision）
    var f3 = q('#funnel3');
    if (f3 && d.n_watch != null) {
      var nw = d.n_watch, nc = d.n_candidate, nd = d.n_decision;
      var r1 = nw ? Math.round(nc / nw * 100) : 0;   // watch→candidate 晋级率
      var r2 = nc ? Math.round(nd / nc * 100) : 0;   // candidate→decision 晋级率
      function seg(label, n, pct, color) {
        return '<div style="flex:1;min-width:90px;text-align:center">' +
          '<b style="font-size:18px">' + n + '</b><div class="mini">' + label + '</div>' +
          '<div style="height:7px;background:rgba(0,0,0,.1);border-radius:4px;margin:3px 6px;overflow:hidden"><div style="height:100%;width:' + Math.min(pct, 100) + '%;background:' + color + '"></div></div>' +
          '<div class="mini">晋级 ' + pct + '%</div></div>';
      }
      f3.innerHTML = '🔻 <b>三层漏斗</b>：' +
        '<div style="display:flex;align-items:center;margin-top:4px">' +
        seg('观察 watch', nw, 100, '#0F6E56') + '<span style="font-size:20px;color:#8a94a6">→</span>' +
        seg('候选 candidate', nc, r1, '#185FA5') + '<span style="font-size:20px;color:#8a94a6">→</span>' +
        seg('决策 decision', nd, r2, '#D4A843') + '</div>' +
        '<div class="mini" style="margin-top:4px">淘汰逻辑：watch→candidate 需技术确认（价>MA50+回撤≤20%+量比≥0.8）；candidate→decision 按 score+行业≤3+资金分档；防守档决策池清空（宁缺毋滥）</div>';
      f3.style.display = '';
    }
    // ★2026-08-11 百轮#54：入池规则实时刷新（pool_layers.rules）
    var rtb = q('#rules-tbody');
    if (rtb && d.rules) {
      var rows = ['watch', 'candidate', 'decision', 'evidence'].map(function (k) {
        return d.rules[k] ? '<tr><td><b>' + k + '</b></td><td class="mini">' + esc(d.rules[k]) + '</td></tr>' : '';
      }).join('');
      var gen = d.rules.generated_at ? '<tr><td><b>generated_at</b></td><td class="mini">' + esc(d.rules.generated_at) + '</td></tr>' : '';
      if (rows + gen) rtb.innerHTML = rows + gen;
    }
  }

  /* ================= 科技池 ================= */
  function renderTech(d, root) {
    if (!d.ok) return;
    root = root || document;
    var q = function (sel) { return root.querySelector(sel); };
    var sub = q('.header .sub');
    if (sub) sub.textContent = '收录 breakout 突破 + pv_consensus 量价共识 ｜ 独立门槛 ' + fmt(d.threshold, 62) + ' 分 ｜ 数据 ' + fmt(d.pool_date, '—') + ' ｜ 实时刷新 ' + d.ts;
    var kpis = root.querySelectorAll('.header .kpi b');
    if (kpis.length >= 2) { kpis[0].textContent = d.entries.length; kpis[1].textContent = d.new_codes.length; }
    var alertEl = q('.alert');
    if (alertEl) {
      alertEl.remove();
      var nc = d.new_codes || [];
      if (nc.length) {
        var div = document.createElement('div');
        div.className = 'alert';
        div.innerHTML = '🔔 <b>实时监控：' + nc.length + ' 只新突破/共识股票入池</b>（' + esc(nc.slice(0, 5).join('、')) + '…）<br>系统自动检测到新符合条件的技术信号股票，已加入科技突破池 —— 请关注是否跟进';
        var wr = q('.warn');
        if (wr && wr.parentNode) wr.parentNode.insertBefore(div, wr);
      }
    }
    var tb = q('.section table tbody');
    if (!tb) return;
    var rows = '';
    d.entries.forEach(function (e) {
      var badge = e.is_new ? '<span class="new">🆕 NEW</span>' : '';
      var board = e.board ? '<span class="board-chip">' + esc(e.board) + '</span>' : '';
      var tech = e.tech_label ? '<span class="tech-chip">' + esc(e.tech_label) + '</span>' : '';
      var conf = e.confidence || '';
      var confCls = conf === '低置信' ? 'c-low' : 'c-mid';
      var sp = (e.stop_plan || {}).desc || '';
      rows += '<tr><td>' + badge + '</td>' +
        '<td><b>' + esc(e.code) + '</b><span class="mini"> ' + esc(e.name) + '</span></td>' +
        '<td><span class="type-chip">' + esc(e.otype_name || e.otype || '') + '</span>' + board + tech + '</td>' +
        '<td><b style="font-size:15px">' + fmt(e.score) + '</b></td>' +
        '<td class="' + confCls + '">' + esc(conf) + '</td>' +
        '<td class="mini">' + esc(String(e.trigger || '—').slice(0, 60)) + '</td>' +
        '<td class="mini risk">' + esc(e.risk_notice || '') + '</td>' +
        '<td class="mini">' + esc(sp.slice(0, 55)) + '</td></tr>';
    });
    tb.innerHTML = rows || '<tr><td colspan=7 class="mini">暂无科技突破候选（当前市场突破类稀少，属正常）</td></tr>';
  }

  /* ================= 持有池 ================= */
  function renderHoldings(d) {
    if (!d.ok) return;
    var pr = d.position_risk || {}, ds = d.daily_signal || {}, pnl = d.pnl || {};
    var sub = document.querySelector('.header .sub');
    if (sub) sub.textContent = '数据 ' + fmt(pr.date, '—') + ' ｜ 纪律：持股≤5 · 类型差异化权重 · 极严格 Pitch ｜ 实时刷新 ' + d.ts;
    // KPI（持仓数/单股集中度/前5集中度/组合波动）——持仓数用真实持仓（pnl.n_holdings），position_risk 的 v3 清单是旧口径
    var kpis = document.querySelectorAll('.header .kpi b');
    if (kpis.length >= 4) {
      kpis[0].textContent = pnl.n_holdings != null ? pnl.n_holdings : fmt(pr.n_holdings);
      kpis[1].textContent = pr.concentration_single != null ? (pr.concentration_single * 100).toFixed(0) + '%' : '—';
      kpis[2].textContent = pr.concentration_top5 != null ? (pr.concentration_top5 * 100).toFixed(0) + '%' : '—';
      kpis[3].textContent = fmt(pr.est_port_vol);
    }
    // ★2026-08-11 百轮#38：盈亏总览（真实持仓 现价/成本/收益率 + 组合汇总）
    // ★2026-08-12 百轮后#116：持仓降权类型标注（3/4 属降权提示类型——显示"所持类型降权观察中"）
    var downMap = {};
    ((d.portfolio || {}).positions || []).forEach(function (p) {
      if (p.down_type) downMap[p.code] = p.down_type;
    });
    var pb = document.getElementById('pnl-box');
    if (pb && pnl.ok) {
      var tr = pnl.total_ret;
      var trCls = tr == null ? '' : (tr >= 0 ? 'style="color:var(--red)"' : 'style="color:var(--green)"');
      var rows = (pnl.rows || []).map(function (r) {
        var ret = r.ret;
        var cls = ret == null ? '' : (ret >= 0 ? 'style="color:var(--red)"' : 'style="color:var(--green)"');
        var retS = ret != null ? (ret >= 0 ? '+' : '') + (ret * 100).toFixed(2) + '%' : '—';
        var downT = downMap[r.code];
        var downBadge = downT
          ? ' <span style="color:#C0392B;font-size:10px;border:1px solid #C0392B;border-radius:8px;padding:0 5px" title="所持类型实盘偏弱降权提示中（审批从严；持有观察）">🔻' + esc(downT) + '</span>'
          : '';
        return '<tr><td><b>' + esc(r.code) + '</b><span class="mini"> ' + esc(r.name) + '</span>' + downBadge + '</td>' +
          '<td class="mini">' + fmt(r.entry_date) + '</td>' +
          '<td>' + (r.entry_price != null ? r.entry_price.toFixed(2) : '—') + '</td>' +
          '<td>' + (r.last_price != null ? r.last_price.toFixed(2) : '—') + '</td>' +
          '<td ' + cls + ' style="font-weight:700">' + retS + '</td></tr>';
      }).join('');
      pb.innerHTML =
        '<b>💰 组合盈亏</b>：持仓 <b>' + pnl.n_holdings + '</b> 只 ｜ 成本 <b>¥' + (pnl.total_cost != null ? pnl.total_cost.toFixed(0) : '—') + '</b>' +
        ' ｜ 总收益率 <b ' + trCls + '>' + (tr != null ? ((tr >= 0 ? '+' : '') + (tr * 100).toFixed(2) + '%') : '—') + '</b>' +
        (pnl.rows && pnl.rows.length ? '<table style="width:100%;margin-top:8px;border-collapse:collapse"><thead><tr><th>股票</th><th>买入日</th><th>成本</th><th>现价</th><th>收益率</th></tr></thead><tbody>' + rows + '</tbody></table>' : '') +
        // ★2026-08-12 百轮后#119：行业敞口（持仓行业分布——集中度风险；行业分散提示）
        (function () {
          var ie = d.industry_exposure || {};
          var keys = Object.keys(ie);
          if (!keys.length) return '';
          var maxN = Math.max.apply(null, keys.map(function (k) { return ie[k]; }));
          var parts = keys.map(function (k) {
            var pct = Math.round(ie[k] / (pnl.n_holdings || 1) * 100);
            var hi = ie[k] >= 2 ? 'style="color:#b0774a"' : '';
            return '<span ' + hi + '>' + esc(k.replace(/^[A-Z]\d+\s*/, '')) + ' ' + ie[k] + '</span>';
          });
          var warn = maxN >= 2 ? ' <span style="color:#b0774a">⚠️ 有行业集中（' + maxN + ' 只同行业）</span>' : ' <span style="color:#0F6E56">分散良好</span>';
          return '<div class="mini" style="margin-top:6px">🏭 行业敞口：' + parts.join(' ｜ ') + warn + '</div>';
        })();
    }
    // 风控 flags
    var sec = document.querySelector('.section');
    if (sec) {
      var fl = sec.querySelector('.flag, .ok');
      if (fl) {
        var flags = pr.flags || {};
        var items = [];
        if (flags.high_corr) items.push('高相关性（组合分散不足）');
        if (flags.high_concentration) items.push('高集中度（单股/行业超限）');
        if (flags.industry_high) items.push('单行业超 20% 上限（' + esc(pr.top_industry || '') + ' ' + (pr.concentration_industry != null ? (pr.concentration_industry * 100).toFixed(0) + '%' : '') + '）');
        if (flags.deep_drawdown) items.push('深度回撤个股 ' + flags.deep_drawdown + ' 只（-60 日回撤>阈值）');
        fl.outerHTML = items.length
          ? items.map(function (s) { return '<div class="flag">🔴 ' + esc(s) + '</div>'; }).join('')
          : '<div class="ok">✅ 无风控告警</div>';
      }
    }
    // 今日指令
    var gate = ds.gate || {};
    var g = document.querySelector('.gate');
    if (g) {
      if (!gate.ok) g.textContent = '⛔ 闸门阻断：' + esc(gate.reason || '');
      else g.remove();
    }
    var ab = document.querySelector('.advice-box');
    if (ab) ab.textContent = fmt(ds.advice, '—');
    // ★2026-08-11 百轮#61：今日操作建议——止盈/止损/组合风险聚合（每持仓建议 + 组合结论）
    try {
      var adv = document.getElementById('lw-advice');
      if (!adv) {
        adv = document.createElement('div'); adv.id = 'lw-advice';
        adv.style.cssText = 'background:#F0F6FF;border:1px solid #B8D4F0;border-radius:10px;padding:10px 14px;margin:10px 0;font-size:12.5px;line-height:1.9';
        var sec3 = document.querySelector('.section');
        if (sec3) sec3.parentNode.insertBefore(adv, sec3);
      }
      var tpmap = {};
      var tp3 = d.take_profit || {};
      (tp3.positions || []).forEach(function (t) { tpmap[t.code] = t; });
      var stmap = {};
      var sa3 = d.stop_alerts || {};
      (sa3.entries || []).forEach(function (e) { stmap[e.code] = e; });
      var acts = [];
      ((d.portfolio || {}).positions || []).forEach(function (p) {
        if (p.status !== 'holding' && p.status !== 'over_limit') return;
        var t = tpmap[p.code] || {};
        var fired = (t.signals || []).filter(function (s) { return s.type === 'target' || s.type === 'pullback' || s.type === 'time'; });
        var se = stmap[p.code] || {};
        if (fired.length) {
          acts.push('<span style="color:#b0774a">🔔 ' + esc(p.code) + ' 建议止盈（' + esc(fired[0].msg || fired[0].type) + '）</span>');
        } else if (se.status === 'TRIGGERED') {
          acts.push('<span style="color:#b0774a">⛔ ' + esc(p.code) + ' 触发止损，建议卖出</span>');
        } else if (se.status === 'NEAR') {
          acts.push('<span style="color:#D4A843;font-weight:700">⚠️ ' + esc(p.code) + ' 接近止损（' + esc((se.alerts || [{rule:''}])[0].rule) + '）</span>');
        } else {
          acts.push('<span style="color:#0F6E56">🛡 ' + esc(p.code) + ' 持有</span>');
        }
      });
      var flags = (d.position_risk || {}).flags || {};
      var combos = [];
      if (flags.high_concentration) combos.push('持仓集中度偏高（≤5 纪律已满）');
      if (flags.industry_high) combos.push('单行业超限');
      if (flags.deep_drawdown) combos.push(flags.deep_drawdown + ' 只深回撤');
      if (combos.length) acts.push('<span style="color:#b0774a">📉 组合：' + combos.join('、') + '</span>');
      if (!acts.length) acts.push('<span style="color:#0F6E56">✅ 无持仓</span>');
      adv.innerHTML = '<b>📋 今日操作建议</b>：' + acts.join(' ｜ ');
    } catch (e) {}
    // ★2026-08-11 百轮#18 止损预警展示（池级 stop_alerts：near 临近 / triggered 触发）
    var sa = d.stop_alerts || {};
    var sec2 = null; // ????????
    if (sec2) {
      var near = (sa.near || 0), trig = (sa.triggered || 0);
      var saEl = document.getElementById('lw-stop-alert');
      if (!saEl) {
        saEl = document.createElement('div'); saEl.id = 'lw-stop-alert';
        saEl.style.cssText = 'margin:8px 0;padding:6px 12px;border-radius:8px;font-size:11.5px;'
          + ((trig > 0 || near > 0) ? 'background:#C0392B22;border:1px solid #C0392B;color:#C0392B;' : 'background:#F0F3F8;border:1px solid #e5e9f0;color:#66788a;');
        sec2.parentNode.insertBefore(saEl, sec2);
      }
      saEl.textContent = (trig > 0 || near > 0 ? '🚨 ' : '🛡 ') + '止损预警：监测 ' + (sa.monitored || 0) + ' 只'
        + (near > 0 ? ' ｜ ⚠️ 临近止损 ' + near + ' 只' : '')
        + (trig > 0 ? ' ｜ 🔴 已触发 ' + trig + ' 只' : '');
    }
    // ★2026-08-11 百轮#5 决策数据联动：持仓表实时渲染（审批买入 → 60s 内自动显示 + 止盈/止损状态）
    var ptb = document.querySelector('#pos-tbody');
    if (ptb) {
      var tmap = {}, tp = d.take_profit || {};
      (tp.positions || []).forEach(function (t) { tmap[t.code] = t; });
      var pfs = (d.portfolio || {}).positions || [];
      var rows = pfs.map(function (p) {        var t = tmap[p.code] || {};
        var sig = '', sigAlert = false;
        (t.signals || []).forEach(function (s) {
          if (s.type === 'target' || s.type === 'pullback' || s.type === 'time') { sig = '<span style="color:#C0392B;font-weight:600">🔔 ' + esc(s.msg) + '</span>'; sigAlert = true; }
          else if (s.type === 'hold') sig = '<span class="mini">🛡 ' + esc(s.msg) + '</span>';
        });
        var ret = t.ret;
        var retHtml = ret != null ? '<b style="color:' + (ret > 0 ? 'var(--red)' : 'var(--green)') + '">' + (ret > 0 ? '+' : '') + (ret * 100).toFixed(0) + '%</b>' : '—';
        var sp = p.stop_plan || {};
        var stopHtml = sp.stop_loss_pct != null ? (sp.stop_loss_pct * 100).toFixed(0) + '%' : '—';
        // ★2026-08-11 百轮#39：卖出按钮（止盈/止损触发红色高亮；现价自动带）
        var sellBtn = '<button class="btn" style="padding:2px 10px;font-size:11px;' +
          (sigAlert ? 'border-color:#C0392B;color:#C0392B' : '') + '" ' +
          'onclick="sellPos(\'' + esc(p.code) + '\',' + (t.close != null ? t.close : 'null') + ',' + (sigAlert ? 'true' : 'false') + ')">' +
          (sigAlert ? '🔔 卖出' : '卖出') + '</button>';
        return '<tr><td><b>' + esc(p.code) + '</b><span class="mini"> ' + esc(p.name || '') + '</span></td>' +
          '<td>' + retHtml + '</td>' +
          '<td class="mini">止损 ' + stopHtml + '</td>' +
          '<td class="mini">止盈 ' + (t.target_price != null ? t.target_price : '—') + '</td>' +
          '<td>' + (sig || '<span class="mini">持有中</span>') + '</td>' +
          '<td class="mini">' + esc(p.entry_date || '—') + '</td>' +
          '<td>' + sellBtn + '</td></tr>';
      }).join('');
    // ★2026-08-11 百轮#40：组合绩效（净值/回撤 SVG 曲线 + 交易统计）
    // ★百轮#69：+ 沪深300 基准虚线 + 超额收益统计
    var pfb = document.getElementById('perf-box');
    if (pfb && d.perf && d.perf.ok) {
      var pf2 = d.perf;
      var st = pf2.stats || {};
      var svg = '';
      var dates = pf2.dates || [], navs = pf2.nav || [], dds = pf2.drawdown || [];
      var bnvs = pf2.bench_nav || [];
      if (navs.length >= 1) {
        var W = 660, H = 150, PAD = 34;
        var allV = navs.concat(bnvs.length ? bnvs : []);
        var maxV = Math.max.apply(null, allV), minV = Math.min.apply(null, allV);
        var minD = Math.min.apply(null, dds.concat([0]));
        maxV = Math.max(maxV, 1.0); minV = Math.min(minV, 1.0);
        if (maxV - minV < 0.02) { maxV += 0.01; minV -= 0.01; }
        function px(i) { return navs.length <= 1 ? W / 2 : PAD + (W - PAD * 2) * i / (navs.length - 1); }
        function pyN(v) { return 20 + (H - 40) * (maxV - v) / (maxV - minV); }
        function pyD(v) { var r = (v - minD) / (0 - minD); return 20 + (H - 40) * (1 - r * 0.6); }
        var npts = navs.map(function (v, i) { return px(i).toFixed(1) + ',' + pyN(v).toFixed(1); }).join(' ');
        var dpts = dds.map(function (v, i) { return px(i).toFixed(1) + ',' + pyD(v).toFixed(1); }).join(' ');
        var bpts = bnvs.length === navs.length
          ? bnvs.map(function (v, i) { return px(i).toFixed(1) + ',' + pyN(v).toFixed(1); }).join(' ')
          : '';
        var lastNav = navs[navs.length - 1];
        var lc = lastNav >= 1 ? 'var(--red)' : 'var(--green)';
        var lastDd = dds.length ? dds[dds.length - 1] : 0;
        svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;background:#FBFCFF;border-radius:8px;border:1px solid #E5E9F0">' +
          '<line x1="' + PAD + '" y1="' + pyN(1) + '" x2="' + (W - PAD) + '" y2="' + pyN(1) + '" stroke="#DDE8F7" stroke-width="1" stroke-dasharray="4 4"/>' +
          '<polyline points="' + dpts + '" fill="none" stroke="#0F6E56" stroke-width="1.4" opacity="0.75"/>' +
          (bpts ? '<polyline points="' + bpts + '" fill="none" stroke="#B9A46A" stroke-width="1.5" stroke-dasharray="6 3"/>' : '') +
          '<polyline points="' + npts + '" fill="none" stroke="var(--blue,#185FA5)" stroke-width="2"/>' +
          '<text x="' + PAD + '" y="18" font-size="10" fill="#8a94a6">净值</text>' +
          (bpts ? '<text x="' + (PAD + 26) + '" y="18" font-size="10" fill="#B9A46A">· · · 沪深300</text>' : '') +
          '<text x="' + (W - PAD) + '" y="18" font-size="11" text-anchor="end" fill="' + lc + '" font-weight="700">' +
          (lastNav >= 1 ? '+' : '') + ((lastNav - 1) * 100).toFixed(2) + '%</text>' +
          '<text x="' + PAD + '" y="' + (H - 8) + '" font-size="9" fill="#8a94a6">' + esc(String(dates[0] || '')) + '</text>' +
          '<text x="' + (W / 2) + '" y="' + (H - 8) + '" font-size="9" fill="#8a94a6" text-anchor="middle">回撤 ' + (lastDd * 100).toFixed(1) + '%</text>' +
          '<text x="' + (W - PAD) + '" y="' + (H - 8) + '" font-size="9" fill="#8a94a6" text-anchor="end">' + esc(String(dates[dates.length - 1] || '')) + '</text>' +
          '</svg>';
      }
      var benchHtml = (st.bench_ret != null)
        ? ' ｜ 基准沪深300 <b style="color:' + (st.bench_ret >= 0 ? 'var(--red)' : 'var(--green)') + '">' +
          (st.bench_ret >= 0 ? '+' : '') + (st.bench_ret * 100).toFixed(2) + '%</b>' +
          (st.excess != null ? ' ｜ 超额 <b style="color:' + (st.excess >= 0 ? 'var(--red)' : 'var(--green)') + '">' +
          (st.excess >= 0 ? '+' : '') + (st.excess * 100).toFixed(2) + '%</b>' : '')
        : '';
      // ★2026-08-12 百轮#109：逐日明细表（组合日收益——T+1 首日起自动填充）
      var dRows = (pf2.daily_rows || []).filter(function (r) { return r.comb_ret != null; });
      var dTable = '';
      if (dRows.length) {
        dTable = '<div style="margin-top:8px"><b>逐日明细</b>' +
          '<table style="width:100%;border-collapse:collapse;margin-top:4px;font-size:11px">' +
          '<thead><tr><th style="text-align:left">日期</th><th style="text-align:right">组合日收益</th></tr></thead><tbody>' +
          dRows.slice(-10).map(function (r) {
            var cls = r.comb_ret >= 0 ? 'var(--red)' : 'var(--green)';
            return '<tr><td>' + r.date + '</td><td style="text-align:right;color:' + cls + ';font-weight:700">' +
              (r.comb_ret >= 0 ? '+' : '') + (r.comb_ret * 100).toFixed(2) + '%</td></tr>';
          }).join('') + '</tbody></table></div>';
      }
      pfb.innerHTML =
        '<b>📈 组合绩效</b>：累计收益 <b style="color:' + ((st.total_ret || 0) >= 0 ? 'var(--red)' : 'var(--green)') + '">' +
        (st.total_ret != null ? ((st.total_ret >= 0 ? '+' : '') + (st.total_ret * 100).toFixed(2) + '%') : '—') + '</b>' +
        benchHtml +
        ' ｜ 最大回撤 <b style="color:var(--green)">' + (st.max_dd != null ? (st.max_dd * 100).toFixed(1) + '%' : '—') + '</b>' +
        ' ｜ 持有 ' + (st.days != null ? st.days + ' 天' : '—') +
        ' ｜ 已平仓 ' + (st.n_trades || 0) + ' 笔' +
        (st.winrate != null ? ' 胜率 ' + (st.winrate * 100).toFixed(0) + '%' : '') +
        (svg ? '<div style="margin-top:8px">' + svg + '</div>' : '') +
        dTable +
        (navs.length <= 1 ? '<div class="mini" style="margin-top:4px">仅 1 个交易日数据（今日买入）——明天起净值曲线与逐日明细自动延伸</div>' : '');
    }
  }
    var hb = document.getElementById('hist-box');
    if (hb) {
      var hist = ((d.portfolio || {}).history || []).slice().reverse();
      if (hist.length) {
        var hrows = hist.map(function (h) {
          var r = null;
          if (h.entry_price && h.exit_price) r = h.exit_price / h.entry_price - 1;
          var rCls = r == null ? '' : (r >= 0 ? 'style="color:var(--red)"' : 'style="color:var(--green)"');
          var rS = r != null ? ((r >= 0 ? '+' : '') + (r * 100).toFixed(1) + '%') : '—';
          return '<tr><td><b>' + esc(h.code) + '</b><span class="mini"> ' + esc(h.name || '') + '</span></td>' +
            '<td class="mini">' + esc(h.exit_date || '—') + '</td>' +
            '<td class="mini">' + (h.entry_price != null ? h.entry_price : '—') + '</td>' +
            '<td class="mini">' + (h.exit_price != null ? h.exit_price : '—') + '</td>' +
            '<td ' + rCls + ' style="font-weight:700">' + rS + '</td>' +
            '<td class="mini">' + esc(h.reason || '—') + '</td></tr>';
        }).join('');
        hb.innerHTML = '<table style="width:100%;border-collapse:collapse"><thead><tr><th>股票</th><th>卖出日</th><th>买入价</th><th>卖出价</th><th>收益</th><th>原因</th></tr></thead><tbody>' + hrows + '</tbody></table>';
      } else {
        hb.innerHTML = '暂无卖出记录——持仓行点「卖出」后自动落账';
      }
    }
  }

  // ★2026-08-11 百轮#39：一键卖出（触发止盈/止损确认卖出 → POST /api/portfolio/sell → 状态机 exit + history）
  function sellPos(code, price, alerted) {
    var msg = alerted
      ? '🔔 ' + code + ' 触发止盈/止损信号，确认按现价卖出？（将记录到历史交易）'
      : '确认卖出 ' + code + (price != null ? '（现价 ' + price + '）' : '') + '？';
    if (!confirm(msg)) return;
    var body = { code: code, reason: alerted ? 'tp_stop_trigger' : 'manual' };
    if (price != null) body.price = price;
    fetch('/api/portfolio/sell', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (r) {
      toastData(r.ok ? ('✅ ' + code + ' 已卖出（' + (r.status || 'exit') + '）') : ('❌ ' + (r.error || '卖出失败')));
      if (r.ok) setTimeout(function () { location.reload(); }, 800);
    }).catch(function (e) { toastData('❌ 卖出失败：' + e.message); });
  }
  window.sellPos = sellPos;   // ★IIFE 内函数挂 window，供 onclick 全局调用

  /* ================= 强因子直通榜（百轮#68：用户"强因子直通 Deck"完整形态） ================= */
  function renderStrongHits(d) {
    var box = document.getElementById('strong-hits-box');
    if (!box) return;
    if (!d.ok) { box.querySelector('.sh-body').innerHTML = '<div class="mini" style="padding:4px">— 直通数据不可用</div>'; return; }
    var body = box.querySelector('.sh-body');
    var top = (d.top || []).slice(0, 10);
    if (!top.length) { body.innerHTML = '<div class="mini" style="padding:4px">— 今日无跨家族强因子命中</div>'; return; }
    var stat = '共 <b>' + d.n + '</b> 只 ｜ <span style="color:#B54708">🔴 极强(≥6家族) ' + d.n_extreme + '</span> ｜ 🟡 强(4-5家族) ' + d.n_strong +
              ' ｜ 机会池内 ' + d.n_in_pool;
    body.innerHTML = '<div class="mini" style="margin-bottom:6px">' + stat + '</div>' +
      '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
      '<tr style="color:#8a6d1d;text-align:left"><th style="padding:3px 6px">代码</th><th>名称</th><th>家族数</th><th>命中因子</th><th>最强rank</th><th>标记</th></tr>' +
      top.map(function (r) {
        return '<tr style="border-top:1px solid #F0E7CF">' +
          '<td style="padding:3px 6px"><a href="/dashboard_stockcheck.html?code=' + encodeURIComponent(r.code) + '" style="color:#1c4d8f">' + esc(r.code) + '</a></td>' +
          '<td>' + esc(r.name || '') + '</td>' +
          '<td style="color:' + (r.n_family >= 6 ? '#B54708' : '#8a6d1d') + ';font-weight:600">' + r.n_family + '</td>' +
          '<td class="mini">' + esc((r.factors || []).slice(0, 4).join('、')) + (r.factors.length > 4 ? '…' : '') + '</td>' +
          '<td>' + r.min_rank.toFixed(2) + '</td>' +
          '<td>' + (r.in_pool ? '<span class="track-tech" style="background:#FFF3E0;color:#B54708;padding:1px 6px;border-radius:10px">机会池</span>' : '<span class="mini" style="color:#999">池外</span>') + '</td>' +
          '</tr>';
      }).join('') + '</table>';
  }

  /* ================= 入口 ================= */
  var RENDER = { opp: renderOpp, watch: renderWatch, tech: renderTech, holdings: renderHoldings, actions: renderActions };
  // ★2026-08-11 百轮#51：待处理面板动态化（止损触发/待审批/新突破 KPI + 区块）
  function renderActions(d) {
    if (!d.ok) return;
    // ★2026-08-11 百轮#52：今日操作简报（择时/待审批/止损/止盈/组合风险）
    var bb = document.getElementById('brief-box');
    if (bb) {
      fetch('/api/live/brief', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (b) {
          if (!b || !b.ok) return;
          var chips = (b.items || []).map(function (i) {
            var cls = i.level === 'high' ? '#C0392B' : (i.level === 'mid' ? '#D4A843' : '#0F6E56');
            return '<span style="display:inline-block;margin:2px 6px 2px 0;padding:3px 12px;border-radius:14px;border:1px solid ' + cls + ';color:' + cls + ';font-weight:600">' +
              esc(i.cat) + '：' + esc(i.msg) + '</span>';
          }).join('');
          bb.innerHTML = '<b>📋 今日简报</b>（' + b.ts.slice(0, 10) + '）' +
            (chips || '<span style="color:#0F6E56;font-weight:600">✅ 无待办事项——系统一切正常</span>');
        }).catch(function () {});
    }
    var k = document.getElementById('kpi-stop');
    if (k) k.textContent = d.n_stop;
    var k2 = document.getElementById('kpi-pending');
    if (k2) k2.textContent = d.n_pending;
    var k3 = document.getElementById('kpi-new');
    if (k3) k3.textContent = d.n_new;
    // 待审批区块（行：code/name/评分/线别/审批链接）
    var pb = document.getElementById('pending-tbody');
    if (pb && d.pending) {
      var rows = d.pending.map(function (p) {
        return '<tr><td><b>' + esc(p.code) + '</b><span class="mini"> ' + esc(p.name || '') + '</span></td>' +
          '<td class="mini">' + (p.line === 'short' ? '🎯短线' : '🏦长线') + (p.tier ? ' ' + p.tier : '') + '</td>' +
          '<td><b>' + (p.score != null ? (+p.score).toFixed(1) : '—') + '</b></td>' +
          '<td></td>' +
          '<td><a class="btn-link" href="/pitch.html?code=' + String(p.code || '').split('.')[0] + '" style="color:#0F6E56;font-weight:700">→ 审批</a></td></tr>';
      }).join('');
      pb.innerHTML = rows || '<tr><td colspan=5 class="mini">无待审批（全部已处理）</td></tr>';
    }
    // 止损触发区块
    var sb = document.getElementById('stop-tbody');
    if (sb && d.stop) {
      var srows = d.stop.map(function (s) {
        return '<tr><td><b>' + esc(s.code) + '</b><span class="mini"> ' + esc(s.name || '') + '</span></td>' +
          '<td class="mini">止损</td><td class="mini">' + esc(s.msg || s.status || '') + '</td>' +
          '<td><a class="btn-link" href="/dashboard_holdings.html" style="color:#b0774a">处理</a></td></tr>';
      }).join('');
      sb.innerHTML = srows || '<tr><td colspan=4 class="mini">无触发</td></tr>';
    }
  }
  // ★2026-08-11 百轮#17 择时横幅（决策相关页通用）：当前是否适合买入 + 环境适配（fetch /api/timing）
  function timingBanner() {
    if (['opp', 'watch', 'holdings', 'pitch', 'pool', 'actions'].indexOf(page) < 0) return;
    fetch('/api/timing', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (tm) {
      if (!tm || !tm.level) return;
      var host = document.querySelector('.header') || document.querySelector('.hero');
      if (!host) return;
      if (document.getElementById('lw-timing-banner')) return;
      var b = document.createElement('div'); b.id = 'lw-timing-banner';
      b.style.cssText = 'margin:10px 0;padding:8px 14px;border-radius:10px;font-size:12.5px;font-weight:600;'
        + (tm.level === '适合买入' ? 'background:#0F6E5622;border:1px solid #0F6E56;color:#0F6E56;'
          : (tm.level === '谨慎买入' ? 'background:#D4A84322;border:1px solid #D4A843;color:#8a6d1a;'
            : 'background:#C0392B22;border:1px solid #C0392B;color:#C0392B;'));
      var rf = tm.regime_fit && tm.regime_fit.map ? ' ｜ 🏷 环境适配 ' + ['breakout', 'tech_sentiment', 'reversal'].map(function (o) { return o + ' ' + (tm.regime_fit.map[o] || '0'); }).join(' · ') : '';
      var st = tm.style_state && tm.style_state.style && tm.style_state.style !== '未知' ? ' ｜ 🎨 风格 ' + tm.style_state.style : '';   // ★#127 风格门控展示
      b.innerHTML = tm.emoji + ' 市场择时：' + esc(tm.level) + '（' + tm.score + ' 分）' + rf + st;
      host.appendChild(b);
    }).catch(function () {});
  }
  timingBanner();
  // ★2026-08-11 百轮#8 数据流动联动：数据变化提示（轮询发现 ts/date 变化 → 右上角 toast "数据已更新"）
  var _lastTs = null, _lastDate = null;
  function toastData(msg) {
    var t = document.getElementById('lw-data-toast');
    if (!t) {
      t = document.createElement('div'); t.id = 'lw-data-toast';
      t.style.cssText = 'position:fixed;top:56px;right:18px;background:#0F6E56;color:#fff;padding:8px 16px;border-radius:20px;font-size:12px;z-index:999;box-shadow:0 4px 14px rgba(0,0,0,.18);transition:opacity .5s;';
      document.body.appendChild(t);
    }
    t.textContent = msg; t.style.display = 'block'; t.style.opacity = '1';
    clearTimeout(t._tm); t._tm = setTimeout(function () { t.style.opacity = '0'; }, 2600);
  }
  function refresh() {
    // ★2026-08-11 百轮#30：未注册页（pitchtrack 等有自身刷新逻辑）不发 /api/live 请求（避免 404 冗余）
    if (!RENDER[page] && page !== 'pool') return;
    fetch('/api/live/' + page, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        _lastData = d;
        // ★2026-08-12 百轮#72：审批状态同步 + 完成后重渲染（观察池回显"已审批"，避免首屏空）
        loadDecided(function () {
          try {
            if (page === 'pool') {
              try { RENDER.opp(d.opp, document.getElementById('pool-opp')); } catch (e) {}
              try { RENDER.watch(d.watch, document.getElementById('pool-watch')); } catch (e) {}
              try { RENDER.tech(d.tech, document.getElementById('pool-tech')); } catch (e) {}
            } else {
              try { RENDER[page](d); } catch (e) {}
            }
          } catch (e) {}
        });
        // ★百轮#68：opp 页联动强因子直通榜（独立数据源，5min 缓存）
        if (page === 'opp') {
          fetch('/api/live/strong_hits', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (sd) { try { renderStrongHits(sd); } catch (e) {} })
            .catch(function () {});
        }
        // ★数据联动提示：数据文件变化（file 名）→ 提示（ts 是请求时间不能用）
        if (d && d.ok && (d.file || d.date)) {
          var _sig = d.file || d.date;
          if (_lastTs !== null && _lastTs !== _sig) {
            toastData('🔄 数据已更新' + (d.date ? '（数据日 ' + d.date + '）' : ''));
          }
          _lastTs = _sig;
        }
      })
      .catch(function () { /* 网络失败静默 */ });
  }
  refresh();
  setInterval(refresh, INTERVAL);
})();
