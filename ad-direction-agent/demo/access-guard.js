(function () {
  'use strict';
  // 访问控制补丁 — 删除本文件即自动失效
  var ALLOWED = ['1064', '1063', '1089'];
  var uid = (new URL(location.href)).searchParams.get('userId') || '';
  if (ALLOWED.indexOf(uid) !== -1) return;

  var overlay = document.createElement('div');
  overlay.id = '_accessGuardOverlay';
  overlay.innerHTML =
    '<div style="'
    + 'position:fixed;inset:0;z-index:99999;'
    + 'background:rgba(0,0,0,.7);'
    + 'display:flex;align-items:center;justify-content:center;'
    + 'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
    + '<div style="'
    + 'background:#fff;border-radius:12px;padding:40px 48px;'
    + 'max-width:420px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3);">'
    + '<div style="font-size:48px;margin-bottom:16px;">&#x1F6AB;</div>'
    + '<h2 style="margin:0 0 12px;font-size:20px;color:#1F2937;">v2.1 测试环境专用</h2>'
    + '<p style="margin:0;font-size:14px;color:#6B7280;line-height:1.6;">'
    + '请到 <strong>v2</strong> 调整广告</p>'
    + '</div></div>';
  document.documentElement.appendChild(overlay);
})();
