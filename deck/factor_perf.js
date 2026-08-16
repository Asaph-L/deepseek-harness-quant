/* deck/factor_perf.js — ★因子推荐质量（2026-08-13 #275）
   读 /api/live/factor_perf → 渲染"因子×pitch×远期业绩"表
   独立 JS 文件（避免生成器 f-string 花括号转义——#186/#265 教训根治） */
(function () {
  var box = document.getElementById('factor-perf-box');
  if (!box) return;
  box.innerHTML = '⏳ 加载中…';
  fetch('/api/live/factor_perf').then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { box.innerHTML = '❌ ' + d.err; return; }
    var rows = (d.factors || []).map(function (f) {
      var t1 = f.t1_avg != null
        ? '<b style="color:' + (f.t1_avg >= 0 ? 'var(--red)' : 'var(--green)') + '">' + (f.t1_avg * 100).toFixed(2) + '%</b>'
        : '<span style="opacity:.5">—</span>';
      var t5 = f.t5_avg != null
        ? '<b style="color:' + (f.t5_avg >= 0 ? 'var(--red)' : 'var(--green)') + '">' + (f.t5_avg * 100).toFixed(2) + '%</b>'
        : '<span style="opacity:.5">⏳ 未到期</span>';
      var win = f.t5_win != null ? (f.t5_win * 100).toFixed(0) + '%' : '—';
      var ex = f.excess_avg != null
        ? '<span style="color:' + (f.excess_avg >= 0 ? 'var(--red)' : 'var(--green)') + '">' + (f.excess_avg * 100).toFixed(2) + '%</span>'
        : '<span style="opacity:.5">—</span>';
      return '<tr><td><b>' + f.factor + '</b></td><td>' + f.n_pitch + '</td><td>' + f.n_done_t5 + '</td><td>' + t1 + '</td><td>' + t5 + '</td><td>' + win + '</td><td>' + ex + '</td></tr>';
    }).join('');
    box.innerHTML =
      '<div class="mini" style="margin-bottom:6px;opacity:.7">' + d.n_factors + ' 个因子 · 最近 80 条明细 · T+5 于 08-14 首批到期自动填充</div>' +
      '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>' +
      '<th>因子</th><th>pitch 次数</th><th>T+5 完成</th><th>T+1 平均</th><th>T+5 平均</th><th>T+5 胜率</th><th>超额</th>' +
      '</tr></thead><tbody>' + (rows || '<tr><td colspan=7 class="mini">暂无数据</td></tr>') + '</tbody></table>';
  }).catch(function (e) { box.innerHTML = '❌ 加载失败: ' + e; });
})();
