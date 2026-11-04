/* ==========================================================================
   全局视图模式切换（full / limited）
   --------------------------------------------------------------------------
   行为：
     full     默认，内容轨道铺满（与改造前完全一致）
     limited  内容限宽居中（main#main max-width: 1440px，见 main.css）

   持久化：localStorage['viewMode']，刷新、跳转、跨标签页都保持。

   加载方式：**同步**放在 <head>（不加 defer）。
   本文件在解析到 <head> 时就会读一次 localStorage 并把状态写到
   <html data-view-mode>，这样样式表计算第一帧时就已经是目标模式，
   不会出现「先全宽铺满、再跳成限宽居中」的闪烁。
   按钮绑定推迟到 DOMContentLoaded，因为顶栏此时还没解析出来。
   ========================================================================== */
(function () {
    'use strict';

    var STORAGE_KEY = 'viewMode';
    var MODE_FULL = 'full';
    var MODE_LIMITED = 'limited';

    var root = document.documentElement;
    var mainEl = null;
    var btn = null;

    function readMode() {
        try {
            return localStorage.getItem(STORAGE_KEY) === MODE_LIMITED ? MODE_LIMITED : MODE_FULL;
        } catch (e) {
            // 隐私模式 / 禁用存储：按默认全宽处理，不抛错，不影响页面
            return MODE_FULL;
        }
    }

    function saveMode(mode) {
        try { localStorage.setItem(STORAGE_KEY, mode); } catch (e) {}
    }

    function labelFor(mode) {
        return mode === MODE_LIMITED ? '切换为全宽视图' : '切换为限宽居中视图';
    }

    /* 把模式落到 DOM。mainEl 尚未解析时必须容忍 null——
       首屏只依赖 <html data-view-mode>（CSS 已单独处理），
       按钮与 main 的类名在 DOMContentLoaded 补齐。 */
    function apply(mode) {
        root.setAttribute('data-view-mode', mode);

        if (mainEl) mainEl.classList.toggle('layout-limited', mode === MODE_LIMITED);

        if (btn) {
            var label = labelFor(mode);
            btn.setAttribute('aria-pressed', mode === MODE_LIMITED ? 'true' : 'false');
            btn.setAttribute('aria-label', label);
            btn.title = label;
        }
    }

    // ① 首帧：立刻写入，避免闪烁
    var current = readMode();
    apply(current);

    function init() {
        mainEl = document.getElementById('main');
        btn = document.getElementById('viewModeToggle');

        if (btn) {
            btn.addEventListener('click', function () {
                current = current === MODE_LIMITED ? MODE_FULL : MODE_LIMITED;
                apply(current);
                saveMode(current);
            });
        }

        apply(current);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    /* 多标签页同步：在另一个标签页切换后，本页跟着变（不写回存储，避免回声） */
    window.addEventListener('storage', function (e) {
        if (e.key !== STORAGE_KEY) return;
        current = e.newValue === MODE_LIMITED ? MODE_LIMITED : MODE_FULL;
        apply(current);
    });
})();
