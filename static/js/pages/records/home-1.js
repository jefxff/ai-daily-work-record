(function () {
    document.addEventListener('DOMContentLoaded', function () {
        // 迷你趋势条 / 进度条：模板值经 data-* 传递，避免内联 style 出现模板语法
        document.querySelectorAll('.home-spark__bar[data-h]').forEach(function (bar) {
            bar.style.height = bar.dataset.h + 'px';
        });
        document.querySelectorAll('.home-progress__bar i[data-w]').forEach(function (bar) {
            bar.style.width = bar.dataset.w + '%';
        });

        // 待办面板 tab 切换：纯前端显隐，各来源数据已随页面下发（无额外请求）
        var tabs = Array.prototype.slice.call(document.querySelectorAll('.home-ttab'));
        if (tabs.length) {
            var panes = {};
            document.querySelectorAll('.home-tpane').forEach(function (pane) {
                panes[pane.id] = pane;
            });
            tabs.forEach(function (tab) {
                tab.addEventListener('click', function () {
                    tabs.forEach(function (t) {
                        t.classList.toggle('is-active', t === tab);
                        t.setAttribute('aria-selected', t === tab ? 'true' : 'false');
                    });
                    Object.keys(panes).forEach(function (id) {
                        var active = id === 'tpane-' + tab.dataset.tab;
                        panes[id].hidden = !active;
                        panes[id].classList.toggle('is-active', active);
                    });
                });
            });
        }
    });
})();
