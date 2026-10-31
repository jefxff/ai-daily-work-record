/* ==========================================================================
   列表页通用表格工具（渐进增强，不改模板结构）
   --------------------------------------------------------------------------
   1) 列设置：按页面记忆显隐（localStorage），操作列 / 展开列不可隐藏；
   2) 无障碍：为数据表补 caption、为可横向滚动的表格容器补 region 焦点入口；
   3) 操作列收纳：桌面端当动作 ≥4 个时，前 2 个外露、其余收进「更多」。
   全部依赖页面已有结构（th / td[data-label] / .table-responsive / .page-actions），
   未命中结构时静默退出，不影响任何页面。
   ========================================================================== */
(function () {
    'use strict';

    var STORE_PREFIX = 'adw-cols:';

    function ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    function pageKey() {
        return STORE_PREFIX + window.location.pathname;
    }

    function readHidden() {
        try {
            var raw = localStorage.getItem(pageKey());
            var arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr : [];
        } catch (e) {
            return [];
        }
    }

    function saveHidden(arr) {
        try { localStorage.setItem(pageKey(), JSON.stringify(arr)); } catch (e) {}
    }

    /* ---------- 1. 表头列信息 ---------- */
    function columnInfo(table) {
        var headRow = table.querySelector('thead tr');
        if (!headRow) return [];
        var ths = Array.prototype.slice.call(headRow.children).filter(function (el) {
            return el.tagName === 'TH';
        });
        var firstRow = table.querySelector('tbody tr:not(.row-detail)');
        var tds = firstRow ? firstRow.children : [];
        var cols = [];
        ths.forEach(function (th, i) {
            var cls = ' ' + (th.className || '') + ' ';
            var name = '';
            var td = tds[i];
            if (td && td.hasAttribute && td.hasAttribute('data-label')) {
                name = td.getAttribute('data-label').trim();
            }
            if (!name) name = (th.textContent || '').trim().replace(/\s+/g, ' ');
            var fixed = cls.indexOf(' col-expand ') > -1 || cls.indexOf(' col-actions ') > -1 ||
                name === '操作' || !name;
            cols.push({ index: i + 1, name: name || ('第 ' + (i + 1) + ' 列'), fixed: fixed });
        });
        return cols;
    }

    function applyHidden(table, cols, hiddenNames) {
        cols.forEach(function (col) {
            var hide = !col.fixed && hiddenNames.indexOf(col.name) > -1;
            var th = table.querySelector('thead tr > th:nth-child(' + col.index + ')');
            if (th) th.classList.toggle('col-hidden', hide);
            var cells = table.querySelectorAll(
                'tbody tr:not(.row-detail) > td:nth-child(' + col.index + ')');
            Array.prototype.forEach.call(cells, function (td) {
                td.classList.toggle('col-hidden', hide);
            });
        });
    }

    /* ---------- 2. 列设置面板 ---------- */
    function initColumnSettings() {
        /* 原先限定在 .table-container 内，导致「证照管理 / 试样台账」这类
           用 .arc-page 自带容器包裹表格的页面拿不到按钮（用户反馈「列设置没看到」）。
           改为全页查找，仍以「全页只有一张列表表格」作为唯一性前提。 */
        var tables = document.querySelectorAll(
            'table.table--cards, table.table-sticky');
        if (tables.length !== 1) return;          // 多表页（搜索/看板）不提供列设置，避免歧义
        var table = tables[0];
        var cols = columnInfo(table);
        var optional = cols.filter(function (c) { return !c.fixed; });
        if (!optional.length) return;

        var hidden = readHidden();
        applyHidden(table, cols, hidden);

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-transparent col-settings-toggle';
        btn.textContent = '列设置';
        btn.setAttribute('aria-expanded', 'false');
        btn.setAttribute('aria-controls', 'colSettingsPanel');

        /* 插入位置：
           1) 模板用 data-col-settings-anchor 声明锚点（重置/导出 Excel/状态按钮等），
              按 data-col-settings-position="before|after" 插到它前/后——各页操作行
              布局不同，精确落位只能由模板自己声明；
           2) 没有锚点时，挂到页面自己的操作行：base.html 顶部有一个空的
              .page-actions（page_actions/top_nav_actions 挂点），而档案柜/借阅记录/
              证照管理等页在内容里另有自己的 .page-actions——早先用 querySelector
              命中的总是前者，列设置因此孤零零悬在页首。挑最后一个非空容器兜底。 */
        var anchor = document.querySelector('[data-col-settings-anchor]');
        if (anchor) {
            anchor.insertAdjacentElement(
                anchor.getAttribute('data-col-settings-position') === 'before'
                    ? 'beforebegin' : 'afterend', btn);
        } else {
            var candidates = Array.prototype.filter.call(
                document.querySelectorAll('.page-actions'),
                function (el) { return el.children.length > 0; });
            var target = candidates.length
                ? candidates[candidates.length - 1]
                : document.querySelector('.page-actions');
            if (!target) return;
            target.appendChild(btn);
        }

        var panel = document.createElement('div');
        panel.className = 'col-settings-panel';
        panel.id = 'colSettingsPanel';
        panel.hidden = true;
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-label', '显示列设置');

        var head = document.createElement('div');
        head.className = 'col-settings-head';
        head.innerHTML = '<span>显示列</span>';
        var reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'col-settings-reset';
        reset.textContent = '恢复默认';
        head.appendChild(reset);
        panel.appendChild(head);

        var list = document.createElement('div');
        list.className = 'col-settings-list';
        panel.appendChild(list);
        document.body.appendChild(panel);

        var boxes = [];
        optional.forEach(function (col) {
            var label = document.createElement('label');
            label.className = 'col-settings-item';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = hidden.indexOf(col.name) === -1;
            cb.value = col.name;
            label.appendChild(cb);
            label.appendChild(document.createTextNode(col.name));
            list.appendChild(label);
            boxes.push(cb);
            cb.addEventListener('change', function () {
                var visibleCount = optional.filter(function (c) {
                    return hidden.indexOf(c.name) === -1;
                }).length;
                if (!cb.checked && visibleCount <= 1) {
                    cb.checked = true;
                    if (window.showToast) window.showToast('至少保留一列', 'info');
                    return;
                }
                hidden = hidden.filter(function (n) { return n !== cb.value; });
                if (!cb.checked) hidden.push(cb.value);
                saveHidden(hidden);
                applyHidden(table, cols, hidden);
            });
        });

        reset.addEventListener('click', function () {
            hidden = [];
            saveHidden(hidden);
            applyHidden(table, cols, hidden);
            boxes.forEach(function (cb) { cb.checked = true; });
        });

        function setOpen(open) {
            panel.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) {
                var r = btn.getBoundingClientRect();
                var width = Math.min(260, window.innerWidth - 24);
                panel.style.width = width + 'px';
                panel.style.top = (r.bottom + 6) + 'px';
                panel.style.left = Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12)) + 'px';
            }
        }

        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            setOpen(panel.hidden);
        });
        panel.addEventListener('click', function (e) { e.stopPropagation(); });
        document.addEventListener('click', function () { setOpen(false); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !panel.hidden) {
                setOpen(false);
                btn.focus();
            }
        });
    }

    /* ---------- 3. 无障碍：caption + 横滚容器 ---------- */
    function initA11y() {
        var tables = document.querySelectorAll('.table-container table, .table-responsive table');
        Array.prototype.forEach.call(tables, function (table) {
            var title = '';
            var h2 = document.querySelector('.page-header h2');
            if (h2) title = (h2.textContent || '').trim().replace(/\s+/g, ' ');
            if (!title) title = (document.title || '').split('-')[0].trim();

            if (!table.querySelector('caption')) {
                var info = document.querySelector('.pagination-info > span');
                var cap = document.createElement('caption');
                cap.className = 'visually-hidden';
                cap.textContent = (title || '数据列表') +
                    (info ? '，' + (info.textContent || '').trim() : '');
                table.insertBefore(cap, table.firstChild);
            }

            // 只有确实可能横向滚动的宽表（table-sticky）才进入 Tab 序列
            if (table.classList.contains('table-sticky')) {
                var wrap = table.closest('.table-responsive');
                if (wrap && !wrap.hasAttribute('role')) {
                    wrap.setAttribute('role', 'region');
                    wrap.setAttribute('tabindex', '0');
                    wrap.setAttribute('aria-label', (title || '数据列表') + '，可横向滚动');
                }
            }
        });
    }

    /* ---------- 4. 操作列「更多」收纳（桌面端 ≥4 个动作） ---------- */
    function initActMore() {
        if (window.innerWidth <= 640) return;     // 卡片模式操作栏需要平铺，不收纳
        var cells = document.querySelectorAll('td[data-label="操作"]');
        Array.prototype.forEach.call(cells, function (td) {
            if (td.querySelector('.act-more')) return;
            var box = (td.firstElementChild && td.firstElementChild.tagName === 'DIV')
                ? td.firstElementChild : td;
            var items = Array.prototype.filter.call(box.children, function (el) {
                return el.tagName === 'FORM' || el.tagName === 'A' || el.tagName === 'BUTTON';
            });
            if (items.length < 4) return;

            var details = document.createElement('details');
            details.className = 'act-more';
            var summary = document.createElement('summary');
            summary.className = 'btn-transparent btn-sm act-more__toggle';
            summary.textContent = '更多';
            summary.setAttribute('aria-label', '更多操作');
            details.appendChild(summary);

            var menu = document.createElement('div');
            menu.className = 'act-more__menu';
            details.appendChild(menu);
            items.slice(2).forEach(function (el) { menu.appendChild(el); });
            box.appendChild(details);

            // 表格容器有 overflow，绝对定位的菜单会被裁切：展开时改为 fixed 定位
            details.addEventListener('toggle', function () {
                if (!details.open) return;
                var r = summary.getBoundingClientRect();
                var w = Math.max(132, menu.offsetWidth);
                var left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
                menu.style.position = 'fixed';
                menu.style.width = w + 'px';
                menu.style.left = left + 'px';
                menu.style.right = 'auto';
                var below = window.innerHeight - r.bottom - 8;
                var need = menu.offsetHeight + 6;
                if (below < need && r.top > need) {
                    menu.style.top = 'auto';
                    menu.style.bottom = (window.innerHeight - r.top + 4) + 'px';
                } else {
                    menu.style.bottom = 'auto';
                    menu.style.top = (r.bottom + 4) + 'px';
                }
            });
        });

        document.addEventListener('click', function (e) {
            Array.prototype.forEach.call(document.querySelectorAll('.act-more[open]'), function (d) {
                if (!d.contains(e.target)) d.removeAttribute('open');
            });
        });
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            Array.prototype.forEach.call(document.querySelectorAll('.act-more[open]'), function (d) {
                d.removeAttribute('open');
            });
        });
        // 菜单为 fixed 定位，滚动后与按钮脱位，滚动即收起
        window.addEventListener('scroll', function () {
            Array.prototype.forEach.call(document.querySelectorAll('.act-more[open]'), function (d) {
                d.removeAttribute('open');
            });
        }, true);
    }

    ready(function () {
        initA11y();
        initColumnSettings();
        initActMore();
    });
})();
