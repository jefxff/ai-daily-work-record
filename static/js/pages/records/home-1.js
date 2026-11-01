(function () {
    document.addEventListener('DOMContentLoaded', function () {
        // 迷你趋势条 / 进度条：模板值经 data-* 传递，避免内联 style 出现模板语法
        document.querySelectorAll('.home-spark__bar[data-h]').forEach(function (bar) {
            bar.style.height = bar.dataset.h + 'px';
        });
        document.querySelectorAll('.home-progress__bar i[data-w]').forEach(function (bar) {
            bar.style.width = bar.dataset.w + '%';
        });

        // 需要关注：Tab 切换（点击 + 左右方向键），一次只显示一个面板
        var tablist = document.querySelector('.home-tabs');
        if (!tablist) return;
        var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
        var panes = Array.prototype.slice.call(document.querySelectorAll('[data-home-pane]'));
        if (!tabs.length) return;

        function activate(tab) {
            tabs.forEach(function (t) {
                var on = t === tab;
                t.setAttribute('aria-selected', on ? 'true' : 'false');
                t.tabIndex = on ? 0 : -1;
            });
            panes.forEach(function (p) {
                p.hidden = p.getAttribute('data-home-pane') !== tab.getAttribute('data-tab');
            });
        }

        tabs.forEach(function (tab, index) {
            tab.addEventListener('click', function () { activate(tab); });
            tab.addEventListener('keydown', function (e) {
                var next = null;
                if (e.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
                else if (e.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
                if (next) { e.preventDefault(); activate(next); next.focus(); }
            });
        });

        var current = tablist.querySelector('[role="tab"][aria-selected="true"]') || tabs[0];
        activate(current);
    });
})();
