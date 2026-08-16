/* deck/nav_common.js — 统一导航条渲染（任务包 G U1-5，2026-08-10 总指导）
 * 用法：<div class="lw-nav" data-page="机会池"></div><script src="/nav_common.js"></script>
 * 自动：页面名 + 回门户 + 数据截至（bars 最新交易日，调 /api/system_live 缓存版或本地计算）
 */
(function () {
  var PAGE_CN = {
    "portal": "总览门户", "opp": "机会池", "factors": "因子监控", "watch": "观察池",
    "holdings": "持有池", "tech": "科技池", "pitchtrack": "远期池", "ranks": "涨跌幅榜",
    "backtest": "回测证据", "actions": "待处理", "live": "系统监控", "stockcheck": "个股检测",
    "auction": "竞价信号", "history": "操作历史", "pitch": "Pitch 审批", "terms": "术语表",
    "pool": "池子总览", "monitor": "监控总览", "research": "研究总览",   // ★2026-08-10 合并页
    "help": "说明", "hold": "持仓", "factors2": "因子池",   // ★#151 新架构页
  };
  var NAV_LINKS = [
    { "k": "portal", "u": "/", "t": "门户" },
    { "k": "pitch", "u": "/pitch", "t": "决策" },
    { "k": "hold", "u": "/holdings", "t": "持仓" },
    { "k": "factors", "u": "/factors", "t": "因子池" },
    { "k": "help", "u": "/help", "t": "说明" },
  ];
  function mount() {
    var navs = document.querySelectorAll('.lw-nav');
    if (!navs.length) return;
    navs.forEach(function (nav) {
      var page = nav.getAttribute('data-page') || '';
      var cn = PAGE_CN[page] || page || '系统';
      var home = '<a class="lw-home" href="/">🏠 门户</a>';
      var links = NAV_LINKS.map(function (l) {
        var on = (l.k === page || (page === 'opp' && l.k === 'pitch')) ? ' class="lw-on"' : '';
        return '<a class="lw-link"' + on + ' href="' + l.u + '">' + l.t + '</a>';
      }).join('');
      var html = '<a class="lw-logo" href="/">LW<b>Quant</b></a>'
        + '<span class="lw-navlinks">' + links + '</span>'
        + '<span class="lw-sp"></span>'
        + '<span class="lw-date">数据截至 <b id="lw-date-' + Math.random().toString(36).slice(2, 7) + '">…</b></span>'
        + home;
      nav.innerHTML = html;
    });
    // 数据截至（bars 最新交易日）——/api/live/opp 的 date（全系统已统一为 bars 最近交易日口径）
    try {
      fetch('/api/live/opp').then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.date) {
          var el = document.querySelector('.lw-date b');
          if (el) el.textContent = d.date;
        }
      }).catch(function () {});
    } catch (e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();

/* ★#156 StyleKit 动画全站接入：旧页卡片统一 stagger 入场 + hover 抬升
   （ui_common.css 已合并 anim_common 动画类；仅对 .card 类生效，不干扰布局） */
(function () {
  function animOld() {
    try {
      var gs = document.querySelectorAll('.cards, .grid, .wrap');
      gs.forEach(function (g, gi) {
        g.style.setProperty('--stagger-delay', (60 + gi * 40) + 'ms');
        var cards = g.querySelectorAll('.card, .kpi, .pos');
        cards.forEach(function (c, j) {
          c.style.setProperty('--stagger-index', j);
          c.classList.add('anim-stagger');
          c.classList.add('anim-hover-lift');
        });
      });
    } catch (e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', animOld);
  } else {
    animOld();
  }
})();
