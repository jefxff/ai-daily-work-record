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

        // 「展开全部 / 收起」：默认每来源只展示前 5 条，点击后首页内全量展开
        // （用户反馈：不要跳列表页/工作台，展开动作留在首页完成）
        document.querySelectorAll('.home-rows__toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var pane = btn.closest('.home-tpane');
                if (!pane) { return; }
                var expanded = btn.getAttribute('aria-expanded') === 'true';
                pane.querySelectorAll('.home-rows__item.is-extra').forEach(function (li) {
                    li.hidden = expanded;
                });
                btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
                var chevron = btn.querySelector('.home-rows__chevron');
                // 文案重建：保留条数（按钮 data-count 由模板条数推断）
                var n = pane.querySelectorAll('.home-rows__item').length;
                btn.childNodes[0].nodeValue = expanded
                    ? '展开全部 ' + n + ' 条'
                    : '收起';
                if (chevron) { chevron.textContent = expanded ? '▾' : '▴'; }
            });
        });
    });
})();
