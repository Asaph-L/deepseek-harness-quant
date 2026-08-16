/* ui_v2/components/icons.js — SVG 几何图标库（★2026-08-13 #309 专业化去 emoji 后的视觉替代）
   用法：LW.icons.home / LW.icons.target ...（返回内联 SVG 字符串，stroke=currentColor 继承文字颜色）
   设计：24x24 线条风格，简洁几何，专业金融终端质感（不花哨） */
(function () {
  'use strict';
  var S = ' viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"';
  function svg(inner) { return '<svg' + S + '>' + inner + '</svg>'; }

  window.LW = window.LW || {};
  window.LW.icons = {
    home:     svg('<path d="M3 10.5L12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>'),
    target:   svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>'),
    bar:      svg('<path d="M5 20V10M12 20V4M19 20v-8"/>'),
    molecule: svg('<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7.5l2.5 8M16 7.5l-2.5 8M7.5 8h9"/>'),
    calendar: svg('<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4M16 3v4M4 11h16"/>'),
    gauge:    svg('<path d="M5 19a9 9 0 1114 0"/><path d="M12 14l4-4"/><circle cx="12" cy="14" r="1.5"/>'),
    doc:      svg('<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/>'),
    db:       svg('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'),
    help:     svg('<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 114 1.8c-.8.4-1.5 1-1.5 2.2"/><circle cx="12" cy="17" r="0.4" fill="currentColor"/>'),
    chart:    svg('<path d="M4 20V10M4 20h16"/><path d="M7 16l4-5 3 3 5-7"/>'),
    funnel:   svg('<path d="M4 5h16l-6 7v5l-4 2v-7z"/>'),
    cpu:      svg('<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>'),
    etf:      svg('<path d="M4 20h16"/><rect x="6" y="11" width="3" height="6"/><rect x="11" y="6" width="3" height="11"/><rect x="16" y="14" width="3" height="3"/><path d="M6 7l4 2 4-3 4 2"/>')
  };
})();
