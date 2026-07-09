/* :8012 体验补丁 — 仅做一件事:
 * 让上层 LLM 接口(tactics/options, execution/options 等)在 MCP 网关 502/超时时
 * **5 秒内**降级为合法空响应,让前端 loadAll 走完 → 主内容显示 → tab5 历史快照正常渲染。
 *
 * 效果:跟 prod 公网 advertAgentV2 快照页一样,直接看到广告活动分析,上层 tab 空但不卡死。
 * 不改 vendor HTML 主体,不改 UI 结构,不触发 LLM。
 */
(function () {
  'use strict';

  // 哪些接口要短超时降级
  const FAST_FALLBACK_PATHS = [
    '/tactics/options',
    '/tactics/recommend',
    '/execution/options',
    '/execution/recommend',
    '/diagnosis',
    '/p3/recommend',
  ];
  const FAST_TIMEOUT_MS = 5000;

  // 各接口返回最小合法响应(字段宽松,前端容忍)
  function makeFallback(path, body) {
    const asin = (body && body.asin) || '';
    if (path.includes('/tactics/')) {
      return {
        asin, dimensions: [], strategy_context: {},
        current_selection: null, target_scores: [], keyword_analysis: [],
        scoring_error: null, llm_status: { ok: false, reason: 'mcp_gateway_unavailable' },
        data_completeness: {}, partial_failures: [
          { name: 'MCP', reason: '网关暂时不可用,已降级展示快照' },
        ], data_freshness: 'fallback',
      };
    }
    if (path.includes('/execution/')) {
      return {
        asin, directions: [], current_selection: null,
        target_acos: null, daily_budget: null,
        partial_failures: [{ name: 'MCP', reason: '网关暂时不可用' }],
        data_freshness: 'fallback',
      };
    }
    if (path.includes('/diagnosis')) {
      return {
        asin, ok: true, items: [],
        partial_failures: [{ name: 'MCP', reason: '网关暂时不可用' }],
        data_freshness: 'fallback',
      };
    }
    if (path.includes('/p3/')) {
      return { asin, ok: true, target_acos: null, daily_budget: null };
    }
    return { ok: false };
  }

  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    const matched = FAST_FALLBACK_PATHS.find(p => url.includes(p));
    if (!matched) return _origFetch(input, init);

    // 拿原始 body(用于 fallback 取 asin)
    let parsedBody = null;
    try {
      if (init && init.body) parsedBody = JSON.parse(init.body);
    } catch (_) {}

    // 5 秒短超时
    const ctrl = new AbortController();
    const opts = Object.assign({}, init || {}, { signal: ctrl.signal });
    const timer = setTimeout(() => ctrl.abort(), FAST_TIMEOUT_MS);

    return _origFetch(input, opts).then(
      (r) => { clearTimeout(timer); return r; },
      (err) => {
        clearTimeout(timer);
        // 超时 / 网络错误 → 给一个合法空响应,让前端 loadAll 不卡死
        console.warn('[codex-ux] fast-fallback', matched, err && err.name);
        const body = JSON.stringify(makeFallback(matched, parsedBody));
        return new Response(body, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
    );
  };

  console.log('[codex-ux] 快速 fallback 已启用:', FAST_FALLBACK_PATHS.join(', '));
})();

/* ── 标题 v3.1 → v2(蓝色) ───────────────────────────────────────── */
(function () {
  function apply() {
    var title = document.querySelector(".topbar-title");
    var pill = title && title.querySelector(".version-pill");
    if (!pill) return false;
    // v2 蓝色 pill
    pill.textContent = "v2";
    pill.style.background = "linear-gradient(135deg,#DBEAFE,#BFDBFE)";
    pill.style.color = "#1D4ED8";
    // 整个标题字号加大
    title.style.fontSize = "18px";
    title.style.fontWeight = "700";
    // 备注:hint 说明只能手动 + 慢
    if (!title.querySelector(".codex-header-hint")) {
      var hint = document.createElement("span");
      hint.className = "codex-header-hint";
      hint.textContent = "(目前只能手动,数据拉取分析会稍慢,肯请谅解)";
      hint.style.cssText = "font-size:11px;color:#94A3B8;margin-left:10px;font-weight:400;letter-spacing:0";
      title.appendChild(hint);
    }
    return true;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }
  // 兜底:DOM 还没渲染时,1s 内轮询
  var tries = 0;
  var iv = setInterval(function () {
    if (apply() || ++tries > 10) clearInterval(iv);
  }, 100);
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:micro-interactions — 微交互补丁(4 项)
 * 1) Modal 淡入缩放 (feedback-overlay)
 * 2) 卡片 hover 提升 (.card)
 * 3) Tab 切换 fade (.tab-panel.active)
 * 4) Toast 通知(拦截 window.alert,不影响 confirm)
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  // ── 注入 CSS ──────────────────────────────────────────────────
  var css = document.createElement("style");
  css.textContent = [
    /* 1) Modal 淡入缩放 —— 每次从 hidden 移除时触发 */
    "@keyframes codexFadeInScale {",
    "  from { opacity: 0; transform: scale(.94); }",
    "  to   { opacity: 1; transform: scale(1); }",
    "}",
    "@keyframes codexOverlayFadeIn {",
    "  from { opacity: 0; } to { opacity: 1; }",
    "}",
    ".feedback-overlay:not(.hidden) { animation: codexOverlayFadeIn .18s ease-out; }",
    ".feedback-overlay:not(.hidden) .feedback-modal {",
    "  animation: codexFadeInScale .22s cubic-bezier(.16,1,.3,1);",
    "  transform-origin: center center;",
    "}",

    /* 2) 卡片 hover 反馈 —— 轻微提升,不影响布局 */
    ".card {",
    "  transition: transform .2s ease, box-shadow .2s ease;",
    "  will-change: transform;",
    "}",
    ".card:hover {",
    "  transform: translateY(-1px);",
    "  box-shadow: 0 4px 12px rgba(15,23,42,.08), 0 1px 3px rgba(15,23,42,.04);",
    "}",

    /* 3) Tab 面板切换 fade+slide */
    "@keyframes codexTabFadeIn {",
    "  from { opacity: 0; transform: translateY(4px); }",
    "  to   { opacity: 1; transform: translateY(0); }",
    "}",
    ".tab-panel.active { animation: codexTabFadeIn .25s ease-out; }",

    /* 4) Toast 通知 */
    "#codexToastLayer {",
    "  position: fixed; top: 20px; right: 20px; z-index: 99999;",
    "  display: flex; flex-direction: column; gap: 10px;",
    "  pointer-events: none;",
    "}",
    ".codex-toast {",
    "  min-width: 260px; max-width: 480px;",
    "  padding: 12px 16px; border-radius: 8px;",
    "  background: #fff; color: #1E293B;",
    "  box-shadow: 0 8px 24px rgba(15,23,42,.16), 0 2px 6px rgba(15,23,42,.08);",
    "  font-size: 13px; line-height: 1.5;",
    "  border-left: 4px solid #3B82F6;",
    "  display: flex; align-items: flex-start; gap: 10px;",
    "  animation: codexToastIn .28s cubic-bezier(.16,1,.3,1);",
    "  pointer-events: auto;",
    "  word-break: break-word;",
    "}",
    ".codex-toast.success { border-left-color: #10B981; }",
    ".codex-toast.error   { border-left-color: #EF4444; }",
    ".codex-toast.warn    { border-left-color: #F59E0B; }",
    ".codex-toast.leaving { animation: codexToastOut .22s ease-in forwards; }",
    ".codex-toast .ico { font-size: 15px; line-height: 1; margin-top: 1px; }",
    ".codex-toast .msg { flex: 1; }",
    ".codex-toast .close {",
    "  cursor: pointer; color: #94A3B8; font-size: 14px; padding: 0 2px;",
    "  line-height: 1; margin-left: 4px;",
    "}",
    ".codex-toast .close:hover { color: #475569; }",
    "@keyframes codexToastIn {",
    "  from { opacity: 0; transform: translateX(30px); }",
    "  to   { opacity: 1; transform: translateX(0); }",
    "}",
    "@keyframes codexToastOut {",
    "  from { opacity: 1; transform: translateX(0); }",
    "  to   { opacity: 0; transform: translateX(30px); }",
    "}",
  ].join("\n");
  document.head.appendChild(css);

  // ── Toast 系统 ─────────────────────────────────────────────
  function ensureLayer() {
    var layer = document.getElementById("codexToastLayer");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "codexToastLayer";
      document.body.appendChild(layer);
    }
    return layer;
  }
  function toast(msg, type) {
    type = type || "info";
    var icons = { success: "✓", error: "✕", warn: "⚠", info: "ℹ" };
    var el = document.createElement("div");
    el.className = "codex-toast " + type;
    el.innerHTML =
      "<span class=\"ico\">" + icons[type] + "</span>" +
      "<span class=\"msg\"></span>" +
      "<span class=\"close\">✕</span>";
    el.querySelector(".msg").textContent = String(msg || "");
    var dismiss = function () {
      if (!el.parentNode || el.classList.contains("leaving")) return;
      el.classList.add("leaving");
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
    };
    el.querySelector(".close").addEventListener("click", dismiss);
    ensureLayer().appendChild(el);
    // 3.6 秒自动关闭(error 延长到 6 秒,方便看清)
    setTimeout(dismiss, type === "error" ? 6000 : 3600);
    return el;
  }
  window._codexToast = toast;

  // 拦截 window.alert:仅在浏览器场景生效,不影响 confirm(它是阻塞的)
  var _origAlert = window.alert;
  window.alert = function (msg) {
    try {
      var s = String(msg == null ? "" : msg);
      var type = "info";
      if (/失败|错误|异常|❌|✗/.test(s)) type = "error";
      else if (/成功|✓|已保存|已创建/.test(s)) type = "success";
      else if (/警告|注意|⚠/.test(s)) type = "warn";
      toast(s, type);
    } catch (e) {
      // 兜底走原 alert,不打断业务
      _origAlert.call(window, msg);
    }
  };

  console.log("[codex-ux:micro] 微交互补丁已加载(modal 淡入 + card hover + tab fade + toast)");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:btn-loading & input-focus — #5 按钮 loading 态 + #7 input 高亮
 *
 * 防御设计:
 * - 只增强 .btn 类,不影响自定义按钮
 * - chain 现有 fetch(不覆盖之前的 fallback 逻辑)
 * - 保存完整 innerHTML 精确恢复
 * - 60s 硬兜底防止永久 disabled
 * - 跳过内部已含 spinner 的按钮(vendor 自管的)
 * - try/catch + finally 异常时必恢复
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  // ── #7 CSS:input focus 高亮 ─────────────────────────────
  var css = document.createElement("style");
  css.textContent = [
    "input:focus, textarea:focus, select:focus, .input:focus, .select:focus {",
    "  outline: 0;",
    "  border-color: #3B82F6 !important;",
    "  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);",
    "  transition: border-color .15s ease, box-shadow .15s ease;",
    "}",
    /* #5 按钮 spinner + disabled 视觉 */
    ".codex-btn-spinner {",
    "  display: inline-block; width: 12px; height: 12px;",
    "  border: 2px solid currentColor; border-top-color: transparent;",
    "  border-radius: 50%; animation: codexBtnSpin .6s linear infinite;",
    "  vertical-align: -2px; margin-right: 6px; opacity: .8;",
    "}",
    "@keyframes codexBtnSpin { to { transform: rotate(360deg); } }",
    "button[data-codex-loading=\"1\"] { cursor: wait !important; opacity: .75; }",
  ].join("\n");
  document.head.appendChild(css);

  // ── #5 按钮 loading 状态管理 ────────────────────────────
  // 判断按钮是否值得增强:.btn 类 + 无内部 spinner + 有明确 action 文案
  function shouldEnhance(btn) {
    if (!btn || btn.disabled) return false;
    if (!btn.classList || !btn.classList.contains("btn")) return false;
    // vendor 内已有 spinner 的按钮不动
    if (btn.querySelector(".spinner, .codex-btn-spinner")) return false;
    // action 类文案才加(排除关闭/取消/查看等纯导航)
    var txt = (btn.textContent || "").trim();
    if (!txt) return false;
    var actionWord = /保存|确认|同意|执行|新建|AI|分析|推荐|刷新|提交|应用|重新|运行/;
    if (!actionWord.test(txt)) return false;
    return true;
  }

  function setLoading(btn) {
    if (!shouldEnhance(btn)) return false;
    try {
      btn.dataset.codexLoading = "1";
      btn.dataset.codexOrigHtml = btn.innerHTML;   // 完整备份
      btn.dataset.codexOrigDisabled = btn.disabled ? "1" : "";
      btn.disabled = true;
      btn.innerHTML = "<span class=\"codex-btn-spinner\"></span>" +
        (btn.textContent || "").trim();
      // 硬兜底:60 秒后强制恢复,防止按钮永久 disabled
      btn._codexTimer = setTimeout(function () { clearLoading(btn); }, 60000);
      return true;
    } catch (e) {
      console.warn("[codex-ux] setLoading 异常:", e);
      return false;
    }
  }

  function clearLoading(btn) {
    if (!btn || btn.dataset.codexLoading !== "1") return;
    try {
      if (btn._codexTimer) { clearTimeout(btn._codexTimer); btn._codexTimer = null; }
      var origHtml = btn.dataset.codexOrigHtml;
      var origDisabled = btn.dataset.codexOrigDisabled;
      delete btn.dataset.codexLoading;
      delete btn.dataset.codexOrigHtml;
      delete btn.dataset.codexOrigDisabled;
      if (origHtml != null) btn.innerHTML = origHtml;
      btn.disabled = origDisabled === "1";
    } catch (e) {
      console.warn("[codex-ux] clearLoading 异常:", e);
      // 兜底:强制启用
      btn.disabled = false;
    }
  }

  // 追踪最近被点击的按钮(500ms 窗口)
  var _lastBtn = null;
  var _lastClick = 0;
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest && e.target.closest("button.btn");
    if (btn) {
      _lastBtn = btn;
      _lastClick = Date.now();
    }
  }, true);

  // Chain 现有 window.fetch(可能已经被 fallback 拦截过一层)
  var _prevFetch = window.fetch;
  window.fetch = function (input, init) {
    var btn = null;
    if (Date.now() - _lastClick < 500 && _lastBtn && !_lastBtn.dataset.codexLoading) {
      btn = _lastBtn;
      setLoading(btn);
      _lastBtn = null;  // 消费一次,避免重复
    }
    return _prevFetch(input, init).finally(function () {
      if (btn) clearLoading(btn);
    });
  };

  console.log("[codex-ux:btn-loading] 按钮 loading + input focus 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:cancel-remount — 修复 vendor bug
 *
 * BUG 现象:在 tab5 分析进行中时点"放弃本次分析",tab5 空白
 * 根因:vendor onCancelEventClick → bootstrap → loadAll 清空 campPanelRoot,
 *      但没有主动重挂 B 态快照(依赖用户手动切 tab)
 * 修复:拦截 cancel-event 成功,等 vendor 状态 settled,主动 mount 最新完成批次
 *
 * 安全设计:
 * - Chain 现有 fetch,不覆盖前面拦截层
 * - 只处理 /decision/cancel-event 成功响应,其他 100% pass through
 * - 不阻塞原 fetch 返回,hook 逻辑后台异步跑
 * - 6 秒轮询上限,时间到自动放弃
 * - 5 层检查才 mount:非进行中 / 有 latest_id / 当前在 tab5 / tab5 空 / 未已挂载
 * - try/catch 兜底,失败只 console.warn 不影响业务
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  function isTab5Active() {
    var p = document.getElementById("panel-tab5");
    return !!(p && p.classList && p.classList.contains("active"));
  }

  function isTab5Empty() {
    var root = document.getElementById("campPanelRoot");
    if (!root) return false;
    return !root.children.length || !root.innerHTML.trim();
  }

  // vendor 的 _decisionContext 是 module-scoped let,拿不到 → 直接 fetch API
  async function getFreshContext() {
    var asin = (window._erpParams && window._erpParams.parentAsin) ||
               (document.getElementById("asinInput") &&
                document.getElementById("asinInput").value.trim()) || "";
    if (!asin) return null;
    try {
      var url = "/api/v1/agent/ad-direction/decision/context?asin=" +
                encodeURIComponent(asin);
      // 用最底层 _origFetch(避免走前面拦截层的额外开销)
      var r = await fetch(url, { cache: "no-store" });
      if (!r || !r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  async function remountLatestSnapshot() {
    // 轮询等待 vendor bootstrap + loadAll 完成
    for (var i = 0; i < 20; i++) {
      await new Promise(function (r) { setTimeout(r, 300); });

      var ctx;
      try { ctx = await getFreshContext(); } catch (_) { ctx = null; }
      if (!ctx) continue;

      // 5 层安全检查
      if (ctx.in_progress) continue;              // 后端还未清 cancel
      if (!ctx.latest_completed_id) return;       // A 态,不该 mount
      if (!isTab5Active()) return;                // 用户已切走 tab
      if (!isTab5Empty()) return;                 // vendor 已经填了内容
      if (window._campaignPanelAPI) return;       // vendor 已挂载

      if (typeof window._mountCampaignBatch !== "function") return;

      try {
        console.log("[codex-ux:cancel-remount] tab5 放弃后为空,主动 mount 最新快照",
                    ctx.latest_completed_id);
        await window._mountCampaignBatch({
          decision_id: ctx.latest_completed_id,
        });
      } catch (e) {
        console.warn("[codex-ux:cancel-remount] mount 失败:", e);
      }
      return;
    }
    console.log("[codex-ux:cancel-remount] 轮询超时(6s),放弃");
  }

  // Chain 现有 fetch(可能已被 fallback、btn-loading 两层拦截过)
  var _prevFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input :
              (input && input.url) || "";
    var isCancel = url.indexOf("/decision/cancel-event") >= 0;
    var p = _prevFetch(input, init);
    if (isCancel) {
      // 不阻塞原返回,异步等结果
      p.then(function (resp) {
        if (resp && resp.ok) {
          remountLatestSnapshot().catch(function (e) {
            console.warn("[codex-ux:cancel-remount] err:", e);
          });
        }
      }).catch(function () { /* fetch 失败原路径处理,不干预 */ });
    }
    return p;
  };

  console.log("[codex-ux:cancel-remount] 修复补丁已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:tab5-density — Tab5 广告活动分析布局优化
 *
 * A) 顶部 sticky-controls 压缩(默认应用)
 *    · summary-grid 数字 22→14px + gap/padding 减半
 *    · synthesis / budget 卡片压缩
 *    · filters 压缩
 *
 * B) 密度切换(紧凑/舒适/详细,浮动按钮 + localStorage 记忆)
 *    · 紧凑:卡片 padding + gap 压紧,detail 隐藏,一屏 5-8 张
 *    · 舒适:vendor 原样
 *    · 详细:卡片 detail 默认展开
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // ── 注入所有 CSS ────────────────────────────────────────────
  var css = document.createElement("style");
  css.id = "codex-ux-tab5-density-style";
  css.textContent = [
    /* ══ A) 顶部压缩(默认无条件) ═══════════════════════════ */
    /* summary-grid: 大数字缩小 */
    ".camp-summary-grid { gap: 8px !important; padding: 4px 12px !important; }",
    ".camp-summary-grid .num { font-size: 15px !important; line-height: 1.1; }",
    ".camp-summary-grid .lab { font-size: 10px !important; margin-top: 1px !important; }",

    /* synthesis 汇总: padding 收紧 */
    ".camp-synthesis-group { padding: 6px 10px !important; margin-bottom: 6px !important; }",
    ".camp-synthesis-group p { line-height: 1.4 !important; margin-bottom: 2px !important; font-size: 11.5px !important; }",
    ".camp-synthesis-special { padding: 6px 10px !important; margin-top: 4px !important; }",

    /* budget 汇总卡片压缩(顶部第一张 camp-card) */
    ".camp-main-area > .camp-card:first-child { padding: 8px 12px !important; margin-bottom: 6px !important; }",

    /* filters 栏压缩 */
    "#camp-filters { padding: 4px 8px !important; margin-bottom: 4px !important; gap: 6px !important; }",
    "#camp-filters .filter-group { gap: 3px !important; font-size: 11px !important; }",
    "#camp-filters input { font-size: 11px !important; padding: 3px 6px !important; }",

    /* sticky-controls 组合气泡区 */
    ".camp-sticky-controls { padding-top: 4px !important; padding-bottom: 4px !important; }",

    /* ══ B) 密度切换按钮 ═══════════════════════════════════ */
    "#codexDensityToggle {",
    "  position: fixed; top: 88px; right: 24px; z-index: 100;",
    "  display: none;",  /* 只在 tab5 激活时 show */
    "  background: #fff; border: 1px solid #E2E8F0;",
    "  border-radius: 20px; box-shadow: 0 2px 8px rgba(15,23,42,.08);",
    "  padding: 3px; font-size: 12px;",
    "  transition: box-shadow .15s;",
    "}",
    "#codexDensityToggle:hover { box-shadow: 0 4px 12px rgba(15,23,42,.12); }",
    "#codexDensityToggle button {",
    "  border: 0; background: transparent; color: #475569;",
    "  padding: 4px 10px; border-radius: 16px;",
    "  cursor: pointer; font-size: 12px; font-weight: 500;",
    "  transition: all .15s ease;",
    "}",
    "#codexDensityToggle button:hover { background: #F1F5F9; }",
    "#codexDensityToggle button.active {",
    "  background: #1D4ED8; color: #fff;",
    "}",

    /* ══ B-1) 紧凑模式 ═══════════════════════════════════════ */
    /* 卡片本身 padding/gap 减小 */
    "body.codex-density-compact .camp-adjustment-card {",
    "  padding: 6px 10px !important; margin-bottom: 4px !important;",
    "  gap: 8px !important;",
    "}",
    "body.codex-density-compact .camp-adjustment-card .head {",
    "  gap: 6px !important; font-size: 12px !important;",
    "}",
    /* 卡片间隙缩小 */
    "body.codex-density-compact .camp-adjustment-card + .camp-adjustment-card {",
    "  margin-top: 3px !important;",
    "}",
    /* 注:详情区展开/收起完全交给 vendor 的 .hidden + inline style 控制,
       密度模式只负责 JS 层的"批量默认状态"(见 syncCardDetailForMode);
       用户手动点过展开/收起的卡片会打上 data-codex-detail-user 标记,
       后续切换密度不再覆盖它。 */

    /* ══ 提示浮层(第一次进入时) ═══════════════════════════ */
    "#codexDensityHint {",
    "  position: fixed; top: 130px; right: 24px; z-index: 99;",
    "  background: #1E293B; color: #fff;",
    "  padding: 10px 14px; border-radius: 8px;",
    "  font-size: 12px; max-width: 240px;",
    "  box-shadow: 0 8px 24px rgba(0,0,0,.2);",
    "  animation: codexHintIn .3s ease-out;",
    "  pointer-events: none;",
    "}",
    "#codexDensityHint::before {",
    "  content: \"\"; position: absolute; top: -6px; right: 40px;",
    "  border: 6px solid transparent; border-bottom-color: #1E293B;",
    "}",
    "@keyframes codexHintIn {",
    "  from { opacity: 0; transform: translateY(-8px); }",
    "  to { opacity: 1; transform: translateY(0); }",
    "}",
  ].join("\n");
  var old = document.getElementById("codex-ux-tab5-density-style");
  if (old) old.remove();
  document.head.appendChild(css);

  // ── 密度按钮 UI + 事件 ────────────────────────────────────
  var STORAGE_KEY = "codex_tab5_density";
  var HINT_KEY = "codex_tab5_density_hint_seen";
  var MODES = ["compact", "comfy", "detailed"];
  var LABELS = { compact: "紧凑", comfy: "舒适", detailed: "详细" };
  var current = null;

  function applyMode(mode) {
    if (MODES.indexOf(mode) < 0) mode = "comfy";
    var changed = current !== mode;
    current = mode;
    // 清除所有密度 class,加当前
    document.body.classList.remove(
      "codex-density-compact", "codex-density-comfy", "codex-density-detailed"
    );
    document.body.classList.add("codex-density-" + mode);
    // 更新按钮激活状态
    var btns = document.querySelectorAll("#codexDensityToggle button");
    btns.forEach(function (b) {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
    try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) {}
    // 用户主动切换密度 → 清除所有卡的 user-toggled 标记,让新模式完全生效
    if (changed) {
      document.querySelectorAll(".camp-adjustment-card[data-codex-detail-user]")
        .forEach(function (c) { delete c.dataset.codexDetailUser; });
    }
    syncAllCardDetailsForMode();
  }

  // ── 详情区状态同步(联动密度模式) ──────────────────────────
  // 紧凑 → 所有未被用户手动打开的卡:收起详情
  // 详细 → 所有未被用户手动关闭的卡:展开详情
  // 舒适 → 保持 vendor 默认(不动)
  function syncCardDetailForMode(card, mode) {
    if (!card || card.dataset.codexDetailUser === "1") return;
    var detail = card.querySelector(".detail");
    var btn = card.querySelector('[data-action="camp-toggle-detail"]');
    if (!detail) return;
    if (mode === "compact") {
      if (!detail.classList.contains("hidden")) {
        detail.classList.add("hidden");
        detail.style.display = "none";
        if (btn) btn.textContent = "展开详情";
      }
    } else if (mode === "detailed") {
      if (detail.classList.contains("hidden")) {
        detail.classList.remove("hidden");
        detail.style.display = "block";
        if (btn) btn.textContent = "收起详情";
      }
    }
    // comfy: 不动
  }
  function syncAllCardDetailsForMode() {
    if (!current) return;
    document.querySelectorAll(".camp-adjustment-card")
      .forEach(function (c) { syncCardDetailForMode(c, current); });
  }

  // 用户点"展开详情/收起详情"按钮 → 打标记,后续密度切换不再覆盖这张卡
  document.addEventListener("click", function (e) {
    var t = e.target;
    var btn = t && t.closest && t.closest('[data-action="camp-toggle-detail"]');
    if (!btn) return;
    var card = btn.closest(".camp-adjustment-card");
    if (card) card.dataset.codexDetailUser = "1";
  }, true);

  // vendor 重挂载卡片列表 → 对新卡片按当前模式同步
  // 同步执行(不用 rAF/setTimeout),浏览器绘制前完成,避免"详情先收起再展开"的抖动
  function attachCardDetailObserver() {
    var root = document.getElementById("campPanelRoot");
    if (!root || root._codexDetailObserver) return;
    var obs = new MutationObserver(function (muts) {
      var hit = false;
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].type === "childList" && muts[i].addedNodes.length) { hit = true; break; }
      }
      if (hit) syncAllCardDetailsForMode();
    });
    obs.observe(root, { childList: true, subtree: true });
    root._codexDetailObserver = obs;
  }
  var _detailObserverRetryLeft = 30;
  (function retryAttach() {
    attachCardDetailObserver();
    if (!document.getElementById("campPanelRoot") && _detailObserverRetryLeft-- > 0) {
      setTimeout(retryAttach, 1000);
    }
  })();

  function ensureToggle() {
    if (document.getElementById("codexDensityToggle")) return;
    var box = document.createElement("div");
    box.id = "codexDensityToggle";
    box.title = "调整卡片密度";
    MODES.forEach(function (m) {
      var b = document.createElement("button");
      b.textContent = LABELS[m];
      b.dataset.mode = m;
      b.addEventListener("click", function () {
        applyMode(m);
        hideHint();
      });
      box.appendChild(b);
    });
    document.body.appendChild(box);
  }

  function isTab5Active() {
    var p = document.getElementById("panel-tab5");
    return !!(p && p.classList && p.classList.contains("active"));
  }

  function updateToggleVisibility() {
    var box = document.getElementById("codexDensityToggle");
    if (!box) return;
    box.style.display = isTab5Active() ? "inline-flex" : "none";
  }

  // 首次提示
  function showHint() {
    try { if (localStorage.getItem(HINT_KEY)) return; } catch (_) {}
    var box = document.getElementById("codexDensityToggle");
    if (!box || box.style.display === "none") return;
    var h = document.getElementById("codexDensityHint");
    if (h) return;
    h = document.createElement("div");
    h.id = "codexDensityHint";
    h.textContent = "💡 可以在这切换卡片密度,一屏看更多";
    document.body.appendChild(h);
    setTimeout(hideHint, 6000);
  }
  function hideHint() {
    var h = document.getElementById("codexDensityHint");
    if (h) { h.style.transition = "opacity .3s"; h.style.opacity = "0";
             setTimeout(function () { if (h.parentNode) h.parentNode.removeChild(h); }, 320); }
    try { localStorage.setItem(HINT_KEY, "1"); } catch (_) {}
  }

  // ── 初始化 ────────────────────────────────────────────────
  function init() {
    ensureToggle();
    // 读取保存的模式,默认舒适(不激进变风格)
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (_) {}
    applyMode(saved || "comfy");
    updateToggleVisibility();
    if (isTab5Active()) setTimeout(showHint, 2000);
  }

  // 监听 tab 切换(vendor 用 .tab-panel.active 表示当前 tab)
  var tabObserver = new MutationObserver(function () {
    updateToggleVisibility();
    if (isTab5Active()) setTimeout(showHint, 1500);
  });
  function startObserver() {
    var panels = document.querySelectorAll(".tab-panel");
    panels.forEach(function (p) {
      tabObserver.observe(p, { attributes: true, attributeFilter: ["class"] });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); startObserver(); });
  } else {
    init(); startObserver();
  }

  console.log("[codex-ux:tab5-density] tab5 密度补丁已加载(默认压缩顶部 + 密度切换按钮)");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:card-compact-v2 & batch-select — 卡片布局深度优化 + 批次下拉动效
 *
 * 1) 紧凑模式的卡片:head + meta + values 横向密排,reason line-clamp 2 行
 * 2) 舒适模式的 values 也改成 auto-fit(即使不切紧凑也节省空间)
 * 3) 历史批次下拉:hover / focus 动效,change 后批次栏 flash
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.textContent = [
    /* ═══ 卡片深度优化 ═══════════════════════════════ */
    /* values grid: 舒适+紧凑都改成 auto-fit,让 bid/预算横向排 */
    ".camp-adjustment-card .values {",
    "  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)) !important;",
    "  gap: 2px 16px !important;",
    "}",

    /* ═══ 紧凑模式:head/meta 一体压扁 + reason line-clamp ═══ */
    "body.codex-density-compact .camp-adjustment-card {",
    "  padding: 6px 10px !important;",
    "  margin-bottom: 3px !important;",
    "  gap: 8px !important;",
    "  line-height: 1.4 !important;",
    "}",
    "body.codex-density-compact .camp-adjustment-card .head {",
    "  gap: 6px !important;",
    "  font-size: 12.5px !important;",
    "}",
    "body.codex-density-compact .camp-adjustment-card .head strong {",
    "  font-size: 13px !important;",
    "}",
    /* meta 与 head 之间近似贴合 */
    "body.codex-density-compact .camp-adjustment-card .meta {",
    "  margin-top: 2px !important;",
    "  font-size: 11px !important;",
    "  line-height: 1.35 !important;",
    "  color: #64748B !important;",
    "  white-space: nowrap;",
    "  overflow: hidden;",
    "  text-overflow: ellipsis;",
    "}",
    /* values 更紧凑 */
    "body.codex-density-compact .camp-adjustment-card .values {",
    "  margin-top: 3px !important;",
    "  font-size: 12px !important;",
    "  gap: 1px 14px !important;",
    "}",
    /* reason 只显示 2 行(整卡点击展开时会去掉 clamp) */
    "body.codex-density-compact .camp-adjustment-card .reason {",
    "  margin-top: 3px !important;",
    "  font-size: 11.5px !important;",
    "  line-height: 1.4 !important;",
    "  display: -webkit-box;",
    "  -webkit-line-clamp: 2;",
    "  -webkit-box-orient: vertical;",
    "  overflow: hidden;",
    "  cursor: pointer;",
    "  transition: max-height .3s ease;",
    "}",
    "body.codex-density-compact .camp-adjustment-card.codex-expanded .reason {",
    "  -webkit-line-clamp: unset;",
    "  display: block;",
    "}",
    /* review-badge 位置微调,不覆盖内容 */
    "body.codex-density-compact .camp-adjustment-card .review-badge {",
    "  top: 4px !important;",
    "  right: 8px !important;",
    "  font-size: 9px !important;",
    "  padding: 1px 6px !important;",
    "}",

    /* ═══ 历史批次下拉 动效 ══════════════════════════════ */
    "#batchesSelect, select#batchesSelect {",
    "  transition: border-color .18s ease, box-shadow .18s ease, background .15s;",
    "  cursor: pointer;",
    "}",
    "#batchesSelect:hover {",
    "  border-color: #3B82F6 !important;",
    "  background: #F8FAFC;",
    "}",
    "#batchesSelect:focus {",
    "  outline: 0;",
    "  border-color: #3B82F6 !important;",
    "  box-shadow: 0 0 0 3px rgba(59,130,246,0.15) !important;",
    "}",

    /* 切换后批次栏 flash */
    "@keyframes codexBatchFlash {",
    "  0% { background-color: rgba(59,130,246,.12); }",
    "  100% { background-color: transparent; }",
    "}",
    ".codex-batch-flash {",
    "  animation: codexBatchFlash .7s ease-out;",
    "  border-radius: 6px;",
    "}",
  ].join("\n");
  document.head.appendChild(css);

  // ── (已弃用) 之前的点卡片展开 reason 逻辑被下方的 click-select 替代 ──

  // ── 历史批次切换时:批次栏 flash 动画 ────────────────────
  function flashBatchBar() {
    // vendor 里批次栏是 batchesBar / 或包含 batchesSelect 的父容器
    var sel = document.getElementById("batchesSelect");
    if (!sel) return;
    var bar = sel.closest(".batch-bar, #batchesBar, [class*=\"batch\"]") || sel.parentElement;
    if (!bar) return;
    bar.classList.remove("codex-batch-flash");
    // 强制重排触发动画
    void bar.offsetWidth;
    bar.classList.add("codex-batch-flash");
    setTimeout(function () { bar.classList.remove("codex-batch-flash"); }, 800);
  }

  // 监听 select change(vendor 会 rebind,用委托避免脱钩)
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "batchesSelect") {
      setTimeout(flashBatchBar, 100);
    }
  }, false);

  console.log("[codex-ux:card-compact-v2] 卡片深度紧凑 + 批次下拉动效 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:card-select-click & sidebar-compact — 综合优化
 *
 * 1) 卡片 values 间距缩小
 * 2) 组合预算汇总胶囊(camp-portfolio-pill)压缩到一行
 * 3) 点击卡片任意位置 → 触发 checkbox(勾选/取消),复选框正常响应
 * 4) 左栏 3 张卡(基础信息/战略/策略)缩窄
 * 5) 运营可见的"快照" → "历史数据"(MutationObserver 替换 textNode)
 * 6) 基础信息表格化(边框+行分隔)
 * 7) 左栏标题放大加粗 + 内容字号增大
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.textContent = [
    /* 1) 卡片 bid/预算 横向间距缩小 */
    ".camp-adjustment-card .values {",
    "  gap: 2px 10px !important;",
    "}",

    /* 2) 组合预算汇总胶囊 单行紧凑 */
    ".camp-portfolio-pill {",
    "  display: inline-flex !important;",
    "  align-items: baseline !important;",
    "  flex-wrap: wrap !important;",
    "  gap: 6px !important;",
    "  padding: 4px 10px !important;",
    "  line-height: 1.35 !important;",
    "}",
    ".camp-portfolio-pill .pp-name {",
    "  font-size: 12px !important;",
    "  margin: 0 !important;",
    "}",
    ".camp-portfolio-pill .pp-budget {",
    "  font-size: 12px !important;",
    "  margin-top: 0 !important;",
    "  opacity: 1 !important;",
    "  font-weight: 500;",
    "}",
    ".camp-portfolio-pill .pp-amount {",
    "  font-size: 10.5px !important;",
    "  margin-top: 0 !important;",
    "  opacity: 0.7 !important;",
    "}",
    ".camp-portfolio-pill .pp-acts {",
    "  margin-top: 0 !important;",
    "  gap: 3px !important;",
    "}",

    /* 3+7) 卡片(左栏)标题放大加粗,内容字号增大 */
    "#cardBaseInfo .card-title,",
    "#cardStrategy .card-title,",
    "#cardTactics .card-title {",
    "  font-size: 16px !important;",
    "  font-weight: 700 !important;",
    "  color: #0F172A !important;",
    "  margin-bottom: 10px !important;",
    "}",
    "#baseInfoContent {",
    "  font-size: 13px !important;",
    "}",
    /* 6) 基础信息表格样式:每行清晰边框 */
    "#baseInfoContent > div {",
    "  padding: 8px 4px !important;",
    "  border-bottom: 1px solid #E2E8F0 !important;",
    "  font-size: 13px !important;",
    "}",
    "#baseInfoContent > div:last-child {",
    "  border-bottom: 0 !important;",
    "}",
    "#baseInfoContent > div > span:first-child {",
    "  font-weight: 600 !important;",
    "  color: #475569 !important;",
    "  min-width: 90px;",
    "}",
    "#baseInfoContent > div > span:last-child {",
    "  color: #0F172A !important;",
    "  font-weight: 500;",
    "}",

    /* 4) 左栏缩窄 — 找到左栏容器 */
    /* vendor 是 grid 两栏 layout,左栏(#sidebar 或 side 类) */
    /* 用属性选择器兜底覆盖任何布局 */
    ".sidebar, #sidebar, [class*=\"side-panel\"] {",
    "  max-width: 260px !important;",
    "  min-width: 260px !important;",
    "}",
    /* 一种可能的布局:顶层 flex,左边有 card 系列 */
    /* 若 body 用 grid-template-columns,把第一列变窄 */

    /* 让战略层和策略层字号也稍大 */
    "#strategyContent, #tacticsContent {",
    "  font-size: 12.5px !important;",
    "}",
  ].join("\n");
  document.head.appendChild(css);

  // ── 3) 点击卡片 → 触发 checkbox ─────────────────────
  document.addEventListener("click", function (e) {
    var target = e.target;
    // 排除:checkbox 本身(交给原生处理)、按钮、链接、review-badge
    if (target.matches("input[type=\"checkbox\"], button, a")) return;
    if (target.closest && target.closest("button, a, .review-badge")) return;

    var card = target.closest && target.closest(".camp-adjustment-card");
    if (!card) return;
    // 找卡片内的 checkbox
    var cb = card.querySelector("input[type=\"checkbox\"]");
    if (!cb || cb.disabled) return;
    // 模拟点击 checkbox(触发原生 change 事件,vendor 会正确响应)
    cb.click();
    // 阻止事件继续冒泡引发副作用
    e.preventDefault();
    e.stopPropagation();
  }, false);

  // ── 5) 运营可见的 "快照" → "历史数据" 替换 ─────────
  var TEXT_MAP = {
    "快照": "历史数据",
    "只读快照": "只读历史",
    "读取失败": "读取失败",  // 保留,已经通俗
  };
  function replaceInNode(node) {
    if (!node) return;
    if (node.nodeType === 3) {
      // 文本节点
      var t = node.nodeValue;
      if (t && t.indexOf("快照") >= 0) {
        node.nodeValue = t.replace(/只读快照/g, "只读历史").replace(/快照/g, "历史数据");
      }
    } else if (node.nodeType === 1) {
      // 元素:递归所有 childNodes
      // 跳过 script/style
      var tag = node.tagName;
      if (tag === "SCRIPT" || tag === "STYLE") return;
      for (var i = 0; i < node.childNodes.length; i++) {
        replaceInNode(node.childNodes[i]);
      }
    }
  }
  // 初始扫一遍
  replaceInNode(document.body);
  // 监听 DOM 变化,新增的元素也替换
  var textObserver = new MutationObserver(function (muts) {
    muts.forEach(function (m) {
      // 新增节点
      m.addedNodes.forEach(function (n) { replaceInNode(n); });
      // characterData 变化(textContent 直接改)
      if (m.type === "characterData") replaceInNode(m.target);
    });
  });
  textObserver.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });

  console.log("[codex-ux:card-select-click] 综合优化补丁已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:sidebar-collapse & card-select-bg & approve-confirm-modal
 *
 * 1) 左栏加宽到 312px + 可折叠(有折叠按钮,状态存 localStorage)
 * 2) 卡片选中(checkbox checked)后浅蓝底色 + 蓝色左边框加粗反馈
 * 3) "同意所选"→"同意执行所选",click 时弹 modal 展示关键词调整明细,
 *    运营在 modal 点"确认执行"才二次触发原按钮走 vendor 原生 approve
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.textContent = [
    /* 1) 左栏宽度 → 360px + 折叠支持 */
    ".sidebar, #sidebar, [class*=\"side-panel\"] {",
    "  max-width: 360px !important;",
    "  min-width: 360px !important;",
    "  transition: max-width .28s ease, min-width .28s ease, opacity .2s ease;",
    "  position: relative;",
    "}",
    "body.codex-sidebar-collapsed .sidebar,",
    "body.codex-sidebar-collapsed #sidebar,",
    "body.codex-sidebar-collapsed [class*=\"side-panel\"] {",
    "  max-width: 0 !important;",
    "  min-width: 0 !important;",
    "  overflow: hidden !important;",
    "  opacity: 0 !important;",
    "  padding: 0 !important;",
    "  margin: 0 !important;",
    "}",
    /* 折叠按钮:浮动在左栏右边缘 */
    "#codexSidebarToggle {",
    "  position: fixed; top: 120px; z-index: 90;",
    "  width: 22px; height: 42px;",
    "  background: #fff; border: 1px solid #E2E8F0; border-left: 0;",
    "  border-radius: 0 8px 8px 0;",
    "  box-shadow: 2px 2px 8px rgba(15,23,42,.06);",
    "  cursor: pointer; display: flex; align-items: center; justify-content: center;",
    "  font-size: 12px; color: #64748B;",
    "  transition: all .2s;",
    "  user-select: none;",
    "}",
    "#codexSidebarToggle:hover { background: #F1F5F9; color: #1D4ED8; }",

    /* 2) 卡片选中反馈:浅蓝底色 + inset 阴影模拟粗左条
       (不改 border-width,内容区宽度零变化 → 点击零抖动、meta chip 不会重排) */
    ".camp-adjustment-card.codex-selected {",
    "  background: #EFF6FF !important;",
    "  box-shadow: inset 3px 0 0 rgba(59,130,246,.55),",
    "              0 0 0 1px rgba(59,130,246,.2),",
    "              0 2px 8px rgba(59,130,246,.08);",
    "}",

    /* 3) 确认执行 modal */
    ".codex-approve-overlay {",
    "  position: fixed; inset: 0; z-index: 9000;",
    "  background: rgba(15,23,42,.5);",
    "  display: flex; align-items: center; justify-content: center;",
    "  animation: codexOverlayFadeIn .2s ease-out;",
    "}",
    ".codex-approve-modal {",
    "  background: #fff; border-radius: 10px;",
    "  width: min(720px, 92vw); max-height: 82vh;",
    "  display: flex; flex-direction: column;",
    "  box-shadow: 0 20px 60px rgba(0,0,0,.28);",
    "  animation: codexFadeInScale .24s cubic-bezier(.16,1,.3,1);",
    "}",
    ".codex-approve-header {",
    "  padding: 16px 20px; border-bottom: 1px solid #E2E8F0;",
    "  display: flex; align-items: center; justify-content: space-between;",
    "  font-size: 15px; font-weight: 700; color: #0F172A;",
    "}",
    ".codex-approve-header .cnt {",
    "  font-size: 12px; font-weight: 500; color: #DC2626;",
    "  background: #FEF2F2; padding: 3px 10px; border-radius: 12px;",
    "  margin-left: 10px;",
    "}",
    ".codex-approve-close {",
    "  border: 0; background: transparent; cursor: pointer;",
    "  font-size: 16px; color: #94A3B8; padding: 4px 6px;",
    "}",
    ".codex-approve-close:hover { color: #475569; }",
    ".codex-approve-body {",
    "  padding: 12px 20px; overflow-y: auto; flex: 1;",
    "}",
    ".codex-approve-body table {",
    "  width: 100%; border-collapse: collapse; font-size: 12.5px;",
    "}",
    ".codex-approve-body th {",
    "  text-align: left; padding: 8px 8px; background: #F8FAFC;",
    "  border-bottom: 2px solid #E2E8F0;",
    "  font-weight: 600; color: #475569; font-size: 11.5px;",
    "  position: sticky; top: 0;",
    "}",
    ".codex-approve-body td {",
    "  padding: 8px 8px; border-bottom: 1px solid #F1F5F9;",
    "  vertical-align: top;",
    "}",
    ".codex-approve-body tr:hover td { background: #F8FAFC; }",
    ".codex-approve-body .kw { font-weight: 600; color: #0F172A; }",
    ".codex-approve-body .old { color: #64748B; }",
    ".codex-approve-body .new { color: #DC2626; font-weight: 600; }",
    ".codex-approve-body .arrow { color: #94A3B8; margin: 0 4px; }",
    ".codex-approve-body .name {",
    "  color: #475569; font-size: 11px; max-width: 260px;",
    "  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;",
    "}",
    ".codex-approve-body .empty {",
    "  padding: 40px; text-align: center; color: #94A3B8;",
    "}",
    ".codex-approve-body .warn {",
    "  padding: 10px 12px; background: #FEF3C7; color: #92400E;",
    "  border-radius: 6px; font-size: 12px; margin-bottom: 10px;",
    "  border-left: 3px solid #F59E0B;",
    "}",
    ".codex-approve-footer {",
    "  padding: 14px 20px; border-top: 1px solid #E2E8F0;",
    "  display: flex; justify-content: flex-end; gap: 10px;",
    "  background: #F8FAFC; border-radius: 0 0 10px 10px;",
    "}",
    ".codex-approve-footer button {",
    "  padding: 8px 20px; border-radius: 6px;",
    "  font-size: 13px; font-weight: 600; cursor: pointer;",
    "  border: 1px solid transparent; transition: all .15s;",
    "}",
    ".codex-approve-footer .cancel { background: #fff; color: #475569; border-color: #CBD5E1; }",
    ".codex-approve-footer .cancel:hover { background: #F1F5F9; }",
    ".codex-approve-footer .confirm { background: #DC2626; color: #fff; }",
    ".codex-approve-footer .confirm:hover { background: #B91C1C; }",
    ".codex-approve-footer .confirm[disabled] {",
    "  background: #CBD5E1; cursor: not-allowed;",
    "}",
    /* 分组标题(关键词调整 / 广告位调整 通用) */
    ".codex-approve-subhead {",
    "  margin: 4px 0 8px; padding: 6px 10px;",
    "  font-size: 13px; font-weight: 600; color: #1E293B;",
    "  background: #EFF6FF; border-left: 3px solid #3B82F6;",
    "  border-radius: 4px;",
    "}",
    /* 第二组(广告位)与上一张表拉开距离,视觉分组清晰 */
    ".codex-approve-body table + .codex-approve-subhead {",
    "  margin-top: 24px; position: relative;",
    "}",
    ".codex-approve-body table + .codex-approve-subhead::before {",
    "  content: \"\"; position: absolute; left: 0; right: 0; top: -13px;",
    "  border-top: 1px dashed #E2E8F0;",
    "}",
    ".codex-approve-subhead .cnt {",
    "  margin-left: 8px; font-size: 11px; color: #64748B; font-weight: 500;",
    "}",
    ".codex-plc-table { margin-top: 0 !important; }",
    ".codex-plc-table td { vertical-align: top !important; padding: 8px 10px !important; }",
    ".codex-plc-line {",
    "  padding: 6px 8px; margin: 4px 0;",
    "  background: #F8FAFC; border: 1px solid #E2E8F0;",
    "  border-radius: 6px; border-left: 3px solid #CBD5E1;",
    "}",
    ".codex-plc-line.changed { border-left-color: #10B981; background: #F0FDF4; }",
    ".codex-plc-line.decl { border-left-color: #F59E0B; background: #FFFBEB; }",
    ".codex-plc-line .codex-plc-head {",
    "  display: inline-flex; align-items: center; gap: 6px;",
    "  font-size: 12px;",
    "}",
    ".codex-plc-line .pos {",
    "  font-weight: 600; color: #0F172A;",
    "  min-width: 40px; display: inline-block;",
    "}",
    ".codex-plc-line .pct { color: #334155; }",
    ".codex-plc-line .old { color: #64748B; }",
    ".codex-plc-line .new { color: #0F172A; font-weight: 600; }",
    ".codex-plc-line.changed .new { color: #059669; }",
    ".codex-plc-line .arrow { color: #94A3B8; margin: 0 4px; }",
    ".codex-plc-line .tag {",
    "  font-size: 10px; padding: 1px 6px; border-radius: 8px;",
    "  background: #E2E8F0; color: #475569;",
    "}",
    ".codex-plc-line.changed .tag { background: #D1FAE5; color: #065F46; }",
    ".codex-plc-line .tag.decl { background: #FEF3C7; color: #92400E; }",
    ".codex-plc-reason {",
    "  margin-top: 4px; padding-left: 2px;",
    "  font-size: 11.5px; color: #475569; line-height: 1.5;",
    "}",
  ].join("\n");
  document.head.appendChild(css);

  // ══════════════════════════════════════════════════
  // 1) 左栏折叠
  // ══════════════════════════════════════════════════
  var COLLAPSE_KEY = "codex_sidebar_collapsed";
  function updateToggleIcon() {
    var t = document.getElementById("codexSidebarToggle");
    if (!t) return;
    t.textContent = document.body.classList.contains("codex-sidebar-collapsed") ? "▶" : "◀";
    t.title = document.body.classList.contains("codex-sidebar-collapsed") ? "展开左栏" : "隐藏左栏";
    // 折叠时按钮贴左边缘
    t.style.left = document.body.classList.contains("codex-sidebar-collapsed") ? "0px" : "360px";
  }
  function ensureToggle() {
    if (document.getElementById("codexSidebarToggle")) return;
    var b = document.createElement("div");
    b.id = "codexSidebarToggle";
    b.addEventListener("click", function () {
      document.body.classList.toggle("codex-sidebar-collapsed");
      try {
        localStorage.setItem(COLLAPSE_KEY,
          document.body.classList.contains("codex-sidebar-collapsed") ? "1" : "0");
      } catch (_) {}
      updateToggleIcon();
    });
    document.body.appendChild(b);
    // 恢复上次状态
    try {
      if (localStorage.getItem(COLLAPSE_KEY) === "1") {
        document.body.classList.add("codex-sidebar-collapsed");
      }
    } catch (_) {}
    updateToggleIcon();
  }

  // ══════════════════════════════════════════════════
  // 2) 卡片选中后浅蓝底色
  // ══════════════════════════════════════════════════
  // 精确 toggle:只在状态实际变化时才动 class,避免触发无谓的 style 重算
  function syncCardSelectionState(cb) {
    var card = cb.closest && cb.closest(".camp-adjustment-card");
    if (!card) return;
    var should = !!cb.checked;
    var has = card.classList.contains("codex-selected");
    if (should === has) return; // 无变化直接返回
    if (should) card.classList.add("codex-selected");
    else card.classList.remove("codex-selected");
  }
  // change 事件:用户勾选/vendor 程序化改都会触发
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t && t.type === "checkbox" && t.closest && t.closest(".camp-adjustment-card")) {
      syncCardSelectionState(t);
    }
  }, false);
  // DOM 变化时(vendor 重挂载卡片列表)才同步选中态背景 — 同步执行,绘制前完成
  function syncAllCardSelection() {
    document.querySelectorAll(".camp-adjustment-card input[type=\"checkbox\"]").forEach(syncCardSelectionState);
  }
  function attachCardObserver() {
    var root = document.getElementById("campPanelRoot");
    if (!root || root._codexCardObserver) return;
    var obs = new MutationObserver(function (muts) {
      var hit = false;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if ((m.type === "childList" && m.addedNodes.length) ||
            (m.type === "attributes" && m.attributeName === "checked")) {
          hit = true; break;
        }
      }
      if (hit) syncAllCardSelection();
    });
    obs.observe(root, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ["checked"],
    });
    root._codexCardObserver = obs;
  }
  attachCardObserver();
  // 兜底:vendor 可能延迟挂载 campPanelRoot,30 秒内每秒重试一次绑定
  var _obsRetry = 0;
  var _obsIv = setInterval(function () {
    attachCardObserver();
    if (++_obsRetry > 30) clearInterval(_obsIv);
  }, 1000);

  // ══════════════════════════════════════════════════
  // 3) "同意所选" → "同意执行所选" + 确认 modal
  // ══════════════════════════════════════════════════
  var CONFIRM_FLAG = "codexApproveConfirmed";

  function renameApproveBtn() {
    var b = document.querySelector("button[data-action=\"camp-batch-approve\"]");
    if (b && !b.dataset.codexRenamed) {
      b.dataset.codexRenamed = "1";
      b.textContent = "同意执行所选";
    }
  }
  setInterval(renameApproveBtn, 1200);

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function collectCheckedItems() {
    var out = [];
    document.querySelectorAll(".camp-adjustment-card").forEach(function (card) {
      var cb = card.querySelector("input[type=\"checkbox\"]");
      if (!cb || !cb.checked || cb.disabled) return;
      var nameEl = card.querySelector(".head strong");
      var name = nameEl ? nameEl.textContent.trim() : "";
      var meta = card.querySelector(".meta");
      var metaText = meta ? meta.textContent.replace(/\s+/g, " ").trim() : "";
      var kw = "";
      var m = metaText.match(/关键词[:：]\s*([^|]+)/);
      if (m) kw = m[1].trim();
      // action label(head 里第一个 badge)
      var actionEl = card.querySelector(".head .camp-badge");
      var action = actionEl ? actionEl.textContent.trim() : "";
      // 从 values 里提 bid/预算前后
      var valsText = "";
      var vals = card.querySelector(".values");
      if (vals) valsText = vals.textContent.replace(/\s+/g, " ").trim();
      var bidFrom = "", bidTo = "", budgetFrom = "", budgetTo = "";
      var bm = valsText.match(/[Bb]id[^0-9\-–—]*([\d\.]+)[^0-9\-–—]*[→\-–—>]+[^0-9\-–—]*([\d\.]+)/);
      if (bm) { bidFrom = bm[1]; bidTo = bm[2]; }
      var pm = valsText.match(/预算[^0-9\-–—]*([\d\.]+)[^0-9\-–—]*[→\-–—>]+[^0-9\-–—]*([\d\.]+)/);
      if (pm) { budgetFrom = pm[1]; budgetTo = pm[2]; }
      // 广告位调整:vendor 用扁平 <div> 列表(每条一个 margin-left:12px 的 div,
      // 内层再有一个 <div> 装 evidence 理由),不是 <ul><li>
      var placements = [];
      var detail = card.querySelector(".detail");
      if (detail) {
        var strongs = detail.querySelectorAll("strong");
        for (var si = 0; si < strongs.length; si++) {
          var st = strongs[si];
          var label = st.textContent || "";
          if (label.indexOf("广告位调整") >= 0) {
            var header = st.closest("div");
            var sib = header && header.nextElementSibling;
            while (sib && sib.tagName === "DIV") {
              var styleAttr = (sib.getAttribute("style") || "").replace(/\s+/g, "");
              if (styleAttr.indexOf("margin-left:12px") < 0) break;
              var mainText = "";
              var reasonText = "";
              for (var ci = 0; ci < sib.childNodes.length; ci++) {
                var node = sib.childNodes[ci];
                if (node.nodeType === 3) {
                  mainText += node.textContent;
                } else if (node.nodeType === 1) {
                  if (node.tagName === "DIV") {
                    reasonText = (node.textContent || "").replace(/\s+/g, " ").trim();
                  } else {
                    mainText += node.textContent || "";
                  }
                }
              }
              mainText = mainText.replace(/\s+/g, " ").trim();
              var pm2 = mainText.match(/^([^:：]+)[:：]\s*(\d+(?:\.\d+)?)\s*%\s*[→\-–—>]+\s*(\d+(?:\.\d+)?)\s*%\s*(?:[（(]\s*([^)）]+?)\s*[)）])?/);
              if (pm2) {
                placements.push({
                  pos: pm2[1].trim(), from: pm2[2], to: pm2[3],
                  actionTag: pm2[4] || "", reason: reasonText,
                });
              } else if (mainText) {
                placements.push({ pos: "", from: "", to: "", raw: mainText, reason: reasonText });
              }
              sib = sib.nextElementSibling;
            }
          } else if (label.indexOf("主投广告位") >= 0) {
            var declTxt = (st.parentElement && st.parentElement.textContent || "")
              .replace(/主投广告位[:：]?/, "").trim();
            if (declTxt) placements.push({ pos: declTxt, from: "", to: "", decl: true });
          }
        }
      }
      out.push({
        card: card, name: name, kw: kw, action: action,
        bidFrom: bidFrom, bidTo: bidTo,
        budgetFrom: budgetFrom, budgetTo: budgetTo,
        placements: placements,
      });
    });
    return out;
  }

  function showApproveModal(items, onConfirm, onCancel) {
    var overlay = document.createElement("div");
    overlay.className = "codex-approve-overlay";

    var kwItems = items.filter(function (i) { return i.kw; });
    var warn = "";
    if (kwItems.length < items.length) {
      warn = "<div class=\"warn\">⚠ " + (items.length - kwItems.length) +
        " 条为活动/预算级调整,不在此列表(执行时会一并处理)</div>";
    }

    var rows = kwItems.length ? kwItems.map(function (it) {
      var bidCell = (it.bidFrom && it.bidTo) ?
        "<span class=\"old\">" + it.bidFrom + "</span><span class=\"arrow\">→</span><span class=\"new\">" + it.bidTo + "</span>"
        : "<span class=\"old\">—</span>";
      var budgetCell = (it.budgetFrom && it.budgetTo) ?
        "<span class=\"old\">" + it.budgetFrom + "</span><span class=\"arrow\">→</span><span class=\"new\">" + it.budgetTo + "</span>"
        : "<span class=\"old\">—</span>";
      return "<tr>" +
        "<td class=\"name\" title=\"" + it.name.replace(/\"/g,"&quot;") + "\">" + it.name + "</td>" +
        "<td class=\"kw\">" + it.kw + "</td>" +
        "<td>" + it.action + "</td>" +
        "<td>" + bidCell + "</td>" +
        "<td>" + budgetCell + "</td>" +
        "</tr>";
    }).join("") : "<tr><td colspan=\"5\" class=\"empty\">当前勾选的都不是关键词级调整,直接执行?</td></tr>";

    // 广告位调整 — 独立块,按活动分组;每条位调整一行,理由在下面
    var placementItems = items.filter(function (i) { return i.placements && i.placements.length; });
    var placementSection = "";
    if (placementItems.length) {
      var plcRows = placementItems.map(function (it) {
        var lines = it.placements.map(function (p) {
          if (p.decl) {
            return "<div class=\"codex-plc-line decl\">" +
              "<span class=\"codex-plc-head\">" +
                "<span class=\"pos\">" + escHtml(p.pos) + "</span>" +
                "<span class=\"tag decl\">仅声明主投</span>" +
              "</span>" +
            "</div>";
          }
          if (p.raw) {
            return "<div class=\"codex-plc-line\">" +
              "<span class=\"codex-plc-head\"><span class=\"pos\">" + escHtml(p.raw) + "</span></span>" +
              (p.reason ? "<div class=\"codex-plc-reason\">" + escHtml(p.reason) + "</div>" : "") +
            "</div>";
          }
          var changed = p.from !== p.to;
          return "<div class=\"codex-plc-line" + (changed ? " changed" : "") + "\">" +
            "<span class=\"codex-plc-head\">" +
              "<span class=\"pos\">" + escHtml(p.pos) + "</span>" +
              "<span class=\"pct\">" +
                "<span class=\"old\">" + escHtml(p.from) + "%</span>" +
                "<span class=\"arrow\">→</span>" +
                "<span class=\"new\">" + escHtml(p.to) + "%</span>" +
              "</span>" +
              (p.actionTag ? "<span class=\"tag\">" + escHtml(p.actionTag) + "</span>" : "") +
            "</span>" +
            (p.reason ? "<div class=\"codex-plc-reason\">" + escHtml(p.reason) + "</div>" : "") +
          "</div>";
        }).join("");
        return "<tr>" +
          "<td class=\"name\" title=\"" + escHtml(it.name) + "\">" + escHtml(it.name) + "</td>" +
          "<td>" + lines + "</td>" +
        "</tr>";
      }).join("");
      placementSection =
        "<div class=\"codex-approve-subhead\">广告位调整 <span class=\"cnt\">" + placementItems.length + " 个活动</span></div>" +
        "<table class=\"codex-plc-table\">" +
          "<thead><tr><th style=\"width:200px\">活动</th><th>广告位变化 · 依据</th></tr></thead>" +
          "<tbody>" + plcRows + "</tbody>" +
        "</table>";
    }

    overlay.innerHTML =
      "<div class=\"codex-approve-modal\">" +
        "<div class=\"codex-approve-header\">" +
          "<span>确认执行以下调整<span class=\"cnt\">" +
            kwItems.length + " 条关键词" +
            (placementItems.length ? " · " + placementItems.length + " 项广告位" : "") +
          "</span></span>" +
          "<button class=\"codex-approve-close\" title=\"关闭\">✕</button>" +
        "</div>" +
        "<div class=\"codex-approve-body\">" +
          warn +
          "<div class=\"codex-approve-subhead\">关键词调整 <span class=\"cnt\">" +
            kwItems.length + " 条关键词</span></div>" +
          "<table class=\"codex-kw-table\">" +
            "<thead><tr>" +
              "<th style=\"width:200px\">活动</th>" +
              "<th>关键词</th>" +
              "<th style=\"width:80px\">动作</th>" +
              "<th style=\"width:120px\">Bid</th>" +
              "<th style=\"width:120px\">预算</th>" +
            "</tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
          "</table>" +
          placementSection +
        "</div>" +
        "<div class=\"codex-approve-footer\">" +
          "<button class=\"cancel\">取消</button>" +
          "<button class=\"confirm\">确认执行 → 下发亚马逊</button>" +
        "</div>" +
      "</div>";

    document.body.appendChild(overlay);

    function close() {
      overlay.style.transition = "opacity .2s";
      overlay.style.opacity = "0";
      setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 220);
    }
    overlay.querySelector(".codex-approve-close").addEventListener("click", function () {
      close(); onCancel && onCancel();
    });
    overlay.querySelector(".cancel").addEventListener("click", function () {
      close(); onCancel && onCancel();
    });
    overlay.querySelector(".confirm").addEventListener("click", function () {
      close(); onConfirm && onConfirm();
    });
    // 点遮罩外部关闭
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) { close(); onCancel && onCancel(); }
    });
    // Esc 关闭
    var escHandler = function (e) {
      if (e.key === "Escape") { close(); onCancel && onCancel();
        document.removeEventListener("keydown", escHandler); }
    };
    document.addEventListener("keydown", escHandler);
  }

  // 拦截 approve 按钮 click(capture 阶段,早于 vendor)
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest &&
              e.target.closest("button[data-action=\"camp-batch-approve\"]");
    if (!btn) return;
    // flag 检测:第二次(用户确认后)触发时放行
    if (btn.dataset[CONFIRM_FLAG] === "1") {
      delete btn.dataset[CONFIRM_FLAG];
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();

    var items = collectCheckedItems();
    if (!items.length) {
      if (window._codexToast) window._codexToast("请先勾选至少一条建议", "warn");
      else alert("请先勾选至少一条建议");
      return;
    }
    showApproveModal(items,
      function onConfirm() {
        // 打 flag 后二次触发,让 vendor 原生 approve 逻辑执行
        btn.dataset[CONFIRM_FLAG] = "1";
        btn.click();
      },
      function onCancel() {
        if (window._codexToast) window._codexToast("已取消,未下发", "info");
      }
    );
  }, true);  // capture

  // ── 初始化 ────────────────────────────────────────
  function init() { ensureToggle(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }

  console.log("[codex-ux:approve-confirm-modal] 综合补丁已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:portfolio-pill-v2 — 组合气泡卡改造
 *  · 组名 + 组合预算 同一行(→ 分隔),压缩宽度
 *  · 未选浅蓝 / 选中浅薄荷绿 + ✓ 徽标 + pop 动画
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-portfolio-pill-v2-style";
  css.textContent = [
    /* 气泡行:紧一点,允许多张同排 */
    ".camp-portfolio-pills-row { gap: 6px !important; }",

    /* 气泡本体:grid 两行两列,name+amount 同行,budget 换行 */
    ".camp-portfolio-pill {",
    "  position: relative;",
    "  flex: 0 1 auto !important;",
    "  min-width: 0 !important;",
    "  max-width: 240px;",
    "  padding: 6px 12px 6px 12px !important;",
    "  border-radius: 8px !important;",
    "  border: 1.5px solid #BFDBFE !important;",
    "  background: #EFF6FF !important;",
    "  color: #1E3A8A !important;",
    "  display: grid !important;",
    "  grid-template-columns: auto 1fr;",
    "  grid-template-rows: auto auto;",
    "  align-items: center;",
    "  column-gap: 6px;",
    "  row-gap: 1px;",
    "  transition: background .2s ease, border-color .2s ease,",
    "              transform .12s ease, box-shadow .2s ease !important;",
    "}",
    ".camp-portfolio-pill:hover {",
    "  background: #DBEAFE !important;",
    "  border-color: #93C5FD !important;",
    "  transform: translateY(-1px);",
    "  box-shadow: 0 2px 6px rgba(59,130,246,.14);",
    "}",

    /* 名字:第一行左 */
    ".camp-portfolio-pill .pp-name {",
    "  grid-column: 1; grid-row: 1;",
    "  font-weight: 600 !important; font-size: 12.5px !important;",
    "  white-space: nowrap; line-height: 1.35;",
    "}",
    /* 金额:第一行右,→ 前缀 */
    ".camp-portfolio-pill .pp-amount {",
    "  grid-column: 2; grid-row: 1;",
    "  font-size: 12px !important; opacity: 1 !important;",
    "  color: #0F172A !important;",
    "  white-space: nowrap; line-height: 1.35;",
    "  margin-top: 0 !important;",
    "}",
    ".camp-portfolio-pill .pp-amount::before {",
    "  content: '→ '; color: #64748B; margin-right: 2px;",
    "}",
    /* 预算统计:第二行,跨两列 */
    ".camp-portfolio-pill .pp-budget {",
    "  grid-column: 1 / span 2; grid-row: 2;",
    "  font-size: 10.5px !important; opacity: 1 !important;",
    "  color: #64748B !important;",
    "  margin-top: 0 !important; line-height: 1.35;",
    "}",

    /* 选中态:浅薄荷绿 */
    ".camp-portfolio-pill.active {",
    "  background: #ECFDF5 !important;",
    "  border-color: #6EE7B7 !important;",
    "  color: #065F46 !important;",
    "  box-shadow: 0 2px 8px rgba(16,185,129,.18);",
    "  animation: codexPortfolioPop .22s ease-out;",
    "}",
    ".camp-portfolio-pill.active:hover {",
    "  background: #D1FAE5 !important;",
    "  border-color: #34D399 !important;",
    "}",
    ".camp-portfolio-pill.active .pp-name { color: #065F46 !important; }",
    ".camp-portfolio-pill.active .pp-amount { color: #047857 !important; }",
    ".camp-portfolio-pill.active .pp-amount::before { color: #10B981 !important; }",
    ".camp-portfolio-pill.active .pp-budget { color: #10B981 !important; }",
    /* 右上角 ✓ 徽标(仅选中时) */
    ".camp-portfolio-pill.active::after {",
    "  content: '✓';",
    "  position: absolute; top: 3px; right: 6px;",
    "  font-size: 10px; color: #059669; font-weight: 700;",
    "  animation: codexPortfolioCheckIn .22s ease-out;",
    "  pointer-events: none;",
    "}",

    "@keyframes codexPortfolioPop {",
    "  0% { transform: scale(.98); }",
    "  55% { transform: scale(1.02); }",
    "  100% { transform: scale(1); }",
    "}",
    "@keyframes codexPortfolioCheckIn {",
    "  from { opacity: 0; transform: scale(.4); }",
    "  to   { opacity: 1; transform: scale(1); }",
    "}",
  ].join("\n");
  var old = document.getElementById("codex-ux-portfolio-pill-v2-style");
  if (old) old.remove();
  document.head.appendChild(css);

  /* 把 "广告组合预算" 简化为 "组合预算"(vendor 每次 render 都会重置文本,
     由 MutationObserver 兜底,幂等,不会累积改字) */
  function trimAmountLabels() {
    var els = document.querySelectorAll(".camp-portfolio-pill .pp-amount");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var t = el.textContent;
      if (t && t.indexOf("广告组合预算") === 0) {
        el.textContent = t.replace(/^广告组合预算/, "组合预算");
      }
    }
  }

  var trimTimer = null;
  function scheduleTrim() {
    if (trimTimer) return;
    trimTimer = requestAnimationFrame(function () {
      trimTimer = null;
      trimAmountLabels();
    });
  }

  function attachPillObserver() {
    var row = document.getElementById("camp-portfolio-pills-row");
    if (!row || row._codexPillObserver) return;
    var obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].type === "childList" && muts[i].addedNodes.length) {
          scheduleTrim();
          return;
        }
      }
    });
    obs.observe(row, { childList: true, subtree: true });
    row._codexPillObserver = obs;
    scheduleTrim();
  }

  var _pillRetry = 30;
  (function retry() {
    attachPillObserver();
    if (!document.getElementById("camp-portfolio-pills-row") && _pillRetry-- > 0) {
      setTimeout(retry, 1000);
    }
  })();

  console.log("[codex-ux:portfolio-pill-v2] 组合气泡卡改造已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:meta-chipify — 卡片 meta 行 chip 化
 * 把 `× ASIN | EXACT | 关键词: xxx | 触发规则: yyy | 审核: AI_REVIEWED`
 * 转成分类彩色 chip,信息密度更高、扫读更快。
 *
 * 安全约束:
 *  1) 幂等:通过 firstElementChild.classList 判定,vendor 重渲染后自动重做
 *  2) parse 失败不动 innerHTML,保留原文,不影响功能
 *  3) 保留 `|` 分隔符(视觉隐藏),textContent 仍含 `|`,
 *     兼容 approve-confirm-modal 的 `关键词[:：]\s*([^|]+)` 提取
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-meta-chipify-style";
  css.textContent = [
    /* 仅在已 chipify 的 meta 上应用 flex 布局,
       避免 vendor 重渲染那 1-2 帧的纯文本 meta 高度与 chip 版本不一致导致抖动 */
    ".camp-adjustment-card .meta.codex-meta-chipped {",
    "  display: flex !important; flex-wrap: wrap;",
    "  gap: 3px 5px !important; align-items: center;",
    "  line-height: 1.6 !important;",
    "}",
    /* 分隔符视觉隐藏,textContent 保留 `|` */
    ".codex-meta-sep {",
    "  display: inline-block; width: 0; overflow: hidden;",
    "  color: transparent; user-select: none;",
    "}",
    /* 通用 chip 基础样式 */
    ".codex-meta-seg {",
    "  display: inline-flex; align-items: center;",
    "  padding: 1px 8px; border-radius: 10px;",
    "  font-size: 11px; line-height: 1.5;",
    "  background: #F1F5F9; color: #475569;",
    "  border: 1px solid #E2E8F0;",
    "  max-width: 100%;",
    "}",
    ".codex-meta-seg b {",
    "  font-weight: 600; color: #0F172A; margin-left: 2px;",
    "  max-width: 260px; overflow: hidden; text-overflow: ellipsis;",
    "  white-space: nowrap; display: inline-block; vertical-align: bottom;",
    "}",
    /* 类型色板 */
    ".codex-meta-seg.asin  { background: #F8FAFC; color: #475569; }",
    ".codex-meta-seg.asin  b { color: #334155; }",
    ".codex-meta-seg.match { background: #EFF6FF; color: #1E40AF; border-color: #BFDBFE; font-weight: 600; }",
    ".codex-meta-seg.kw    { background: #ECFDF5; color: #065F46; border-color: #A7F3D0; }",
    ".codex-meta-seg.kw    b { color: #064E3B; }",
    ".codex-meta-seg.rule  { background: #FAF5FF; color: #6B21A8; border-color: #E9D5FF; }",
    ".codex-meta-seg.rule  b { color: #581C87; }",
    ".codex-meta-seg.rule  code { background: transparent; font-family: inherit; font-weight: inherit; padding: 0; color: inherit; }",
    ".codex-meta-seg.audit { background: #FFFBEB; color: #92400E; border-color: #FDE68A; }",
    ".codex-meta-seg.audit b { color: #78350F; }",
    ".codex-meta-seg.rank  { background: #F0F9FF; color: #075985; border-color: #BAE6FD; }",
    ".codex-meta-seg.warn  { background: #FEF2F2; color: #991B1B; border-color: #FECACA; font-weight: 500; }",
    ".codex-meta-seg.class { background: #FDF4FF; color: #86198F; border-color: #F5D0FE; }",
    ".codex-meta-seg.misc  { background: #F1F5F9; color: #475569; }",
  ].join("\n");
  var old = document.getElementById("codex-ux-meta-chipify-style");
  if (old) old.remove();
  document.head.appendChild(css);

  function classifySegment(t) {
    if (!t) return "misc";
    if (t.charAt(0) === "×") return "asin";
    if (/^(EXACT|BROAD|PHRASE|AUTO)$/i.test(t)) return "match";
    if (t.indexOf("关键词") === 0) return "kw";
    if (t.indexOf("触发规则") === 0) return "rule";
    if (t.indexOf("审核") === 0) return "audit";
    if (t.indexOf("自然排名") === 0 || t.indexOf("已掉榜") >= 0) return "rank";
    if (t.indexOf("⚠") >= 0 || t.indexOf("核心词") >= 0) return "warn";
    if (/^(精准词|大词|竞品词|长尾词|类目词|其他词|自动词)/.test(t)) return "class";
    return "misc";
  }

  function escapeHtmlMeta(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function segToHtml(t) {
    var cls = classifySegment(t);
    var m = t.match(/^([^:：]+)[:：]\s*(.+)$/);
    var inner;
    if (m && (cls === "kw" || cls === "rule" || cls === "audit")) {
      inner = escapeHtmlMeta(m[1]) + ": <b>" + escapeHtmlMeta(m[2]) + "</b>";
    } else {
      inner = escapeHtmlMeta(t);
    }
    return "<span class=\"codex-meta-seg " + cls + "\">" + inner + "</span>";
  }

  function needsChipify(meta) {
    var first = meta.firstElementChild;
    return !(first && first.classList && first.classList.contains("codex-meta-seg"));
  }

  function chipifyOne(meta) {
    if (!meta) return;
    if (!needsChipify(meta)) return;
    var raw = (meta.textContent || "").replace(/\s+/g, " ").trim();
    if (!raw) return;
    var segs = raw.split("|").map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length; });
    if (!segs.length) return;
    try {
      var html = segs.map(segToHtml).join(
        "<span class=\"codex-meta-sep\">|</span>"
      );
      meta.innerHTML = html;
      meta.classList.add("codex-meta-chipped");
    } catch (e) {
      /* parse 失败 → 不动,保留原样 */
    }
  }

  function chipifyAll() {
    var metas = document.querySelectorAll(".camp-adjustment-card .meta");
    for (var i = 0; i < metas.length; i++) chipifyOne(metas[i]);
  }

  function attach() {
    var root = document.getElementById("campPanelRoot");
    if (!root || root._codexMetaObserver) return;
    var obs = new MutationObserver(function (muts) {
      var hasChild = false;
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].type === "childList" && muts[i].addedNodes.length) {
          hasChild = true; break;
        }
      }
      /* 同步执行 chipify(在浏览器绘制这批 mutation 之前完成),
         避免用户看到 "纯文本 meta → chip meta" 中间态导致的高度抖动 */
      if (hasChild) chipifyAll();
    });
    obs.observe(root, { childList: true, subtree: true });
    root._codexMetaObserver = obs;
    chipifyAll();
  }

  var _retry = 30;
  (function retry() {
    attach();
    if (!document.getElementById("campPanelRoot") && _retry-- > 0) {
      setTimeout(retry, 1000);
    }
  })();

  console.log("[codex-ux:meta-chipify] meta 行 chip 化已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:analyzing-overlay — 广告活动分析 tab5 专用 loading 遮罩
 *  · 触发:仅当 tab5 处于激活态 + /campaign/viewmodel 请求在飞行
 *  · 展示:居中 deepseek_loading.png(缺失 fallback 到 CSS 圆环)
 *         + 轮播文字 + 三点动画 + "放弃本次分析" 二步内联确认
 *  · 隐藏:所有分析请求结束 / 用户点确认放弃 / 60s 超时兜底
 *  · 安全:fail-open,任何环节报错都自动隐藏,不锁死界面
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var IMG_URL = "./deepseek_loading.png";
  // 只监听 tab5 那一次真正的重分析请求;snapshot 是快请求,不打扰
  var ANALYSIS_PATHS = ["/campaign/viewmodel"];
  var MESSAGES = [
    "🤖 AI 正在消化数据…",
    "📊 AI 拼命分析中…",
    "🔍 正在识别关键活动信号…",
    "⚙️ 正在生成调整建议…",
    "🧠 AI 在跟数据较劲,请稍等…",
    "✨ 快好了,正在整理输出…",
  ];
  var HIDE_SAFETY_MS = 60000;

  var css = document.createElement("style");
  css.id = "codex-ux-analyzing-style";
  css.textContent = [
    /* 遮罩 = absolute 铺满 tab5 面板,不覆盖顶部按钮/tab 栏/侧边栏 */
    "#panel-tab5.tab-panel { position: relative; }",
    ".codex-analyzing-overlay {",
    "  position: absolute; inset: 0; z-index: 500;",
    "  background: rgba(255,255,255,.82);",
    "  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);",
    "  display: flex; flex-direction: column;",
    "  align-items: center; justify-content: center;",
    "  animation: codexAnalyzeIn .25s ease-out;",
    "  border-radius: 6px;",
    "}",
    /* 我们的遮罩存在时,隐藏 vendor 的老 loading 文字(避免"正在运行分析...")穿透 */
    "#panel-tab5:has(.codex-analyzing-overlay) #camp-loader,",
    "#panel-tab5:has(.codex-analyzing-overlay) .camp-spinner {",
    "  display: none !important;",
    "}",
    ".codex-analyzing-inner {",
    "  display: flex; flex-direction: column; align-items: center;",
    "  gap: 18px; padding: 30px 40px;",
    "  transform: translateY(-30px);",
    "}",
    ".codex-analyzing-img {",
    "  width: 220px; height: 220px; object-fit: contain;",
    "  animation: codexAnalyzeBreathe 2.4s ease-in-out infinite;",
    "  mix-blend-mode: multiply; user-select: none; -webkit-user-drag: none;",
    "}",
    ".codex-analyzing-spinner {",
    "  width: 92px; height: 92px; border-radius: 50%;",
    "  border: 6px solid #E0F2FE; border-top-color: #1D4ED8;",
    "  animation: codexAnalyzeSpin 1s linear infinite;",
    "}",
    ".codex-analyzing-text {",
    "  font-size: 15px; color: #1E293B; font-weight: 600;",
    "  min-height: 22px; letter-spacing: .3px;",
    "  transition: opacity .25s ease; text-align: center;",
    "}",
    ".codex-analyzing-hint {",
    "  font-size: 11.5px; color: #64748B; margin-top: 2px;",
    "}",
    ".codex-analyzing-dots {",
    "  display: inline-flex; gap: 4px; margin-top: 2px;",
    "}",
    ".codex-analyzing-dots i {",
    "  width: 6px; height: 6px; border-radius: 50%;",
    "  background: #93C5FD;",
    "  animation: codexAnalyzeDot 1.2s ease-in-out infinite;",
    "}",
    ".codex-analyzing-dots i:nth-child(2){ animation-delay: .18s; }",
    ".codex-analyzing-dots i:nth-child(3){ animation-delay: .36s; }",
    /* 取消区 */
    ".codex-analyzing-cancel {",
    "  margin-top: 14px; display: flex; gap: 10px; align-items: center;",
    "}",
    ".codex-analyzing-cancel button {",
    "  border: 1px solid #E2E8F0; background: #fff; color: #64748B;",
    "  padding: 6px 16px; border-radius: 18px; font-size: 12px;",
    "  cursor: pointer; transition: all .15s ease;",
    "}",
    ".codex-analyzing-cancel button:hover { background: #F1F5F9; color: #334155; }",
    ".codex-analyzing-cancel button.confirm {",
    "  background: #FEF2F2; border-color: #FCA5A5; color: #B91C1C; font-weight: 600;",
    "}",
    ".codex-analyzing-cancel button.confirm:hover { background: #FEE2E2; }",
    ".codex-analyzing-cancel .prompt {",
    "  font-size: 12px; color: #B91C1C; margin-right: 4px;",
    "}",
    "@keyframes codexAnalyzeIn { from { opacity: 0; } to { opacity: 1; } }",
    "@keyframes codexAnalyzeBreathe {",
    "  0%,100% { transform: scale(1); }",
    "  50%     { transform: scale(1.04); }",
    "}",
    "@keyframes codexAnalyzeSpin { to { transform: rotate(360deg); } }",
    "@keyframes codexAnalyzeDot {",
    "  0%,80%,100% { opacity: .3; transform: translateY(0); }",
    "  40%         { opacity: 1;  transform: translateY(-4px); }",
    "}",
  ].join("\n");
  var _old = document.getElementById("codex-ux-analyzing-style");
  if (_old) _old.remove();
  document.head.appendChild(css);

  var overlayEl = null;
  var textEl = null;
  var rotateTimer = null;
  var safetyTimer = null;
  var msgIdx = 0;
  var inflightCount = 0;
  var manualUntil = 0;

  function isTab5Active() {
    var p = document.getElementById("panel-tab5");
    return !!(p && p.classList && p.classList.contains("active"));
  }

  function renderCancelDefault(cancelBox) {
    cancelBox.innerHTML = "";
    var b = document.createElement("button");
    b.textContent = "放弃本次分析";
    b.addEventListener("click", function () { renderCancelConfirm(cancelBox); });
    cancelBox.appendChild(b);
  }
  function renderCancelConfirm(cancelBox) {
    cancelBox.innerHTML = "";
    var tip = document.createElement("span");
    tip.className = "prompt";
    tip.textContent = "确认放弃?";
    var back = document.createElement("button");
    back.textContent = "继续等待";
    back.addEventListener("click", function () { renderCancelDefault(cancelBox); });
    var ok = document.createElement("button");
    ok.className = "confirm";
    ok.textContent = "确认放弃";
    ok.addEventListener("click", function () { doCancelAnalysis(); });
    cancelBox.appendChild(tip);
    cancelBox.appendChild(back);
    cancelBox.appendChild(ok);
  }

  function doCancelAnalysis() {
    // 归零飞行计数:被取消的 fetch 稍后 settle 时 decrement 会走 Math.max 保护,
    // 下次分析请求能干净地重新触发 overlay
    inflightCount = 0;
    hideOverlay(true);
    if (typeof window.onCancelEventClick !== "function") {
      console.warn("[codex-ux:analyzing] onCancelEventClick 不存在,回退到直接点原按钮");
      var btn = document.getElementById("btnCancelEvent");
      if (btn) btn.click();
      return;
    }
    // 临时接管 window.confirm 让 vendor 的 confirm() 直接通过(用户已在我们这确认过)
    var origConfirm = window.confirm;
    window.confirm = function () { return true; };
    try { window.onCancelEventClick(); }
    catch (e) { console.warn("[codex-ux:analyzing] cancel 报错:", e); }
    setTimeout(function () { window.confirm = origConfirm; }, 200);
  }

  function ensureOverlay() {
    if (overlayEl) return overlayEl;
    var wrap = document.createElement("div");
    wrap.className = "codex-analyzing-overlay";
    var inner = document.createElement("div");
    inner.className = "codex-analyzing-inner";
    var img = document.createElement("img");
    img.className = "codex-analyzing-img";
    img.src = IMG_URL;
    img.alt = "";
    img.onerror = function () {
      var sp = document.createElement("div");
      sp.className = "codex-analyzing-spinner";
      img.replaceWith(sp);
    };
    inner.appendChild(img);
    textEl = document.createElement("div");
    textEl.className = "codex-analyzing-text";
    textEl.textContent = MESSAGES[0];
    inner.appendChild(textEl);
    var dots = document.createElement("div");
    dots.className = "codex-analyzing-dots";
    dots.innerHTML = "<i></i><i></i><i></i>";
    inner.appendChild(dots);
    var hint = document.createElement("div");
    hint.className = "codex-analyzing-hint";
    hint.textContent = "AI 分析中,请勿刷新";
    inner.appendChild(hint);
    var cancelBox = document.createElement("div");
    cancelBox.className = "codex-analyzing-cancel";
    renderCancelDefault(cancelBox);
    inner.appendChild(cancelBox);
    wrap.appendChild(inner);
    // 挂到 tab5 面板里(而非 body),这样 tab 切走时随 panel 隐藏,不覆盖其他 tab
    var host = document.getElementById("panel-tab5") || document.body;
    host.appendChild(wrap);
    overlayEl = wrap;
    return wrap;
  }

  function startRotate() {
    if (rotateTimer) return;
    msgIdx = 0;
    rotateTimer = setInterval(function () {
      if (!textEl) return;
      msgIdx = (msgIdx + 1) % MESSAGES.length;
      textEl.style.opacity = "0";
      setTimeout(function () {
        if (!textEl) return;
        textEl.textContent = MESSAGES[msgIdx];
        textEl.style.opacity = "1";
      }, 220);
    }, 2500);
  }
  function stopRotate() {
    if (rotateTimer) { clearInterval(rotateTimer); rotateTimer = null; }
  }
  function showOverlay() {
    if (!isTab5Active()) return; // 只在广告活动分析 tab 展示
    try {
      ensureOverlay();
      overlayEl.style.display = "flex";
      overlayEl.style.opacity = "";
      startRotate();
      if (safetyTimer) clearTimeout(safetyTimer);
      safetyTimer = setTimeout(function () {
        console.warn("[codex-ux:analyzing] 60s 超时,强制隐藏 loading");
        hideOverlay(true);
      }, HIDE_SAFETY_MS);
    } catch (_) { hideOverlay(true); }
  }
  function hideOverlay(force) {
    if (!force && inflightCount > 0) return;
    stopRotate();
    if (safetyTimer) { clearTimeout(safetyTimer); safetyTimer = null; }
    if (overlayEl) {
      var el = overlayEl;
      el.style.transition = "opacity .25s";
      el.style.opacity = "0";
      setTimeout(function () {
        if (el && el.parentNode) el.parentNode.removeChild(el);
      }, 260);
      overlayEl = null; textEl = null;
    }
  }

  // 分析类 fetch 在飞行
  var _origFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input : (input && input.url) || "";
    var isAnalysis = false;
    for (var i = 0; i < ANALYSIS_PATHS.length; i++) {
      if (url.indexOf(ANALYSIS_PATHS[i]) >= 0) { isAnalysis = true; break; }
    }
    if (isAnalysis) { inflightCount++; showOverlay(); }
    var p;
    try { p = _origFetch.call(this, input, init); }
    catch (e) {
      if (isAnalysis) { inflightCount = Math.max(0, inflightCount - 1); hideOverlay(); }
      throw e;
    }
    if (isAnalysis) {
      p.then(function () {}, function () {}).then(function () {
        inflightCount = Math.max(0, inflightCount - 1);
        setTimeout(function () { if (inflightCount === 0) hideOverlay(); }, 400);
      });
    }
    return p;
  };

  // 触发三:vendor 的 #camp-loader 可见 = 分析进行中 → 保持 overlay 显示
  //          #camp-loader 隐藏 = 分析结束 → 隐藏 overlay(消除 fetch 结束到渲染完成之间的空档)
  //  · 只在"可见→不可见"或"不可见→可见"的**跃迁**触发,避免 sync 时误关刚开的 overlay
  //  · campPanelRoot 会重挂,#camp-loader 元素会被替换,故每秒轮询兜底重新绑定
  function watchVendorLoader() {
    var loader = document.getElementById("camp-loader");
    if (!loader || loader._codexAnalyzeWatched) return;
    loader._codexAnalyzeWatched = true;
    var prev = null;
    function sync() {
      var visible = !loader.classList.contains("hidden")
        && (loader.textContent || "").indexOf("正在运行分析") >= 0;
      if (prev === visible) return;
      prev = visible;
      if (visible) showOverlay();
      else if (inflightCount === 0) hideOverlay(true);
    }
    var obs = new MutationObserver(sync);
    obs.observe(loader, {
      attributes: true, attributeFilter: ["class"],
      childList: true, characterData: true, subtree: true,
    });
    sync();
  }
  setInterval(watchVendorLoader, 1000);

  console.log("[codex-ux:analyzing-overlay] 分析中 loading 遮罩已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:dd-panel-wide — 战略层下拉面板加宽,允许溢出侧栏
 *   产品定位/产品阶段/淡旺季 在窄侧栏里下拉选项被压得看不清 →
 *   打开时让面板 min-width: 340px,向右伸出,并把祖先 overflow 放开
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  var css = document.createElement("style");
  css.id = "codex-ux-dd-panel-wide";
  css.textContent = [
    /* #cardStrategy 提升 stacking 层级,让下拉浮层压过下面的策略层卡片 */
    "#cardStrategy { position: relative; z-index: 40; }",
    "#cardTactics  { position: relative; z-index: 1;  }",
    /* 面板打开时向右伸展,不再被 flex-item 1/3 宽度锁住 */
    "#cardStrategy .dd-panel, #cardStrategy .md-panel {",
    "  right: auto !important;",
    "  min-width: 340px !important;",
    "  max-width: 420px;",
    "  max-height: 340px !important;",
    "  z-index: 200 !important;",
    "}",
    "#cardStrategy .dd-panel .dd-option,",
    "#cardStrategy .md-panel .md-option {",
    "  padding: 8px 12px !important;",
    "  white-space: normal !important;",
    "}",
    "#cardStrategy .dd-panel .dd-opt-label,",
    "#cardStrategy .md-panel .md-opt-label {",
    "  font-size: 13px !important; font-weight: 600 !important;",
    "  color: #0F172A !important;",
    "}",
    "#cardStrategy .dd-panel .dd-opt-desc,",
    "#cardStrategy .md-panel .md-opt-desc {",
    "  font-size: 11.5px !important; color: #475569 !important;",
    "  margin-top: 3px !important; line-height: 1.5 !important;",
    "  white-space: normal !important; word-break: break-word;",
    "}",
    /* 祖先容器放开 overflow,别裁掉浮层 */
    "#cardStrategy, #cardStrategy .card-body, #cardStrategy #strategyContent,",
    "#cardStrategy .dd-wrapper, #cardStrategy .dd-wrapper > div {",
    "  overflow: visible !important;",
    "}",
    /* 靠右的第三个下拉容易撑出侧栏 → 打开时贴右侧起,往左伸 */
    "#cardStrategy #strategyContent > div > .dd-wrapper:last-child .dd-panel {",
    "  left: auto !important; right: 0 !important;",
    "}",
  ].join("\n");
  var old = document.getElementById("codex-ux-dd-panel-wide");
  if (old) old.remove();
  document.head.appendChild(css);
  console.log("[codex-ux:dd-panel-wide] 战略层下拉加宽已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:save-unified — 战略层保存配置 + 底部一键保存策略层
 *  · 隐藏:btnTactics / p3AcosLeftBtn / p3BudgetLeftBtn / btnSaveDirections
 *  · 顶部 btnStrategy: 改文字"保存配置",走 vendor 原生 confirmStrategy()
 *    (仅新建分析时 vendor 会自动 show 它,复用其 hidden class 逻辑)
 *  · 底部"一键保存策略层": 只在 btnTactics 存在时显示,
 *    点击保存 策略层 card 全部字段(tactics + ACOS + 预算 + 方向)
 *  · 无强制冷却时长:isSaving 拦重复点击,请求返回即解锁
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var TOAST_MS = 2200;

  var css = document.createElement("style");
  css.id = "codex-ux-save-unified";
  css.textContent = [
    /* 隐藏 4 个老保存按钮 + 附属 status 行(不占空间) */
    "#btnTactics,",
    "#p3AcosLeftBtn,",
    "#p3BudgetLeftBtn,",
    "#btnSaveDirections {",
    "  display: none !important;",
    "}",
    "#p3AcosLeftStatus:not(.codex-force-show),",
    "#p3BudgetLeftStatus:not(.codex-force-show),",
    "#leftDirectionsStatus:not(.codex-force-show) {",
    "  display: none !important;",
    "}",
    /* 战略层"保存配置"按钮 — 缩小 */
    "#btnStrategy {",
    "  margin-top: 6px !important;",
    "  padding: 5px 12px !important;",
    "  font-size: 12.5px !important;",
    "  font-weight: 600 !important;",
    "}",
    /* 底部一键保存(sticky 固定;默认隐藏,btnTactics 存在时才显示) */
    "#codexSaveAllBar {",
    "  position: sticky; bottom: 0; z-index: 20;",
    "  padding: 8px 0 6px;",
    "  background: linear-gradient(to top, #fff 70%, rgba(255,255,255,0));",
    "  margin-top: auto;",
    "  display: none;",
    "}",
    "#codexSaveAllBar.codex-visible { display: block; }",
    "#codexSaveAllBtn {",
    "  width: 100%; padding: 6px 12px;",
    "  background: #1D4ED8; color: #fff;",
    "  border: 0; border-radius: 6px;",
    "  font-size: 12.5px; font-weight: 600;",
    "  cursor: pointer;",
    "  box-shadow: 0 2px 6px rgba(29,78,216,.22);",
    "  transition: all .18s ease;",
    "  display: inline-flex; align-items: center; justify-content: center; gap: 5px;",
    "}",
    "#codexSaveAllBtn:hover:not([disabled]) {",
    "  background: #1E40AF;",
    "  box-shadow: 0 3px 8px rgba(29,78,216,.28);",
    "}",
    "#codexSaveAllBtn[disabled] {",
    "  background: #94A3B8 !important; cursor: not-allowed !important; opacity: .85;",
    "  box-shadow: none !important;",
    "}",
    "#codexSaveAllBtn .spinner-mini {",
    "  display: inline-block; width: 11px; height: 11px;",
    "  border: 2px solid rgba(255,255,255,.35);",
    "  border-top-color: #fff;",
    "  border-radius: 50%;",
    "  animation: codexSaveSpin .7s linear infinite;",
    "}",
    "@keyframes codexSaveSpin { to { transform: rotate(360deg); } }",
    /* 压缩侧栏卡片(整体页面变短) */
    ".sidebar > .card, #sidebar > .card {",
    "  padding: 10px 12px !important;",
    "  margin-bottom: 6px !important;",
    "}",
    ".sidebar > .card .card-title,",
    "#sidebar > .card .card-title {",
    "  margin-bottom: 4px !important;",
    "}",
    ".sidebar > .card .card-desc,",
    "#sidebar > .card .card-desc {",
    "  font-size: 11.5px !important; line-height: 1.5 !important;",
    "  margin-bottom: 4px !important;",
    "}",
    /* 反馈 toast */
    "#codexSaveToast {",
    "  position: fixed; bottom: 24px; left: 50%;",
    "  transform: translateX(-50%);",
    "  background: rgba(15,23,42,.94); color: #fff;",
    "  padding: 10px 20px; border-radius: 8px;",
    "  font-size: 12.5px; z-index: 9800;",
    "  box-shadow: 0 8px 24px rgba(0,0,0,.24);",
    "  animation: codexToastIn .22s ease-out;",
    "  max-width: 480px; text-align: center;",
    "}",
    "#codexSaveToast.success { background: rgba(6,95,70,.96); }",
    "#codexSaveToast.error   { background: rgba(153,27,27,.96); }",
    "#codexSaveToast .cnt {",
    "  display: inline-flex; gap: 8px; margin-left: 8px;",
    "}",
    "#codexSaveToast .cnt b { font-weight: 600; }",
    "#codexSaveToast .codex-toast-err {",
    "  margin-top: 6px; padding: 4px 8px;",
    "  background: rgba(255,255,255,.12);",
    "  border-left: 2px solid rgba(252,165,165,.9);",
    "  border-radius: 4px;",
    "  font-size: 11.5px; text-align: left;",
    "}",
    "#codexSaveToast .codex-toast-err + .codex-toast-err {",
    "  margin-top: 3px;",
    "}",
    "@keyframes codexToastIn {",
    "  from { opacity: 0; transform: translate(-50%, 10px); }",
    "  to   { opacity: 1; transform: translate(-50%, 0);   }",
    "}",
  ].join("\n");
  var old = document.getElementById("codex-ux-save-unified");
  if (old) old.remove();
  document.head.appendChild(css);

  var isSaving = false;

  // 只保存"策略层" card 里的字段:tactics + ACOS + 预算 + 方向
  // ⚠ vendor 的保存函数遇到无效输入时不抛异常,只显示内联红字后 return。
  //   所以必须先做同 vendor 一致的前置校验,失败直接记 fail、不再调 vendor。
  async function saveTacticsCard() {
    var results = [];

    // 1. 策略层(广告目的 + 关键词策略)
    var hasTacticsForm = !!document.querySelector('input[data-dim="ad_purposes"]');
    var apCbs = document.querySelectorAll('input[data-dim="ad_purposes"]:checked');
    var ktCbs = document.querySelectorAll('input[data-dim="target_keyword_strategy"]:checked');
    if (hasTacticsForm && typeof window.confirmTactics === "function") {
      if (apCbs.length === 0 || ktCbs.length === 0) {
        results.push({ k: "策略", ok: false, err: "至少各选 1 项(广告目的+关键词类型)" });
      } else {
        try { await window.confirmTactics(); results.push({ k: "策略", ok: true }); }
        catch (e) { results.push({ k: "策略", ok: false, err: e.message || "保存失败" }); }
      }
    }

    // 2. 目标 ACOS(校验:整数 5-100)
    var acosInput = document.getElementById("p3AcosLeftInput");
    if (acosInput && acosInput.value !== "" && typeof window.saveP3AcosLeft === "function") {
      var acosVal = parseInt(acosInput.value, 10);
      if (isNaN(acosVal) || acosVal < 5 || acosVal > 100) {
        results.push({ k: "ACOS", ok: false, err: "请输入 5-100 的整数" });
      } else {
        try { await window.saveP3AcosLeft(); results.push({ k: "ACOS", ok: true }); }
        catch (e) { results.push({ k: "ACOS", ok: false, err: e.message || "保存失败" }); }
      }
    }

    // 3. 每日预算(校验:>0)
    var budInput = document.getElementById("p3BudgetLeftInput");
    if (budInput && budInput.value !== "" && typeof window.saveP3BudgetLeft === "function") {
      var budVal = parseFloat(budInput.value);
      if (isNaN(budVal) || budVal <= 0) {
        results.push({ k: "预算", ok: false, err: "请输入 >0 的数值" });
      } else {
        try { await window.saveP3BudgetLeft(); results.push({ k: "预算", ok: true }); }
        catch (e) { results.push({ k: "预算", ok: false, err: e.message || "保存失败" }); }
      }
    }

    // 4. 广告方向(校验:至少 1 项)
    var hasDirsForm = !!document.querySelector('input[data-dim="directions"]');
    var dirCbs = document.querySelectorAll('input[data-dim="directions"]:checked');
    if (hasDirsForm && typeof window.saveDirectionsLeft === "function") {
      if (dirCbs.length === 0) {
        results.push({ k: "方向", ok: false, err: "请至少选 1 个方向" });
      } else {
        try { await window.saveDirectionsLeft(); results.push({ k: "方向", ok: true }); }
        catch (e) { results.push({ k: "方向", ok: false, err: e.message || "保存失败" }); }
      }
    }

    return results;
  }

  // 拦重复点击(无强制冷却时长,请求返回即解锁)
  async function saveTacticsClick() {
    if (isSaving) return;
    isSaving = true;
    lockBottomBtn(true);
    var results = [];
    try { results = await saveTacticsCard(); }
    catch (e) { results = [{ k: "系统", ok: false, err: e.message }]; }
    showToast(results);
    lockBottomBtn(false);
    isSaving = false;
  }

  function lockBottomBtn(locked) {
    var b = document.getElementById("codexSaveAllBtn");
    if (!b) return;
    if (locked) {
      b.disabled = true;
      b.innerHTML = '<span class="spinner-mini"></span> 保存中…';
    } else {
      b.disabled = false;
      b.innerHTML = "💾 一键保存策略层";
    }
  }

  function showToast(results) {
    var old = document.getElementById("codexSaveToast");
    if (old) old.remove();
    if (!results.length) {
      var w0 = document.createElement("div");
      w0.id = "codexSaveToast";
      w0.textContent = "ℹ 暂无可保存项(请先填写)";
      document.body.appendChild(w0);
      setTimeout(function () { if (w0.parentNode) w0.remove(); }, TOAST_MS);
      return;
    }
    var okCount = results.filter(function (r) { return r.ok; }).length;
    var failCount = results.length - okCount;
    var cls = failCount === 0 ? "success" : (okCount === 0 ? "error" : "");
    var wrap = document.createElement("div");
    wrap.id = "codexSaveToast";
    wrap.className = cls;
    var main = failCount === 0 ? "✓ 已保存" : (okCount === 0 ? "✗ 全部失败" : "⚠ 部分成功");
    // 头部 chip 行:全部字段 ✓/✗
    var chips = results.map(function (r) {
      return "<b>" + r.k + (r.ok ? " ✓" : " ✗") + "</b>";
    }).join(" · ");
    // 失败详情行:每个失败字段一行显示原因
    var errLines = results.filter(function (r) { return !r.ok && r.err; })
      .map(function (r) {
        return '<div class="codex-toast-err">✗ <b>' + r.k + '</b>:' + r.err + "</div>";
      }).join("");
    wrap.innerHTML = main + '<span class="cnt">' + chips + "</span>" + errLines;
    // 有失败时延长展示时长(让用户看清原因)
    var showMs = failCount > 0 ? TOAST_MS + 2000 : TOAST_MS;
    document.body.appendChild(wrap);
    setTimeout(function () {
      if (wrap.parentNode) {
        wrap.style.transition = "opacity .3s";
        wrap.style.opacity = "0";
        setTimeout(function () { if (wrap.parentNode) wrap.remove(); }, 320);
      }
    }, showMs);
  }

  function ensureBottomBtn() {
    var sidebar = document.querySelector(".sidebar, #sidebar, [class*='side-panel']");
    if (!sidebar || document.getElementById("codexSaveAllBar")) return;
    var bar = document.createElement("div");
    bar.id = "codexSaveAllBar";
    var btn = document.createElement("button");
    btn.id = "codexSaveAllBtn";
    btn.type = "button";
    btn.innerHTML = "💾 一键保存策略层";
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      saveTacticsClick();
    });
    bar.appendChild(btn);
    sidebar.appendChild(bar);
  }

  // 底部按钮可见性:仅当 vendor 的 btnTactics 存在于 DOM 时显示
  // (= 战略层已保存 → tactics 已挂载 = 新建分析进行中)
  function syncBottomVisibility() {
    var bar = document.getElementById("codexSaveAllBar");
    if (!bar) return;
    var hasTactics = !!document.getElementById("btnTactics");
    bar.classList.toggle("codex-visible", hasTactics);
  }

  // 顶部 btnStrategy: 只改文字,不拦截 click(走 vendor 原生 confirmStrategy())
  function renameStrategyBtn() {
    var b = document.getElementById("btnStrategy");
    if (!b) return;
    if (b.textContent === "保存" || b.textContent === "⏳ 保存中...") {
      b.textContent = "保存配置";
    }
  }

  setInterval(function () {
    ensureBottomBtn();
    renameStrategyBtn();
    syncBottomVisibility();
  }, 800);
  ensureBottomBtn();
  renameStrategyBtn();
  syncBottomVisibility();

  console.log("[codex-ux:save-unified] 保存配置 + 一键保存策略层 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:tab5-space — 广告活动分析页 卡片区扩容
 *  A · 顶部整体再压缩(策略总览 / 概览统计 / 预算 / sub-tab /
 *      筛选栏 / 组合气泡行 / 批量按钮 padding 全部收窄)
 *  C · 让 tab5 面板 flex-column,#camp-list 占满余下(min 55vh)
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-tab5-space";
  css.textContent = [
    /* A · 策略总览(占最多空间的大灰底) */
    "#camp-overview {",
    "  margin-bottom: 6px !important;",
    "  padding: 6px 10px !important;",
    "}",
    "#camp-overview .camp-card-title {",
    "  font-size: 12.5px !important; margin-bottom: 3px !important;",
    "}",
    "#camp-overview-body {",
    "  font-size: 12px !important; line-height: 1.55 !important;",
    "}",

    /* A · 分析概览统计(总活动/淘汰/调整) */
    ".camp-summary-grid {",
    "  padding: 3px 12px !important;",
    "  gap: 6px !important;",
    "  margin-bottom: 4px !important;",
    "}",
    ".camp-summary-grid .stat { padding: 2px 0 !important; }",
    ".camp-summary-grid .num { font-size: 14px !important; }",
    ".camp-summary-grid #camp-sum-meta {",
    "  font-size: 10.5px !important; margin-top: 0 !important;",
    "}",

    /* A · 预算汇总卡片 + 其他 camp-card */
    "#camp-budget-summary { margin-bottom: 4px !important; }",
    ".camp-main-area > .camp-card:not(#camp-overview) {",
    "  padding: 6px 12px !important; margin-bottom: 4px !important;",
    "}",

    /* A · sticky 控制区 padding */
    ".camp-sticky-controls {",
    "  padding-top: 2px !important; padding-bottom: 2px !important;",
    "}",
    /* A · 明细/汇总/告警 sub-tab */
    ".camp-tab-bar {",
    "  padding: 0 !important; margin: 0 0 4px !important;",
    "  min-height: 26px !important;",
    "}",
    ".camp-tab-bar button {",
    "  padding: 4px 12px !important; font-size: 12.5px !important;",
    "}",

    /* A · 筛选栏 */
    "#camp-filters {",
    "  padding: 3px 8px !important; margin-bottom: 3px !important;",
    "  gap: 5px !important;",
    "}",
    "#camp-filters select {",
    "  font-size: 11px !important; padding: 2px 5px !important;",
    "}",
    "#camp-filters input {",
    "  font-size: 11px !important; padding: 2px 6px !important;",
    "  min-height: 22px !important;",
    "}",
    ".camp-seg button {",
    "  padding: 2px 8px !important; font-size: 11px !important;",
    "}",

    /* A · 组合气泡行(压顶距) */
    ".camp-portfolio-pills-row {",
    "  margin-top: 3px !important; padding-top: 3px !important;",
    "}",

    /* A · 批量工具栏 */
    ".camp-batch-toolbar {",
    "  padding: 3px 6px !important; gap: 5px !important;",
    "  min-height: 30px !important;",
    "}",
    ".camp-batch-toolbar button {",
    "  padding: 4px 10px !important; font-size: 12px !important;",
    "}",
    ".camp-batch-toolbar #camp-batch-count {",
    "  font-size: 11.5px !important;",
    "}",

    /* C · flex column,#camp-list 占余下空间 */
    "#panel-tab5.tab-panel.active { display: flex !important; flex-direction: column; }",
    "#panel-tab5 > * { min-height: 0; }",
    "#panel-tab5 #campPanelRoot {",
    "  flex: 1 1 auto; min-height: 0;",
    "  display: flex; flex-direction: column;",
    "}",
    "#panel-tab5 .camp-root,",
    "#panel-tab5 .camp-app-layout,",
    "#panel-tab5 .camp-main-area {",
    "  flex: 1 1 auto; min-height: 0;",
    "  display: flex; flex-direction: column;",
    "}",
    /* C · 卡片列表:最少 55vh(保证初见一屏至少半屏是卡片) */
    "#panel-tab5 #camp-list {",
    "  flex: 1 1 auto;",
    "  min-height: 55vh;",
    "  overflow: visible;",
    "}",
  ].join("\n");
  var old = document.getElementById("codex-ux-tab5-space");
  if (old) old.remove();
  document.head.appendChild(css);

  console.log("[codex-ux:tab5-space] 卡片区扩容 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:err-retry — 全局错误 banner 出现时自动挂"🔄 刷新"按钮
 *  · 观察 #errGlobal 状态变化
 *  · 有可见错误文本 → 在其后面注入一个刷新按钮
 *  · 点击 = window.location.reload()
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-err-retry-style";
  css.textContent = [
    "#codexErrRetryBtn {",
    "  display: inline-flex; align-items: center; gap: 4px;",
    "  margin-left: 8px; padding: 3px 10px;",
    "  background: #DC2626; color: #fff;",
    "  border: 0; border-radius: 12px;",
    "  font-size: 11px; font-weight: 600;",
    "  cursor: pointer; vertical-align: middle;",
    "  transition: all .15s ease;",
    "}",
    "#codexErrRetryBtn:hover {",
    "  background: #B91C1C;",
    "  transform: translateY(-1px);",
    "  box-shadow: 0 2px 6px rgba(220,38,38,.28);",
    "}",
    "#codexErrRetryBtn:active { transform: translateY(0); }",
    "#codexErrRetryBtn:disabled {",
    "  opacity: .7; cursor: wait;",
    "  transform: none; box-shadow: none;",
    "}",
    /* err banner 稍加装饰,让红色更醒目 */
    "#errGlobal:not(.hidden) {",
    "  display: block !important;",
    "  padding: 6px 10px !important;",
    "  background: #FEF2F2 !important;",
    "  border: 1px solid #FECACA !important;",
    "  border-left: 3px solid #DC2626 !important;",
    "  border-radius: 6px !important;",
    "  color: #991B1B !important;",
    "  font-size: 12px !important;",
    "  line-height: 1.6 !important;",
    "  margin-top: 8px !important;",
    "}",
  ].join("\n");
  var oldStyle = document.getElementById("codex-ux-err-retry-style");
  if (oldStyle) oldStyle.remove();
  document.head.appendChild(css);

  function ensureRetryBtn(el) {
    if (!el) return;
    if (el.querySelector("#codexErrRetryBtn")) return; // 已有
    var btn = document.createElement("button");
    btn.id = "codexErrRetryBtn";
    btn.type = "button";
    btn.innerHTML = "🔄 刷新重试";
    btn.title = "刷新页面重新加载数据";
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      btn.disabled = true;
      btn.innerHTML = "🔄 刷新中…";
      window.location.reload();
    });
    el.appendChild(btn);
  }

  function removeRetryBtn(el) {
    if (!el) return;
    var b = el.querySelector("#codexErrRetryBtn");
    if (b) b.remove();
  }

  function sync() {
    var el = document.getElementById("errGlobal");
    if (!el) return;
    var visible = !el.classList.contains("hidden");
    // 排除按钮自身文本,只算错误 message 内容
    var textOnly = (el.textContent || "").replace(/🔄 刷新重试|🔄 刷新中…/g, "")
      .replace(/\s+/g, "").length;
    if (visible && textOnly > 0) ensureRetryBtn(el);
    else removeRetryBtn(el);
  }

  function attach() {
    var el = document.getElementById("errGlobal");
    if (!el || el._codexRetryWatched) return;
    el._codexRetryWatched = true;
    var obs = new MutationObserver(sync);
    obs.observe(el, {
      attributes: true, attributeFilter: ["class"],
      childList: true, characterData: true, subtree: true,
    });
    sync();
  }

  // 每秒兜底(vendor 可能延迟挂载/替换 #errGlobal)
  setInterval(attach, 1000);
  attach();

  console.log("[codex-ux:err-retry] 全局错误刷新按钮 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:preserve-manual — 防"保存配置"覆盖运营手填的 ACOS/预算/方向
 *
 *  Bug 场景:
 *   1. 用户填 ACOS=25 / 预算=100 / 勾选精准主力/精准测试
 *   2. 点顶部"保存配置"(btnStrategy)
 *   3. vendor 内部链:
 *      confirmStrategy → loadTactics(returnVisit) → enableTabsForReturnVisit
 *      → loadMainInsightPanels → loadP3Rec.autoFillLeftInputs()
 *        清空 aIn/bIn.value = ''
 *      → loadExecution() 用 innerHTML 重建方向下拉,勾选归零到 AI 默认
 *   4. 用户手填被覆盖
 *
 *  修法:
 *   · 用户交互跟踪 flag(input/change 事件)
 *   · 拦截 autoFillLeftInputs:若用户碰过 ACOS/预算,vendor 跑完后恢复
 *   · 拦截 loadExecution:若用户碰过方向,vendor 重建 DOM 后恢复选中
 *   · 例外:"🔄 AI 重新推荐"点击 → 允许覆盖一次,并清 touched 状态
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var touched = { acos: false, budget: false, directions: false };
  var allowOverrideNext = false;

  // 1. 跟踪用户输入
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (!t || !t.id) return;
    if (t.id === "p3AcosLeftInput") touched.acos = true;
    else if (t.id === "p3BudgetLeftInput") touched.budget = true;
  }, true);
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('input[data-dim="directions"]')) {
      touched.directions = true;
    }
  }, true);

  // 2. "🔄 AI 重新推荐"点击 → 下一次 fill 允许覆盖
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest && e.target.closest("#btnRefreshP3");
    if (b) {
      allowOverrideNext = true;
      touched.acos = false;
      touched.budget = false;
    }
  }, true);

  function wrapAutoFill() {
    var fn = window.autoFillLeftInputs;
    if (typeof fn !== "function" || fn._codexWrapped) return;
    window.autoFillLeftInputs = function (data) {
      var aIn = document.getElementById("p3AcosLeftInput");
      var bIn = document.getElementById("p3BudgetLeftInput");
      var savedA = aIn ? aIn.value : "";
      var savedB = bIn ? bIn.value : "";
      var allow = allowOverrideNext;
      allowOverrideNext = false;

      fn.call(this, data);

      if (!allow) {
        if (aIn && touched.acos && savedA !== "") aIn.value = savedA;
        if (bIn && touched.budget && savedB !== "") bIn.value = savedB;
      }
    };
    window.autoFillLeftInputs._codexWrapped = true;
  }

  function wrapLoadExecution() {
    var fn = window.loadExecution;
    if (typeof fn !== "function" || fn._codexWrapped) return;
    window.loadExecution = async function () {
      var checked = Array.prototype.map.call(
        document.querySelectorAll('input[data-dim="directions"]:checked'),
        function (cb) { return cb.value; }
      );
      var wasTouched = touched.directions;
      var res = await fn.apply(this, arguments);
      if (wasTouched) {
        // vendor 用 innerHTML 重建 DOM,等一帧让 vendor 渲染完再改选中
        requestAnimationFrame(function () {
          document.querySelectorAll('input[data-dim="directions"]').forEach(function (cb) {
            cb.checked = checked.indexOf(cb.value) >= 0;
          });
          if (typeof window.updateMultiTags === "function") {
            try { window.updateMultiTags("directions"); } catch (_) {}
          }
        });
      }
      return res;
    };
    window.loadExecution._codexWrapped = true;
  }

  wrapAutoFill();
  wrapLoadExecution();
  setInterval(function () {
    wrapAutoFill();
    wrapLoadExecution();
  }, 1000);

  console.log("[codex-ux:preserve-manual] 保护手填 ACOS/预算/方向 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:main-empty-spinner — 主内容加载中态换成友好 spinner + 新文案
 *  · 原:"正在从 ERP 上下文加载分析…" → 新:"AI 正在从 ERP 拉数据……"
 *  · 加旋转 spinner(纯 CSS ::before)
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-main-empty-spinner";
  css.textContent = [
    "#mainEmpty:not(.hidden):not(:empty) {",
    "  display: flex !important;",
    "  align-items: center; justify-content: center;",
    "  gap: 10px;",
    "  font-size: 13.5px !important;",
    "  color: #475569 !important;",
    "}",
    "#mainEmpty:not(.hidden):not(:empty)::before {",
    "  content: \"\";",
    "  width: 16px; height: 16px;",
    "  border: 2px solid #E0E7FF;",
    "  border-top-color: #1D4ED8;",
    "  border-radius: 50%;",
    "  animation: codexMainEmptySpin 0.7s linear infinite;",
    "  flex-shrink: 0;",
    "}",
    "@keyframes codexMainEmptySpin { to { transform: rotate(360deg); } }",
  ].join("\n");
  var old = document.getElementById("codex-ux-main-empty-spinner");
  if (old) old.remove();
  document.head.appendChild(css);

  var TEXT_MAP = {
    "正在从 ERP 上下文加载分析…": "AI 正在从 ERP 拉数据……",
    "正在从 ERP 上下文加载分析...": "AI 正在从 ERP 拉数据……",
  };

  function syncEmpty() {
    var el = document.getElementById("mainEmpty");
    if (!el) return;
    var t = (el.textContent || "").trim();
    if (TEXT_MAP[t]) {
      el.textContent = TEXT_MAP[t];
    }
  }

  function attach() {
    var el = document.getElementById("mainEmpty");
    if (!el || el._codexEmptyWatched) return;
    el._codexEmptyWatched = true;
    var obs = new MutationObserver(syncEmpty);
    obs.observe(el, {
      childList: true, characterData: true, subtree: true,
      attributes: true, attributeFilter: ["class"],
    });
    syncEmpty();
  }

  attach();
  setInterval(attach, 1500);

  console.log("[codex-ux:main-empty-spinner] 加载中态友好化 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:ux-polish — UX 全站抛光
 *  A · alert() 拦截 → 优雅浮层 toast(不阻塞流程)
 *  B · "运行执行层分析" → "🚀 生成调整建议(AI 分析)"
 *  C · 英文常量翻译(触发规则、审核字段)
 *  D · 按钮颜色对调("同意"主蓝、"驳回"淡红)
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-polish";
  css.textContent = [
    /* toast 样式(通用) */
    "#codexAlertToast {",
    "  position: fixed; top: 24px; left: 50%;",
    "  transform: translateX(-50%);",
    "  background: rgba(15,23,42,.96); color: #fff;",
    "  padding: 12px 20px; border-radius: 10px;",
    "  font-size: 13px; z-index: 9900;",
    "  box-shadow: 0 8px 28px rgba(0,0,0,.28);",
    "  max-width: 480px; text-align: center;",
    "  animation: codexToastDropIn .28s cubic-bezier(.2,.9,.3,1);",
    "  display: inline-flex; align-items: center; gap: 8px;",
    "}",
    "#codexAlertToast.info    { background: rgba(30,64,175,.96); }",
    "#codexAlertToast.success { background: rgba(6,95,70,.96); }",
    "#codexAlertToast.warn    { background: rgba(146,64,14,.96); }",
    "#codexAlertToast.error   { background: rgba(153,27,27,.96); }",
    "#codexAlertToast .codex-alert-icon { font-size: 16px; }",
    "@keyframes codexToastDropIn {",
    "  from { opacity: 0; transform: translate(-50%, -18px); }",
    "  to   { opacity: 1; transform: translate(-50%, 0);      }",
    "}",
    /* 按钮颜色对调 */
    "button[data-action=\"camp-batch-approve\"] {",
    "  background: #1D4ED8 !important; color: #fff !important;",
    "  border: 0 !important;",
    "  transition: background .18s ease, box-shadow .18s ease, transform .12s !important;",
    "}",
    "button[data-action=\"camp-batch-approve\"]:hover:not([disabled]) {",
    "  background: #1E40AF !important;",
    "  box-shadow: 0 3px 10px rgba(29,78,216,.28) !important;",
    "}",
    "button[data-action=\"camp-batch-approve\"]:active:not([disabled]) {",
    "  transform: translateY(1px);",
    "}",
    "button[data-action=\"camp-batch-reject\"] {",
    "  background: #FEF2F2 !important; color: #991B1B !important;",
    "  border: 1px solid #FCA5A5 !important;",
    "  transition: background .18s ease !important;",
    "}",
    "button[data-action=\"camp-batch-reject\"]:hover:not([disabled]) {",
    "  background: #FEE2E2 !important;",
    "}",
  ].join("\n");
  var _old = document.getElementById("codex-ux-polish");
  if (_old) _old.remove();
  document.head.appendChild(css);

  /* ─── A · alert() 拦截 ─── */
  var _alertKindDetect = function (msg) {
    var m = String(msg || "");
    if (/失败|错误|无法|超时|error|fail/i.test(m)) return "error";
    if (/未保存|请先|请勿|拦下|注意|警告/.test(m)) return "warn";
    if (/成功|完成|已.*成|已创建/.test(m)) return "success";
    return "info";
  };
  var _kindIcon = { info: "ℹ", success: "✓", warn: "⚠", error: "✗" };

  function showAlertToast(msg, kind) {
    var old = document.getElementById("codexAlertToast");
    if (old) old.remove();
    var el = document.createElement("div");
    el.id = "codexAlertToast";
    el.className = kind;
    el.innerHTML = '<span class="codex-alert-icon">' + (_kindIcon[kind] || "ℹ") + "</span>"
      + '<span>' + escapeAlert(String(msg || "")) + "</span>";
    document.body.appendChild(el);
    // 错误 5s、警告 4s、其他 3s
    var dur = kind === "error" ? 5000 : (kind === "warn" ? 4000 : 3000);
    setTimeout(function () {
      if (el.parentNode) {
        el.style.transition = "opacity .28s, transform .28s";
        el.style.opacity = "0";
        el.style.transform = "translate(-50%, -12px)";
        setTimeout(function () { if (el.parentNode) el.remove(); }, 320);
      }
    }, dur);
  }
  function escapeAlert(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  var _origAlert = window.alert;
  window.alert = function (msg) {
    try { showAlertToast(msg, _alertKindDetect(msg)); }
    catch (_) { _origAlert.call(window, msg); }
  };

  /* ─── B · 按钮改名 ─── */
  var RENAME_MAP = {
    campRunRealtimeBtn: "🚀 生成调整建议(AI 分析)",
  };
  function renameKnownButtons() {
    Object.keys(RENAME_MAP).forEach(function (id) {
      var el = document.getElementById(id);
      if (el && el.textContent.trim() !== RENAME_MAP[id]) {
        // 保留原始 title 提示
        if (!el.dataset.codexOrigTitle) el.dataset.codexOrigTitle = el.textContent.trim();
        el.textContent = RENAME_MAP[id];
        if (!el.title) el.title = "触发 tab5 AI 分析生成建议清单 · 不下发亚马逊(需要另外点『同意执行所选』)";
      }
    });
  }
  setInterval(renameKnownButtons, 1000);
  renameKnownButtons();

  /* ─── C · 英文常量翻译 ─── */
  var CONST_TRANS = {
    // 触发规则
    BUDGET_NO_SPEND: "预算充足未花完",
    LOW_BID: "出价偏低",
    HIGH_BID: "出价偏高",
    ALL_HEALTHY: "健康",
    HIGH_ACOS: "ACOS 偏高",
    LOW_ACOS: "ACOS 偏低",
    HIGH_SPEND_NO_ORDERS: "花费高无订单",
    NO_KEYWORD_DATA: "关键词无数据",
    CVR_LOW: "CVR 偏低",
    CVR_HIGH: "CVR 偏高",
    CTR_LOW: "CTR 偏低",
    NO_IMPRESSIONS: "无曝光",
    NEW_KEYWORD: "新关键词",
    // 审核级别(部分要藏)
    AI_REVIEWED: "AI 已复核",
    AUTO_BATCHABLE: "可批量执行",
    MANUAL_REVIEW: "需人工审核",
    NEED_HUMAN: "需人工确认",
  };
  function translateConsts(root) {
    if (!root) return;
    // 找 codex-meta-seg 里的 <b> 元素("触发规则:X" / "审核:Y" 的 X/Y)
    var els = root.querySelectorAll(".codex-meta-seg.rule b, .codex-meta-seg.audit b");
    for (var i = 0; i < els.length; i++) {
      var b = els[i];
      if (b.dataset.codexTrans === "1") continue;
      var t = (b.textContent || "").trim();
      var zh = CONST_TRANS[t];
      if (zh) {
        b.textContent = zh;
        b.title = t;              // hover 显示原英文,方便技术追溯
        b.dataset.codexTrans = "1";
      }
    }
  }
  var _consTimer = null;
  function scheduleConsts() {
    if (_consTimer) return;
    _consTimer = requestAnimationFrame(function () {
      _consTimer = null;
      translateConsts(document);
    });
  }
  var _consObs = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      if (muts[i].type === "childList" && muts[i].addedNodes.length) {
        scheduleConsts(); return;
      }
    }
  });
  _consObs.observe(document.body, { childList: true, subtree: true });
  scheduleConsts();

  console.log("[codex-ux:ux-polish] alert 拦截 + 按钮改名 + 常量翻译 + 按钮色对调 已加载");
})();

/* ═══════════════════════════════════════════════════════════════════
 * codex-ux:soft-anim-load — 全站柔和动画 + 加载中提示补齐
 *  · 卡片/按钮/tab hover/active 微动效
 *  · 标签切换淡入
 *  · 卡片列表首次渲染骨架屏
 *  · 筛选框应用视觉反馈
 *  · fetch 中长请求点数提示("加载中...")
 * ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var css = document.createElement("style");
  css.id = "codex-ux-soft-anim";
  css.textContent = [
    /* 全局按钮微交互 */
    "button:not([disabled]) {",
    "  transition: background .18s ease, box-shadow .18s ease,",
    "              transform .1s ease, opacity .15s !important;",
    "}",
    "button:not([disabled]):active {",
    "  transform: translateY(1px);",
    "}",
    /* 卡片微 hover(不改选中态阴影) */
    ".camp-adjustment-card:not(.codex-selected):hover {",
    "  box-shadow: 0 3px 10px rgba(15,23,42,.06);",
    "  transition: box-shadow .18s ease, transform .12s ease;",
    "}",
    /* Tab 切换淡入 */
    ".tab-panel.active {",
    "  animation: codexTabFadeIn .22s cubic-bezier(.2,.9,.3,1);",
    "}",
    "@keyframes codexTabFadeIn {",
    "  from { opacity: 0; transform: translateY(4px); }",
    "  to   { opacity: 1; transform: translateY(0);   }",
    "}",
    /* 分析概览数字变化过渡 */
    ".camp-summary-grid .num { transition: color .3s, transform .2s; }",
    /* 筛选值改变时的短暂高亮 */
    "#camp-filters select.codex-filter-flash,",
    "#camp-filters input.codex-filter-flash {",
    "  animation: codexFilterFlash .55s ease-out;",
    "}",
    "@keyframes codexFilterFlash {",
    "  0%   { background: #DBEAFE; box-shadow: 0 0 0 3px rgba(59,130,246,.2); }",
    "  100% { background: transparent; box-shadow: 0 0 0 0 transparent; }",
    "}",
    /* 骨架屏(shimmer) */
    ".codex-skel {",
    "  background: linear-gradient(90deg,#EDF2F7 25%,#E2E8F0 50%,#EDF2F7 75%);",
    "  background-size: 200% 100%;",
    "  animation: codexShimmer 1.4s linear infinite;",
    "  border-radius: 6px;",
    "}",
    "@keyframes codexShimmer {",
    "  0%   { background-position: 200% 0; }",
    "  100% { background-position: -200% 0; }",
    "}",
    /* 长请求点点提示 · sticky bottom-left,不打扰 */
    "#codexReqHint {",
    "  position: fixed; bottom: 22px; left: 22px;",
    "  z-index: 40; padding: 8px 14px;",
    "  background: rgba(30,64,175,.94); color: #fff;",
    "  font-size: 12px; border-radius: 18px;",
    "  box-shadow: 0 4px 12px rgba(29,78,216,.22);",
    "  display: inline-flex; align-items: center; gap: 8px;",
    "  animation: codexReqIn .2s ease-out;",
    "  pointer-events: none;",
    "}",
    "#codexReqHint .dot {",
    "  width: 6px; height: 6px; border-radius: 50%;",
    "  background: rgba(255,255,255,.9);",
    "  animation: codexReqDot 1s ease-in-out infinite;",
    "}",
    "#codexReqHint .dot:nth-child(2){ animation-delay: .15s; }",
    "#codexReqHint .dot:nth-child(3){ animation-delay: .30s; }",
    "@keyframes codexReqIn {",
    "  from { opacity: 0; transform: translateY(6px); }",
    "  to   { opacity: 1; transform: translateY(0);   }",
    "}",
    "@keyframes codexReqDot {",
    "  0%,60%,100% { opacity: .3; transform: scale(.7); }",
    "  30%          { opacity: 1;  transform: scale(1);  }",
    "}",
    /* 按钮 loading 变体(点击后自动脉冲) */
    ".btn.codex-btn-pulse {",
    "  position: relative; overflow: hidden;",
    "}",
    ".btn.codex-btn-pulse::after {",
    "  content: ''; position: absolute; inset: 0;",
    "  background: rgba(255,255,255,.28);",
    "  transform: translateX(-100%);",
    "  animation: codexBtnSweep .7s ease-out;",
    "  pointer-events: none;",
    "}",
    "@keyframes codexBtnSweep {",
    "  from { transform: translateX(-100%); }",
    "  to   { transform: translateX(100%);  }",
    "}",
  ].join("\n");
  var _o2 = document.getElementById("codex-ux-soft-anim");
  if (_o2) _o2.remove();
  document.head.appendChild(css);

  /* ─── 筛选值变化时闪一下 ─── */
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.matches) return;
    if (t.matches("#camp-filters select, #camp-filters input")) {
      t.classList.remove("codex-filter-flash");
      // 触发重放
      void t.offsetWidth;
      t.classList.add("codex-filter-flash");
      setTimeout(function () { t.classList.remove("codex-filter-flash"); }, 600);
    }
  }, true);

  /* ─── 按钮点击时短暂脉冲(仅一次)─── */
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var b = t.closest("button.btn:not([disabled])");
    if (!b) return;
    // 跳过我们已经有自己 loading 的按钮
    if (b.id === "codexSaveAllBtn" || b.id === "btnStrategy") return;
    b.classList.add("codex-btn-pulse");
    setTimeout(function () { b.classList.remove("codex-btn-pulse"); }, 720);
  }, true);

  /* ─── 长请求(> 800ms)显示点点提示 ─── */
  // 只 hint 慢的接口,并防止和 tab5 loading overlay 冲突
  var HINT_PATHS = [
    "/tactics/options", "/tactics/recommend",
    "/execution/options", "/execution/recommend",
    "/diagnosis", "/p3/recommend",
    "/decision/new-event", "/direction/analyze",
    "/campaign/snapshot",
  ];
  var _reqDepth = 0;
  var _reqHintTimer = null;
  var _reqHintEl = null;
  function showReqHint() {
    if (_reqHintEl) return;
    var el = document.createElement("div");
    el.id = "codexReqHint";
    el.innerHTML = '<span>加载中</span><span class="dot"></span><span class="dot"></span><span class="dot"></span>';
    document.body.appendChild(el);
    _reqHintEl = el;
  }
  function hideReqHint() {
    if (!_reqHintEl) return;
    var el = _reqHintEl;
    el.style.transition = "opacity .2s";
    el.style.opacity = "0";
    setTimeout(function () { if (el.parentNode) el.remove(); }, 220);
    _reqHintEl = null;
  }

  var _origFetch2 = window.fetch;
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input : (input && input.url) || "";
    var isSlow = HINT_PATHS.some(function (p) { return url.indexOf(p) >= 0; });
    // tab5 遮罩优先,不叠加
    var tab5Loading = !!document.querySelector(".codex-analyzing-overlay");
    if (isSlow && !tab5Loading) {
      _reqDepth++;
      if (_reqDepth === 1) {
        // 800ms 才出现,快请求不打扰
        if (_reqHintTimer) clearTimeout(_reqHintTimer);
        _reqHintTimer = setTimeout(showReqHint, 800);
      }
    }
    var p;
    try { p = _origFetch2.call(this, input, init); }
    catch (e) {
      if (isSlow) {
        _reqDepth = Math.max(0, _reqDepth - 1);
        if (_reqDepth === 0) {
          if (_reqHintTimer) { clearTimeout(_reqHintTimer); _reqHintTimer = null; }
          hideReqHint();
        }
      }
      throw e;
    }
    if (isSlow) {
      p.then(function () {}, function () {}).then(function () {
        _reqDepth = Math.max(0, _reqDepth - 1);
        if (_reqDepth === 0) {
          if (_reqHintTimer) { clearTimeout(_reqHintTimer); _reqHintTimer = null; }
          hideReqHint();
        }
      });
    }
    return p;
  };

  console.log("[codex-ux:soft-anim-load] 柔和动画 + 加载提示 已加载");
})();
