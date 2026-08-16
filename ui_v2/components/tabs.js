/* ui_v2/components/tabs.js — Tab 容器组件（UI v2 标准 #20 逻辑层级）
   功能：Tab 切换 + 懒加载（激活即拉数据——api.js 30s 缓存兜底）+ 徽章计数
   用法：
     var api = LW.tabs.init('tabs-demo', [
       { id: 'timing', label: '择时', badge: 0, load: function(box){ ... } },
       { id: 'opp', label: '机会池', badge: 418, load: function(box){ ... } }
     ]);
     api.reload();   // 重载当前激活 Tab（数据刷新——审批/卖出后局部刷新用）
   load(box) 在激活时调用，box 是内容容器。 */
(function (win) {
  'use strict';

  function init(containerId, tabs, opts) {
    var wrap = document.getElementById(containerId);
    if (!wrap) return null;
    opts = opts || {};
    var active = null;

    // 结构：tab 头 + 内容
    // ★2026-08-14 菜单栏显示不全修复：原 overflow-x:auto 在窄窗口会把尾部 tab
    //   （Pitch 长/Pitch 短/机器池）挤到隐藏滚动区，用户看不到 → 改 flex-wrap 自动换行，
    //   所有 tab 恒可见（滚动改为换行，配合白色背景区分两行）
    var head = document.createElement('div');
    head.style.cssText = 'display:flex;flex-wrap:wrap;gap:2px 4px;border-bottom:2px solid var(--line);margin-bottom:14px;background:transparent;padding:0 2px';
    var body = document.createElement('div');
    wrap.appendChild(head);
    wrap.appendChild(body);

    // 每个 Tab 独立内容容器（box）——reload 时只重建当前 box
    function getBox(t) {
      var box = document.getElementById('tab-' + t.id);
      if (!box) {
        box = document.createElement('div');
        box.id = 'tab-' + t.id;
      }
      return box;
    }

    function render(t) {
      var box = getBox(t);
      body.innerHTML = '';
      body.appendChild(box);   // 重新挂回（innerHTML 清空会移除旧 box）
      active = t;
      box.className = 'anim-fade-in-up';   // ★#166 Tab 切换入场动画（每次激活重新淡入上滑）
      if (t.load) {
        box.innerHTML = '<div class="mini" style="padding:12px;opacity:.6"> 加载 ' + t.label + '…</div>';
        try { t.load(box); }
        catch (e) { box.innerHTML = '<div class="mini" style="color:var(--red)"> ' + e + '</div>'; }
      }
    }

    function highlight(btn) {
      head.querySelectorAll('button').forEach(function (b) { b.style.color = 'var(--muted)'; b.style.borderBottomColor = 'transparent'; });
      btn.style.color = 'var(--navy)'; btn.style.borderBottomColor = 'var(--navy)';
    }

    tabs.forEach(function (t, i) {
      // ★2026-08-14 链接型 Tab（href）：渲染为 <a> 跳转，不参与切换（用于隐藏入口，如主观量化 Beta）
      if (t.href) {
        var a = document.createElement('a');
        a.href = t.href;
        a.style.cssText =
          'padding:9px 16px;border:none;background:none;font-size:14px;font-weight:600;' +
          'color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap;text-decoration:none';
        a.innerHTML = t.label;
        head.appendChild(a);
        return;
      }
      var btn = document.createElement('button');
      btn.style.cssText =
        'padding:9px 16px;border:none;background:none;cursor:pointer;font-size:14px;font-weight:600;' +
        'color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap';
      btn.innerHTML = t.label + (t.badge ? ' <b style="color:var(--navy)">' + t.badge + '</b>' : '');
      btn.onclick = function () {
        highlight(btn);
        render(t);
      };
      head.appendChild(btn);
      if (i === 0) { highlight(btn); render(t); }  // 默认第一个 Tab
    });

    var api = {
      reload: function () {
        if (active) { render(active); }
      },
      getActive: function () { return active; }
    };
    win.LW = win.LW || {};
    win.LW.tabs = win.LW.tabs || {};
    win.LW.tabs._instances = win.LW.tabs._instances || {};
    win.LW.tabs._instances[containerId] = api;
    return api;
  }

  win.LW = win.LW || {};
  win.LW.tabs = win.LW.tabs || {};
  win.LW.tabs.init = init;
})(window);
