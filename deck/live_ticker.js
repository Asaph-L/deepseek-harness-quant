/* ══════════════════════════════════════════════════════════════
   deck/live_ticker.js — 动态行情条 + 信息轮播（2026-08-13 #315 v3）
   彭博终端特征：黑底亮字 · 高速滚动 · 红绿强烈对比 · 紧迫倒计时
   中国习惯配色：红=涨/机会 绿=跌/风险（regional_conventions）
   ① 顶部滚动行情条：14s 无缝高速滚动 + 闪烁分隔符 + 机会稀缺标注
   ② 信息轮播：3s/条 快速切换 + 倒计时元素（T+5/日历窗口）
   ③ 数据源：timing_dash（温度/宽度/拥挤度/评分）+ portal_dash（机会/Pitch），30s 刷新
   ★#315 修复：温度/宽度/拥挤度原取 st.temp/st.ind_breadth_60（style_state 无此字段=数据未接入）
      → 改从 /api/live/timing_dash 的 temp_hist/width_hist/crowd_hist/score_hist 取末值；去 emoji
   ══════════════════════════════════════════════════════════════ */
(function () {
  var els = document.querySelectorAll('.lt-bar');
  if (!els.length) return;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function last(a) { return (a && a.length) ? a[a.length - 1] : null; }
  /* 倒计时：距某日期的自然日（紧迫感元素） */
  function daysTo(dateStr) {
    if (!dateStr) return null;
    var t = new Date(dateStr).getTime();
    var d = Math.ceil((t - Date.now()) / 86400000);
    return d >= 0 ? d : null;
  }

  function renderBar(bar, td, pd) {
    var th = td.temp_hist || [], wh = td.width_hist || [], ch = td.crowd_hist || [], sh = td.score_hist || [];
    var temp = last(th), width = last(wh), crowd = last(ch), score = last(sh);
    var pools = (pd && pd.pools) || {};
    var opp = (pools.opp_pool && pools.opp_pool.n != null) ? pools.opp_pool.n : '—';
    var pitch = (pools.pitch && pools.pitch.n != null) ? pools.pitch.n : '—';
    // 彭博式条目（红=机会 绿=风险）
    var items = [
      ['择时', score != null ? score.toFixed(1) : '—', score != null ? (score >= 70 ? '#ff4d4f' : (score >= 55 ? '#faad14' : '#52c41a')) : '#64748b'],
      ['温度', temp != null ? temp.toFixed(1) + '/100' : '—', temp != null ? (temp <= 30 ? '#ff4d4f' : (temp <= 60 ? '#faad14' : '#52c41a')) : '#64748b'],
      ['宽度', width != null ? (width * 100).toFixed(0) + '%' : '—', width != null ? (width <= 0.2 ? '#ff4d4f' : '#38bdf8') : '#64748b'],
      ['拥挤度', crowd != null ? (crowd * 100).toFixed(1) + '%' : '—', crowd != null ? (crowd >= 0.8 ? '#ff4d4f' : (crowd >= 0.5 ? '#faad14' : '#52c41a')) : '#64748b'],
      ['机会', opp, '#ff4d4f'],
      ['Pitch', pitch, '#ff4d4f']
    ];
    var html = '<div class="lt-track">';
    for (var copy = 0; copy < 2; copy++) {
      items.forEach(function (t) {
        html += '<span class="lt-item"><span class="lt-dot" style="background:' + t[2] + '"></span>' +
          '<b>' + t[0] + '</b> <span class="lt-val" style="color:' + t[2] + '">' + t[1] + '</span></span>' +
          '<span class="lt-sep">|</span>';
      });
      var lvl = (td.timing && td.timing.level) || '—';
      var zone = String(td.temp_zone || '').replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FE0F}]/gu, '');
      html += '<span class="lt-item lt-msg"><b>环境</b> <span class="lt-val">' + esc(lvl) + ' · ' + esc(zone) + '</span></span><span class="lt-sep">|</span>';
    }
    html += '</div>';
    bar.innerHTML = html;
  }

  function loadBar(bar) {
    Promise.all([
      fetch('/api/live/timing_dash').then(function (r) { return r.json(); }),
      fetch('/api/live/portal_dash').then(function (r) { return r.json(); })
    ]).then(function (res) {
      renderBar(bar, res[0] || {}, res[1] || {});
    }).catch(function () {});
  }

  /* 信息轮播：3s/条 快速切换，倒计时 + high 置顶（彭博紧迫感） */
  var rotators = document.querySelectorAll('.lt-rotator');
  function loadRotator(rot) {
    var items = [];
    fetch('/api/live/brief').then(function (r) { return r.json(); }).then(function (d) {
      items = (d && d.items) || [];
      fetch('/api/live/alerts').then(function (r) { return r.json(); }).then(function (a) {
        var al = (a && a.alerts) || [];
        al.forEach(function (x) {
          if (false && x.level === 'high') items.unshift({ cat: '告警 ' + x.cat, msg: x.msg, level: 'high' });
        });
        // 倒计时项置顶（T+5 复核 08-14）
        var t5 = daysTo('2026-08-14');
        if (t5 != null) {
          items.unshift({ cat: 'T+5 复核', msg: t5 + ' 天后首批 5 只到期自动复核（08-14）', level: 'mid' });
        }
        if (items.length) startRotate(rot, items);
      }).catch(function () {});
    }).catch(function () {});
  }
  function startRotate(rot, items) {
    var i = 0;
    function show() {
      var it = items[i % items.length];
      var color = it.level === 'high' ? '#ff4d4f' : (it.level === 'mid' ? '#faad14' : '#0f172a');
      var bg = it.level === 'high' ? '#fef2f2' : (it.level === 'mid' ? '#fffbeb' : '#f8fafc');
      rot.style.background = bg;
      rot.innerHTML = '<span class="lt-rot-cat" style="color:' + color + '">' + esc(it.cat || '') + '</span>' +
        ' <span class="lt-rot-msg">' + esc(it.msg || '') + '</span>';
      i++;
    }
    show();
    setInterval(show, 3000);
  }

  els.forEach(function (bar) {
    loadBar(bar);
    setInterval(function () { loadBar(bar); }, 30000);
    // 数字跳动
    bar.addEventListener('DOMNodeInserted', function () {
      var val = bar.querySelectorAll('.lt-val');
      val.forEach(function (v, idx) {
        v.style.transition = 'opacity .3s';
        v.style.opacity = '0';
        setTimeout(function () { v.style.opacity = '1'; }, 30 + idx * 20);
      });
    }, false);
  });
  document.querySelectorAll('.lt-rotator').forEach(function (r) {
    loadRotator(r);
  });
})();
