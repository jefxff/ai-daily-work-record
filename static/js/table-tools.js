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

    function readHidden(key) {
        try {
            var raw = localStorage.getItem(key);
            var arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr : [];
        } catch (e) {
            return [];
        }
    }

    function saveHidden(key, arr) {
        try { localStorage.setItem(key, JSON.stringify(arr)); } catch (e) {}
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

    function applyHidden(tables, cols, hiddenNames) {
        cols.forEach(function (col) {
            var hide = !col.fixed && hiddenNames.indexOf(col.name) > -1;
            tables.forEach(function (table) {
                var th = table.querySelector('thead tr > th:nth-child(' + col.index + ')');
                if (th) th.classList.toggle('col-hidden', hide);
                var cells = table.querySelectorAll(
                    'tbody tr:not(.row-detail) > td:nth-child(' + col.index + ')');
                Array.prototype.forEach.call(cells, function (td) {
                    /* 空状态行是 colspan 合并单元格，按 nth-child 命中会把它一起隐藏，
                       整表会出现「一行空白」。跨列单元格不参与列显隐。 */
                    if (td.colSpan > 1) return;
                    td.classList.toggle('col-hidden', hide);
                });
            });
        });
    }

    /* ---------- 2. 列设置面板 ----------
       一个面板 = 一张表。
       单表页：按钮挂模板锚点（没有锚点时落到表格上方的自动工具条）。
       多表页：模板用 [data-col-settings-scope] 圈住「表格 + 它自己的锚点」，
               每张表各拿一个按钮、各自记忆显隐（档案总览：一张台账一个按钮）。
               没圈作用域的多表页（异常检测那 4 张分组表）不生成按钮——
               一个不知道该管哪张表的齿轮比没有更糟。
       ------------------------------------------------------------------ */

    /* 同页可能有多个面板：打开一个就收起其余的，避免两个浮层叠在一起 */
    var panelClosers = [];

    function gearIcon() {
        return '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
    }

    function createColumnSettings(table, anchor, storageKey, serial) {
        var cols = columnInfo(table);
        var optional = cols.filter(function (c) { return !c.fixed; });
        if (!optional.length) return;

        var hidden = readHidden(storageKey);
        applyHidden([table], cols, hidden);

        var panelId = serial ? 'colSettingsPanel-' + serial : 'colSettingsPanel';

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'col-settings-gear';
        btn.setAttribute('aria-expanded', 'false');
        btn.setAttribute('aria-controls', panelId);
        btn.setAttribute('aria-label', '列设置');
        btn.title = '列设置';
        btn.innerHTML = gearIcon();

        /* 插入位置：
           1) 模板用 data-col-settings-anchor 声明锚点，按 data-col-settings-position
              插到它前/后；
           2) 没有锚点时挂在表格正上方右侧的自动工具条里（齿轮图标）——列设置作用于
              「这张表」，放在页面工具栏会被当成又一个页面级动作。 */
        if (anchor) {
            anchor.insertAdjacentElement(
                anchor.getAttribute('data-col-settings-position') === 'before'
                    ? 'beforebegin' : 'afterend', btn);
        } else {
            var host = table.closest('.table-responsive') || table;
            var bar = host.previousElementSibling;
            if (!bar || !bar.classList || !bar.classList.contains('table-tools-bar')) {
                bar = document.createElement('div');
                bar.className = 'table-tools-bar';
                host.parentNode.insertBefore(bar, host);
            }
            bar.appendChild(btn);
        }

        var panel = document.createElement('div');
        panel.className = 'col-settings-panel';
        panel.id = panelId;
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
                saveHidden(storageKey, hidden);
                applyHidden([table], cols, hidden);
            });
        });

        reset.addEventListener('click', function () {
            hidden = [];
            saveHidden(storageKey, hidden);
            applyHidden([table], cols, hidden);
            boxes.forEach(function (cb) { cb.checked = true; });
        });

        function setOpen(open) {
            panel.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (!open) return;
            panelClosers.forEach(function (close) { if (close !== closeSelf) close(); });
            var r = btn.getBoundingClientRect();
            var width = Math.min(260, window.innerWidth - 24);
            panel.style.width = width + 'px';
            panel.style.top = (r.bottom + 6) + 'px';
            panel.style.left = Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12)) + 'px';
        }

        function closeSelf() { setOpen(false); }
        panelClosers.push(closeSelf);

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

    function initColumnSettings() {
        /* 原先限定在 .table-container 内，导致「证照管理 / 试样台账」这类
           用 .arc-page 自带容器包裹表格的页面拿不到按钮（用户反馈「列设置没看到」）。
           改为全页查找。 */
        var tables = Array.prototype.slice.call(document.querySelectorAll(
            'table.table--cards, table.table-sticky'));
        if (!tables.length) return;

        /* 单表页：全页唯一一张列表表格，锚点可以放在页面操作区任意位置 */
        if (tables.length === 1) {
            createColumnSettings(
                tables[0], document.querySelector('[data-col-settings-anchor]'), pageKey(), 0);
            return;
        }

        /* 多表页：只认「显式声明了作用域」的表。
           作用域内再找锚点，保证按钮落在这张表自己的卡片头里；localStorage
           按「页面 + 作用域 key」分开存，各表互不影响。 */
        tables.forEach(function (table, i) {
            var scope = table.closest('[data-col-settings-scope]');
            if (!scope) return;
            var anchor = scope.querySelector('[data-col-settings-anchor]');
            if (!anchor) return;
            var key = pageKey() + '#' + (scope.getAttribute('data-col-settings-key') || i);
            createColumnSettings(table, anchor, key, i);
        });
    }

    /* ---------- 3. 无障碍：caption + 横滚容器 ---------- */
    function initA11y() {
        var tables = document.querySelectorAll('.table-container table, .table-responsive table');
        Array.prototype.forEach.call(tables, function (table) {
            var title = '';
            // 页面主标题 2026-11 起改用 h1（语义层级），此处两种都认
            var heading = document.querySelector('.page-header h1, .page-header h2');
            if (heading) title = (heading.textContent || '').trim().replace(/\s+/g, ' ');
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

        /* 长文本被 CSS 截断（text-overflow: ellipsis）后，内容就"看不见"了。
           现在把截断铺到所有列表表格，但只对「超过 24 字、纯文本、没有嵌套块」
           的单元格生效：
           - 含链接/按钮 → 跳过，避免把「操作」列一串按钮文案拼成 tooltip；
           - 含 ul/ol/dl/details/table → 跳过，时间线、清单类单元格必须能换行；
           - 短文案不处理，小表格的换行保持原样。 */
        var clipTargets = document.querySelectorAll(
            '.table-container tbody td, .table-sticky tbody td'
        );
        Array.prototype.forEach.call(clipTargets, function (td) {
            if (td.title || td.classList.contains('cell-wrap')) return;
            if (td.getAttribute('data-label') === '操作') return;
            if (td.querySelector('a, button, form, input, select, textarea, ul, ol, dl, table, details')) return;
            var text = (td.textContent || '').replace(/\s+/g, ' ').trim();
            if (text.length <= 24) return;
            td.title = text;
            /* 同时落两个类名：
               .is-clipped   历史样式（22em 截断 + 卡片模式还原），保持既有页面的观感；
               .col-truncate 全局台账规范（250px 截断），供新页面按统一口径书写。
               两者声明基本重合，重复加不改变渲染结果。 */
            td.classList.add('is-clipped', 'col-truncate');
        });
    }

    /* ---------- 3.5 表头吸附：全站数据表统一启用 ----------
       57 个模板里有 41 个没给 table 加 .table-sticky，滚动后表头跟着走、
       列语义丢失。这里在运行时统一补齐，模板不再需要逐页改类名：
       - 只处理「有 thead、≥2 列、不在弹窗/菜单里」的数据表；
       - 缺 .table-responsive 容器时补一层，否则 sticky 没有滚动上下文；
       - 用 .table-head-sticky 而不是 .table-sticky，避免顺带冻结首列。 */
    function initStickyHeads() {
        var skipSelector = '.form-modal, .modal, .confirm-modal, .nav-notify-menu, .nav-user-menu, [role="dialog"]';
        var tables = document.querySelectorAll('table');
        Array.prototype.forEach.call(tables, function (table) {
            if (table.classList.contains('table-head-sticky')) return;
            if (table.closest(skipSelector)) return;
            var thead = table.tHead;
            if (!thead || !thead.rows.length) return;
            if (thead.rows[0].cells.length < 2) return;

            var wrap = table.parentElement;
            if (!wrap || !wrap.classList.contains('table-responsive')) {
                wrap = document.createElement('div');
                wrap.className = 'table-responsive';
                table.parentNode.insertBefore(wrap, table);
                wrap.appendChild(table);
            }
            table.classList.add('table-head-sticky');
        });
    }

    /* ---------- 3.6 数值列右对齐 ----------
       金额与数量原先左对齐，千分位宽度不一时无法纵向比大小。
       两条识别路径，模板与脚本各管一半：
       1) 模板显式在 <th> 上写 .col-num —— 语义最准，脚本负责「透传」到同列 <td>，
          否则表头右对齐、表体居中，比不改更难看；
       2) 没写类名时按表头文字兜底识别（50 多个模板不必逐个改）。
       两条路径都落到 .col-num / .is-num，样式只在 main.css 里定义一次。 */
    var NUMERIC_KEYS = ['金额', '核减', '报价', '保费', '单价', '价格', '原价', '核后',
        '数量', '合计', '小计', '费用', '工资', '折扣', '涉案', '额度', '税额',
        '利息', '余额', '成本'];

    function isNumericLabel(text) {
        if (!text) return false;
        for (var i = 0; i < NUMERIC_KEYS.length; i++) {
            if (text.indexOf(NUMERIC_KEYS[i]) !== -1) return true;
        }
        return false;
    }

    function initNumericColumns() {
        var tables = document.querySelectorAll('table');
        Array.prototype.forEach.call(tables, function (table) {
            if (table.classList.contains('is-num-ready')) return;
            var headRow = table.tHead && table.tHead.rows[0];
            if (!headRow) return;
            var indexes = [];
            Array.prototype.forEach.call(headRow.cells, function (th, i) {
                var label = (th.textContent || '').trim();
                if (!th.classList.contains('col-num') && !isNumericLabel(label)) return;
                indexes.push(i);
                th.classList.add('col-num', 'is-num');
            });
            if (!indexes.length) return;
            Array.prototype.forEach.call(table.tBodies, function (tbody) {
                Array.prototype.forEach.call(tbody.rows, function (row) {
                    indexes.forEach(function (i) {
                        var cell = row.cells[i];
                        if (cell) cell.classList.add('col-num', 'is-num');
                    });
                });
            });
            table.classList.add('is-num-ready');
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

            // 展开时把菜单挂到 body 上，再用 fixed 定位到按钮下方。
            // 只改 position:fixed 不够：菜单仍留在 td.col-actions 这个 sticky
            // 层叠上下文里，后续行的同层操作列会把它盖住（点开只见一条白边）；
            // 表格容器的 overflow 也可能裁掉它。挂到 body 后两者都不再影响菜单。
            details._actMenu = menu;
            details.addEventListener('toggle', function () {
                if (!details.open) {
                    if (menu.parentNode === document.body) {
                        details.appendChild(menu);
                        menu.removeAttribute('style');
                    }
                    return;
                }
                if (menu.parentNode !== document.body) document.body.appendChild(menu);
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
                var menu = d._actMenu;
                // 菜单已挂到 body，contains 检查要连它一起算，否则点菜单里的
                // 「续保 / 列为一次性」会被当成点外部而先收起，动作丢失。
                if (!d.contains(e.target) && !(menu && menu.contains(e.target))) {
                    d.removeAttribute('open');
                }
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
        initStickyHeads();
        initNumericColumns();
        initA11y();
        initColumnSettings();
        initActMore();
    });
})();
