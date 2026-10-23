/* 知识库文档详情页脚本（由 document_detail.html 内联脚本抽离，行为保持不变）。 */
/* ═══ 脚本一：文档可见性切换（全员可见 ↔ 仅自己可见）═══
   保留既有实现：DOM 契约（kbVisBadge/kbVisToggle/kbVisBadgeText/kbVisToggleText）
   与文案（设为全员可见/收回为仅自己可见）被测试依赖，勿改结构。 */
(function () {
    var toggle = document.getElementById('kbVisToggle');
    if (!toggle) return;
    var csrf = toggle.getAttribute('data-csrf') || '';
    var badge = document.getElementById('kbVisBadge');
    var badgeText = document.getElementById('kbVisBadgeText');
    var toggleText = document.getElementById('kbVisToggleText');

    var ICON_LOCK = '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>';
    var ICON_PEOPLE = '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>';

    function renderState(vis, label) {
        var isPrivate = vis === 'private';
        toggle.dataset.current = vis;
        toggleText.textContent = isPrivate ? '设为全员可见' : '收回为仅自己可见';
        toggle.querySelector('svg').innerHTML = isPrivate ? ICON_PEOPLE : ICON_LOCK;
        if (badge) {
            badge.classList.toggle('warning', isPrivate);
            badge.classList.toggle('info', !isPrivate);
            badge.title = isPrivate ? '只有你能看到这篇文档' : '所有有知识库权限的同事都能看到这篇文档';
            var svg = badge.querySelector('svg');
            if (svg) svg.innerHTML = isPrivate ? ICON_LOCK : ICON_PEOPLE;
        }
        if (badgeText && label) badgeText.textContent = label;
    }

    toggle.addEventListener('click', function () {
        var next = toggle.dataset.current === 'private' ? 'public' : 'private';
        var body = new URLSearchParams();
        body.set('visibility', next);
        toggle.disabled = true;
        fetch(toggle.dataset.url, {
            method: 'POST',
            // 必须显式声明表单类型：fetch 对字符串 body 默认发 text/plain，
            // Django 的 request.POST 不解析 text/plain，会拿到空值报 400
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-CSRFToken': getCookie('csrftoken') || csrf,
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: body.toString(),
            keepalive: true
        })
        .then(function (resp) { return resp.json().then(function (data) { return { ok: resp.ok, data: data }; }); })
        .then(function (result) {
            if (result.ok && result.data.ok) {
                renderState(result.data.visibility, result.data.visibility_label);
                if (window.showToast) window.showToast(result.data.message, 'success');
            } else {
                if (window.showToast) window.showToast((result.data && result.data.message) || '操作失败，请重试', 'error');
            }
        })
        .catch(function () {
            if (window.showToast) window.showToast('网络异常，请重试', 'error');
        })
        .finally(function () { toggle.disabled = false; });
    });
})();


/* ═══ 脚本二：书房阅读器 —— 视图切换 / 只读大纲 / 思维导图 ═══
   大纲与导图共用同一份懒加载树数据（api_doc_tree，GET 只读零写入）。
   导图用 canvas measureText 精确测宽（中文不再按 13px 估算导致裁切/溢出），
   右向分层布局 + 贝塞尔连线 + 折叠徽章 + 缩放 + 错峰入场；
   舞台高度按内容收紧（小图不再是一片大海），内容更矮时垂直居中。 */
(function () {
    var tabs = document.querySelectorAll('.kb-view-tab');
    var readView = document.getElementById('kbReadView');
    var outlineView = document.getElementById('kbOutlineView');
    var mindmapView = document.getElementById('kbMindmapView');
    var mmCanvas = document.getElementById('kbMindmapCanvas');
    if (!tabs.length || !readView || !outlineView || !mindmapView || !mmCanvas) return;

    var apiUrl = mmCanvas.getAttribute('data-tree-url') || '';
    var titleEl = document.querySelector('.kb-doc-title-serif');
    var docTitle = titleEl ? titleEl.textContent.trim() : '文档';
    var treePromise = null;
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function getTree() {
        if (!treePromise) {
            treePromise = fetch(apiUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (!data.ok) throw new Error('load failed');
                    return { tree: data.tree || [], editor_mode: data.editor_mode || 'markdown' };
                })
                .catch(function (err) { treePromise = null; throw err; });
        }
        return treePromise;
    }

    /* 导图树：结构化文档（outline 模式）直接用节点树；
       markdown 文档的 md_to_tree 是扁平列表（标题和段落同级），这里按标题
       层级分组成树，**所有非标题节点（段落 / 列表项 / todo / 引用 / 代码块）
       都挂到它前面最近的那个祖先标题下当叶子子节点**——确保导图覆盖全文。
       divider 节点不进导图（纯视觉分隔，没有实质内容）。 */
    function buildMapTree(data) {
        var nodes = (data && data.tree) || [];
        if (data && data.editor_mode === 'outline') return nodes;
        var roots = [], stack = [];  // stack: [{ level, node }]
        var lastHeadingNode = null;  // 最近一次见到的标题节点（供正文挂载）
        nodes.forEach(function (n) {
            if (n.type === 'divider') return;
            if (n.type === 'heading') {
                var level = n.heading_level || 1;
                while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
                var item = {
                    text: n.text || '', type: 'heading', heading_level: level,
                    children: [], collapsed: false
                };
                if (stack.length) stack[stack.length - 1].node.children.push(item);
                else roots.push(item);
                stack.push({ level: level, node: item });
                lastHeadingNode = item;
            } else {
                // 正文节点：优先挂到最近的标题下；没有前置标题时挂根层
                var leaf = {
                    text: n.text || '', type: n.type || 'text',
                    done: !!n.done, due: n.due || '',
                    children: [], collapsed: false
                };
                // 代码块没有 text 正文存在 note 里，导图里显示 info string + 行数
                if (n.type === 'code') {
                    var lines = (n.note || '').split('\n').length;
                    leaf.text = (n.text || '代码块') + '（' + lines + ' 行）';
                }
                // ref：优先显示标签
                if (n.type === 'ref') leaf.text = n.ref_label || n.text || '';

                if (lastHeadingNode) {
                    lastHeadingNode.children.push(leaf);
                } else {
                    roots.push(leaf);
                }
            }
        });
        return roots;
    }

    /* ═══════════ 只读大纲（参照参考图：大号笔记 + #标签徽章） ═══════════ */
    var CARET = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><polyline points="9 18 15 12 9 6"/></svg>';
    var STROKE_ATTRS = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';
    var TYPE_ICONS = {
        heading: '<svg class="icon" viewBox="0 0 24 24" ' + STROKE_ATTRS + '><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/></svg>',
        quote: '<svg class="icon" viewBox="0 0 24 24" ' + STROKE_ATTRS + '><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        code: '<svg class="icon" viewBox="0 0 24 24" ' + STROKE_ATTRS + '><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
        divider: '<svg class="icon" viewBox="0 0 24 24" ' + STROKE_ATTRS + '><line x1="5" y1="12" x2="19" y2="12"/></svg>',
        ref: '<svg class="icon" viewBox="0 0 24 24" ' + STROKE_ATTRS + '><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>'
    };
    /* #标签 → 徽章：只认「行首或空白之后」的 #，避免误伤 C# 之类写法 */
    var TAG_RE = /(^|\s)(#[^\s#]+)/g;

    function appendTextWithTags(el, text) {
        var s = text || '';
        var last = 0, m;
        TAG_RE.lastIndex = 0;
        while ((m = TAG_RE.exec(s))) {
            if (m.index + m[1].length > last) {
                el.appendChild(document.createTextNode(s.slice(last, m.index + m[1].length)));
            }
            var chip = document.createElement('span');
            chip.className = 'kb-tag-chip';
            chip.textContent = m[2];
            el.appendChild(chip);
            last = TAG_RE.lastIndex;
        }
        if (last < s.length) el.appendChild(document.createTextNode(s.slice(last)));
    }

    function isOverdue(due) {
        if (!due) return false;
        var d = new Date(due + 'T00:00:00');
        if (isNaN(d.getTime())) return false;
        var today = new Date(); today.setHours(0, 0, 0, 0);
        return d < today;
    }

    function buildOutlineList(nodes) {
        var ul = document.createElement('ul');
        nodes.forEach(function (node) {
            var type = node.type || 'text';
            var li = document.createElement('li');
            var row = document.createElement('div');
            row.className = 'kb-outline-row kb-node-' + type;
            var level = parseInt(node.heading_level, 10);
            if (level >= 1 && level <= 3) row.classList.add('is-h' + level);
            if (type === 'todo' && node.done) row.classList.add('is-done');

            if (node.children && node.children.length) {
                var caret = document.createElement('button');
                caret.type = 'button';
                caret.className = 'kb-caret' + (node.collapsed ? '' : ' is-open');
                caret.setAttribute('aria-label', '折叠或展开子节点');
                caret.setAttribute('aria-expanded', node.collapsed ? 'false' : 'true');
                caret.innerHTML = CARET;
                caret.addEventListener('click', function () {
                    var open = caret.classList.toggle('is-open');
                    caret.setAttribute('aria-expanded', open ? 'true' : 'false');
                    var childUl = li.querySelector(':scope > ul');
                    if (childUl) childUl.hidden = !open;
                });
                row.appendChild(caret);
            } else {
                var spacer = document.createElement('span');
                spacer.className = 'kb-caret-spacer';
                row.appendChild(spacer);
            }

            if (type === 'todo') {
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.className = 'kb-outline-todo';
                cb.checked = !!node.done;
                cb.disabled = true;   /* 只读视图：不写库 */
                cb.setAttribute('aria-label', '待办完成状态');
                row.appendChild(cb);
            } else if (TYPE_ICONS[type]) {
                var icon = document.createElement('span');
                icon.className = 'kb-node-type';
                icon.innerHTML = TYPE_ICONS[type];
                row.appendChild(icon);
            }

            var text = document.createElement('span');
            text.className = 'kb-outline-text';
            if (type === 'code') {
                var lines = (node.note || '').split('\n').length;
                text.textContent = (node.text || '代码块') + (node.note ? '（' + lines + ' 行）' : '');
            } else if (type === 'divider') {
                text.textContent = '— — —';
            } else if (type === 'ref') {
                text.textContent = node.ref_label || node.text || '';
            } else {
                appendTextWithTags(text, node.text || '');
            }
            row.appendChild(text);

            if (type === 'todo' && node.due) {
                var due = document.createElement('span');
                due.className = 'kb-outline-due' + (isOverdue(node.due) ? ' is-overdue' : '');
                due.textContent = String(node.due).slice(5);
                due.title = '到期日 ' + node.due;
                row.appendChild(due);
            }
            li.appendChild(row);

            if (node.children && node.children.length) {
                var childUl = buildOutlineList(node.children);
                if (node.collapsed) childUl.hidden = true;
                li.appendChild(childUl);
            }
            ul.appendChild(li);
        });
        return ul;
    }

    /* ═══════════ 思维导图（参照参考图：根胶囊 + 主色一级 + 折叠徽章） ═══════════ */
    var NS = 'http://www.w3.org/2000/svg';
    var MM = {
        ROOT_H: 46, L1_H: 36, NODE_H: 32, GAP: 9,
        LEVEL_W: 232, PAD: 15, CLIP: 220,
        STAGE_PAD: 48,       /* .kb-mindmap 上下 padding 合计（--sp-6 × 2） */
        STAGE_MIN: 260,      /* 舞台贴合内容时的最小高度 */
        SCALE_MAX: 2.2       /* 小图自动放大的上限（再大就失真/稀疏） */
    };
    var measureCtx = document.createElement('canvas').getContext('2d');
    var FONT_NODE = '13px sans-serif';
    var FONT_ROOT = '700 15px "Noto Serif SC","Songti SC",SimSun,serif';
    var mmState = { svg: null, w: 0, h: 0, autoScale: 1, zoom: 1 };

    function measure(s, font) { measureCtx.font = font; return measureCtx.measureText(s).width; }

    function clipText(s, maxW, font) {
        s = (s || '').replace(/\s+/g, ' ').trim() || '（空）';
        if (measure(s, font) <= maxW) return s;
        var out = s;
        while (out.length > 1 && measure(out + '…', font) > maxW) out = out.slice(0, -1);
        return out + '…';
    }

    function renderMindmap(nodes) {
        mmCanvas.innerHTML = '';
        mmCanvas.style.minHeight = '';
        mmState.svg = null;
        if (!nodes.length) {
            var empty = document.createElement('div');
            empty.className = 'kb-mm-empty';
            empty.textContent = '本文档暂无标题结构，切换到「阅读」查看全文；正文中加入 # 标题即可生成导图';
            mmCanvas.appendChild(empty);
            mmCanvas.style.minHeight = '180px';   /* 空态紧凑舞台，不再是一整片点阵 */
            return;
        }
        var root = { text: docTitle, type: 'root', children: nodes, collapsed: false };
        var cursorY = 0;
        (function place(node, depth, parent) {
            node._depth = depth;
            node._parent = parent;
            var font = depth === 0 ? FONT_ROOT : FONT_NODE;
            node._label = clipText(node.text, MM.CLIP, font);
            node._w = Math.max(72, measure(node._label, font) + MM.PAD * 2);
            node._h = depth === 0 ? MM.ROOT_H : (depth === 1 ? MM.L1_H : MM.NODE_H);
            node._kidsVisible = !node.collapsed && node.children && node.children.length > 0;
            if (node._kidsVisible) {
                node.children.forEach(function (c) { place(c, depth + 1, node); });
                node._y = (node.children[0]._y + node.children[node.children.length - 1]._y) / 2;
            } else {
                node._y = cursorY + node._h / 2;
                cursorY += node._h + MM.GAP;
            }
            node._x = depth * MM.LEVEL_W;
        })(root, 0, null);

        /* 画布尺寸 = 全部节点盒子的并集。
           历史事故：旧实现按 _subH（子节点高度的最大值）推高度，多节点时高度
           被算成 47px，根与后续节点全被 viewBox 裁掉——用户看到「内容没有
           正确显示，只剩一个 pill 和大片空白」（2026-10-22 截图）。 */
        var maxX = 0, minTop = Infinity, maxBottom = -Infinity;
        (function bounds(n) {
            maxX = Math.max(maxX, n._x + n._w);
            minTop = Math.min(minTop, n._y - n._h / 2);
            maxBottom = Math.max(maxBottom, n._y + n._h / 2);
            if (n._kidsVisible) n.children.forEach(bounds);
        })(root);
        var offsetY = MM.PAD - minTop;   /* 父盒可能探出首子节点上方，整体下移补回 */
        (function shift(n) { n._y += offsetY; if (n._kidsVisible) n.children.forEach(shift); })(root);
        var width = maxX + MM.PAD;
        var height = (maxBottom - minTop) + MM.PAD * 2;

        var svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', docTitle + ' 思维导图');
        mmState.svg = svg; mmState.w = width; mmState.h = height;

        function el(name, attrs, parent) {
            var e = document.createElementNS(NS, name);
            for (var k in attrs) e.setAttribute(k, attrs[k]);
            (parent || svg).appendChild(e);
            return e;
        }

        /* 先画全部连线（压在节点下方），再画节点 */
        (function drawEdges(node) {
            if (!node._kidsVisible) return;
            node.children.forEach(function (c) {
                var px = node._x + node._w, x = c._x;
                var bend = Math.max(36, (x - px) * 0.5);
                el('path', {
                    'class': 'kb-mm-edge' + (c._depth === 1 ? ' is-l1' : ''),
                    d: 'M ' + px + ' ' + node._y +
                       ' C ' + (px + bend) + ' ' + node._y +
                       ', ' + (x - bend) + ' ' + c._y + ', ' + x + ' ' + c._y
                });
                drawEdges(c);
            });
        })(root);

        var groups = [];
        (function draw(node) {
            var g = el('g', { 'class': 'kb-mm-node' });
            groups.push(g);
            var x = node._x, yTop = node._y - node._h / 2;
            var boxClass = 'kb-mm-box';
            if (node._depth === 0) boxClass += ' is-root';
            else if (node._depth === 1) boxClass += ' is-l1';
            if (node.type === 'todo') boxClass += ' is-todo';
            var box = el('rect', {
                'class': boxClass, x: x, y: yTop,
                width: node._w, height: node._h, rx: node._depth === 0 ? 12 : 8
            }, g);
            var tip = el('title', {}, g);
            tip.textContent = node.text || '';

            var textClass = 'kb-mm-text';
            if (node._depth === 0) textClass += ' is-root';
            else if (node._depth === 1) textClass += ' is-l1';
            if (node.type === 'todo' && node.done) textClass += ' is-muted';
            if (node.type === 'quote') textClass += ' is-quote';
            el('text', {
                'class': textClass, x: x + MM.PAD, y: node._y + (node._depth === 0 ? 5 : 4)
            }, g).textContent = node._label;

            /* 折叠徽章：显示被折叠的子节点数，点击仅展开本级 */
            if (node.collapsed && node.children && node.children.length) {
                var cx = x + node._w + 13, cy = node._y;
                var badge = el('circle', { 'class': 'kb-mm-collapse', cx: cx, cy: cy, r: 9 }, g);
                badge.addEventListener('click', function (ev) {
                    ev.stopPropagation();
                    node.collapsed = false;
                    renderMindmap(nodes);
                });
                el('text', { 'class': 'kb-mm-collapse-text', x: cx - 4, y: cy + 3.5 }, g)
                    .textContent = String(node.children.length);
            }
            /* 点击框体：有子节点则折叠/展开（根节点除外） */
            if (node._depth > 0 && node.children && node.children.length) {
                box.addEventListener('click', function () {
                    node.collapsed = !node.collapsed;
                    renderMindmap(nodes);
                });
                box.setAttribute('role', 'button');
                box.setAttribute('tabindex', '0');
                box.setAttribute('aria-label', (node.collapsed ? '展开' : '折叠') + '「' + (node.text || '') + '」的子节点');
                box.addEventListener('keydown', function (ev) {
                    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); box.dispatchEvent(new MouseEvent('click')); }
                });
            }

            if (node._kidsVisible) node.children.forEach(draw);
        })(root);

        /* 错峰入场：一次编排，reduced-motion 直接到位 */
        if (reduceMotion) {
            groups.forEach(function (g) { g.classList.add('is-in'); });
        } else {
            groups.forEach(function (g, i) {
                setTimeout(function () { g.classList.add('is-in'); }, Math.min(i * 40, 640));
            });
        }

        /* 舞台贴合 + 自适应缩放：内容矮时收紧舞台（小图不再是一片大海）；
           窄图自动放大铺满舞台宽度（封顶 SCALE_MAX、永不缩小——大图保持
           100%，交还 CSS 56vh 起步 / 76vh 封顶滚动）；垂直富余居中，
           水平居中走 CSS（.kb-mindmap svg margin auto）。 */
        var vh = window.innerHeight || 800;
        var stageMin = Math.max(MM.STAGE_MIN, height + MM.STAGE_PAD + 16);
        if (stageMin < vh * 0.56) mmCanvas.style.minHeight = stageMin + 'px';
        mmState.autoScale = Math.min(
            ((mmCanvas.clientWidth || width) - MM.STAGE_PAD) / width,
            (vh * 0.62 - MM.STAGE_PAD) / height,
            MM.SCALE_MAX
        );
        if (!(mmState.autoScale >= 1)) mmState.autoScale = 1;   /* 隐藏态/NaN 兜底 */
        var slack = stageMin - MM.STAGE_PAD - height * mmState.autoScale;
        if (slack > 0) svg.style.marginTop = Math.round(slack / 2) + 'px';
        mmState.zoom = 1;
        applyZoom();
        mmCanvas.appendChild(svg);
    }

    function applyZoom() {
        var zoomEl = document.getElementById('kbMmZoom');
        var total = (mmState.autoScale || 1) * mmState.zoom;
        if (zoomEl) zoomEl.textContent = Math.round(total * 100) + '%';
        if (!mmState.svg) return;
        mmState.svg.setAttribute('width', mmState.w * total);
        mmState.svg.setAttribute('height', mmState.h * total);
    }

    /* 工具条：展开/折叠全部 + 缩放（树未加载时先拉数据再执行） */
    document.querySelectorAll('[data-mm-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var action = btn.dataset.mmAction;
            if (action === 'zoom-in') { mmState.zoom = Math.min(2, +(mmState.zoom + 0.25).toFixed(2)); applyZoom(); return; }
            if (action === 'zoom-out') { mmState.zoom = Math.max(0.5, +(mmState.zoom - 0.25).toFixed(2)); applyZoom(); return; }
            getTree().then(function (data) {
                var tree = buildMapTree(data);
                (function walk(nodes) {
                    nodes.forEach(function (n) {
                        if (action === 'expand') n.collapsed = false;
                        if (action === 'collapse' && n.children && n.children.length) n.collapsed = true;
                        if (n.children) walk(n.children);
                    });
                })(tree);
                renderMindmap(tree);
            }).catch(function () {
                if (window.showToast) window.showToast('大纲数据加载失败，请重试', 'error');
            });
        });
    });

    /* ── 视图切换 ── */
    tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
            var view = tab.dataset.view;
            tabs.forEach(function (t) {
                var active = t === tab;
                t.classList.toggle('is-active', active);
                t.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            readView.hidden = view !== 'read';
            outlineView.hidden = view !== 'outline';
            mindmapView.hidden = view !== 'mindmap';
            if (view === 'read') return;
            getTree().then(function (data) {
                if (view === 'outline') {
                    outlineView.innerHTML = '';
                    outlineView.appendChild(buildOutlineList(data.tree));
                } else {
                    renderMindmap(buildMapTree(data));
                }
            }).catch(function () {
                if (window.showToast) window.showToast('大纲数据加载失败，请重试', 'error');
            });
        });
    });
})();
