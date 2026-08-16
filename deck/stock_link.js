/* deck/stock_link.js — 全站个股代码联动组件（2026-08-12 用户需求#181）
 * 板块数据互通：任意页面出现的股票代码自动可点 → 弹窗展示个股全息
 * （评级/维度/远期池三池/止损止盈/因子归因/审批历史/远期收益）
 * 用法：<script src="/stock_link.js"></script>（自动扫描 .code-link 或含股票代码的元素）
 */
(function () {
  const RE = /(\d{6}\.(?:SZ|SH|BJ))|(\d{6})/g;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    return (v * 100 > 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
  }

  /* 弹窗 */
  let modal = null;
  function showModal(html) {
    if (!modal) {
      modal = document.createElement('div');
      modal.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(2,8,23,.55);z-index:9999;display:flex;align-items:center;justify-content:center';
      modal.addEventListener('click', function (e) { if (e.target === modal) hideModal(); });
      document.body.appendChild(modal);
    }
    modal.innerHTML = '<div style="background:#fff;border-radius:14px;max-width:640px;width:92%;max-height:82vh;overflow:auto;padding:18px 22px;box-shadow:0 20px 60px rgba(0,0,0,.35)">' + html + '</div>';
    modal.style.display = 'flex';
  }
  function hideModal() { if (modal) modal.style.display = 'none'; }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideModal(); });

  async function openStock(code) {
    showModal('<div style="text-align:center;color:#64748B">⏳ 正在聚合 ' + esc(code) + ' 全板块数据…</div>');
    try {
      const r = await fetch('/api/stock_check?code=' + encodeURIComponent(code));
      const d = await r.json();
      if (!d.ok) { showModal('<h3 style="margin:0 0 10px">' + esc(code) + '</h3><div style="color:#C0392B">' + esc(d.error || '查询失败') + '</div>'); return; }
      const lk = d.linkage || {};
      const poolCN = { auto_pitch: '🅰 自动入池', machine_top01: '🅱 机器强因子', human_select: '🅲 人工选择' };
      const sp = lk.pool_stop_plan || d.stop_plan || {};
      const fwd = d.forward || {};
      const rows = [
        ['📌 综合评级', '<b>' + esc(d.rating || '—') + '</b>' + (d.conclusion ? '<div class="mini" style="color:#64748B;margin-top:4px">' + esc(d.conclusion) + '</div>' : '')],
        ['🎯 远期池', lk.pool_type ? '<b>' + esc(poolCN[lk.pool_type] || lk.pool_type) + '</b><div class="mini">T+1 ' + fmtPct((fwd.t1 || {}).ret) + ' · T+5 ' + fmtPct((fwd.t5 || {}).ret) + '</div>' : '<span style="color:#94A3B8">不在远期池</span>'],
        ['🛑 止损止盈', sp ? '<div class="mini">' + (sp.stop_loss_pct ? esc(sp.stop_loss_pct) + '% 硬止损 · ' : '') + (sp.time_stop_weeks ? esc(sp.time_stop_weeks) + ' 周时间止损 · ' : '') + (sp.max_drawdown_pct ? esc(sp.max_drawdown_pct) + '% 回撤' : '') + (sp.trailing_ma ? ' · 追踪MA' + esc(sp.trailing_ma) : '') + '</div>' : '—'],
        ['🧬 因子归因', lk.factors ? '<div class="mini">' + Object.entries(lk.factors).map(function (x) { return esc(x[0]) + '=' + (+x[1]).toFixed(2); }).join(' · ') + (lk.signal_family ? '（' + esc(lk.signal_family) + '家族）' : '') + '</div>' : '<span style="color:#94A3B8">无归因记录</span>'],
        ['📋 审批历史', lk.decisions && lk.decisions.length ? '<div class="mini">' + lk.decisions.map(function (x) { return x.date + (x.env ? ' @' + esc(x.env) : ''); }).join('<br>') + '</div>' : '<span style="color:#94A3B8">无审批记录</span>'],
        ['📊 持仓', d.position ? '<div class="mini">' + esc(d.position.status || '') + ' · 成本 ' + esc(d.position.cost || '—') + '</div>' : '<span style="color:#94A3B8">未持有</span>'],
      ];
      const html =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
        '<h3 style="margin:0">' + esc(d.name || d.code) + ' <span style="color:#64748B;font-size:13px">' + esc(d.code) + '</span></h3>' +
        '<button onclick="document.querySelector(\'#sl-modal-close\').click()" style="background:none;border:none;font-size:18px;cursor:pointer;color:#64748B" title="关闭">✕</button></div>' +
        '<div id="sl-modal-close" style="display:none"></div>' +
        rows.map(function (r) {
          return '<div style="padding:8px 0;border-bottom:1px solid #EEF2F7;font-size:13px"><div style="color:#64748B;font-size:11px;margin-bottom:2px">' + r[0] + '</div>' + r[1] + '</div>';
        }).join('') +
        '<div style="margin-top:12px;font-size:11px;color:#94A3B8;text-align:center">板块数据联动 · 评级/三池/止损/因子/审批/持仓</div>';
      showModal(html);
    } catch (e) {
      showModal('<h3 style="margin:0 0 10px">' + esc(code) + '</h3><div style="color:#C0392B">连接失败：' + esc(e.message) + '</div>');
    }
  }

  /* 扫描并绑定：已有 .code-link 直接绑；含 6 位数字的元素加可点样式 */
  function scan() {
    const linkable = document.querySelectorAll('.code-link');
    linkable.forEach(function (el) {
      if (el.dataset.slBound) return;
      el.dataset.slBound = '1';
      el.style.cursor = 'pointer';
      el.style.textDecoration = 'underline dotted';
      el.style.textUnderlineOffset = '3px';
      el.addEventListener('click', function () {
        const c = el.dataset.code || el.textContent.trim().match(/\d{6}/);
        if (c) openStock(String(c[0] || c));
      });
    });
  }
  scan();
  if (document.body) new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  window.StockLink = { open: openStock };
})();
