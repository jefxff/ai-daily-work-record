/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-knowledge_base-outline_editor" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_knowledge_base_outline_editor = (function () {
  var el = document.getElementById('pageData-knowledge_base-outline_editor');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

(function() {
    'use strict';
    // ===================================================== 常量与状态
    var DOC_ID = D_knowledge_base_outline_editor.vars['v1'];
    var TREE_URL = D_knowledge_base_outline_editor.urls['u1'];
    var CSRF = document.querySelector('meta[name="csrf-token"]').content;
    var MAX_NODES = 1000, MAX_DEPTH = 20;
    var DATE_RE = /^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$/;
    var TODO_MARK_RE = /^\[( |x|X)\]\s+(.*)$/;
    var LS_KEY = 'kbOutlineDraft_' + DOC_ID;

    var seed = JSON.parse(document.getElementById('kb-editor-data').textContent);
    var state = {
        tree: seed.tree && seed.tree.length ? seed.tree : [emptyNode()],
        version: seed.version,
        dirty: false, saving: false, savingAgain: false, composing: false,
        saveTimer: null, retryTimer: null, draftTimer: null,
        pendingFocus: null, conflictVersion: null
    };
    var rowsEl = document.getElementById('kbEdRows');
    var statusEl = document.getElementById('kbEdStatus');
    var statusText = document.getElementById('kbEdStatusText');
    var conflictEl = document.getElementById('kbEdConflict');
    var draftEl = document.getElementById('kbEdDraft');

    // ===================================================== 树操作工具
    function emptyNode(text) {
        return { uid: genUid(), type: 'text', text: text || '', done: false,
                 note: '', due: null, heading_level: null,
                 collapsed: false, children: [] };
    }
    function genUid() {
        var s = '';
        for (var i = 0; i < 12; i++) s += Math.floor(Math.random() * 16).toString(16);
        return s;
    }
    function findNode(uid, nodes, parent, siblings) {
        nodes = nodes || state.tree;
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].uid === uid) {
                return { node: nodes[i], parentList: siblings || nodes,
                         parent: parent || null, index: i };
            }
            var hit = findNode(uid, nodes[i].children, nodes[i], nodes[i].children);
            if (hit) return hit;
        }
        return null;
    }
    function countAll(nodes) {
        var n = 0;
        (nodes || []).forEach(function(x) { n += 1 + countAll(x.children); });
        return n;
    }
    function depthAll(nodes, base) {
        var d = base || 0, max = d;
        (nodes || []).forEach(function(x) {
            max = Math.max(max, depthAll(x.children, d + 1));
        });
        return max;
    }
    function flattenVisible(nodes, depth, out) {
        out = out || [];
        (nodes || []).forEach(function(n) {
            out.push({ node: n, depth: depth });
            if (!n.collapsed) flattenVisible(n.children, depth + 1, out);
        });
        return out;
    }

    // ===================================================== 渲染
    var SVG_CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><polyline points="6 9 12 15 18 9"/></svg>';

    function buildRow(entry) {
        var n = entry.node;
        var row = document.createElement('div');
        row.className = 'kb-ed-row is-' + n.type;
        if (n.type === 'todo' && n.done) row.classList.add('is-done');
        row.dataset.uid = n.uid;
        row.style.setProperty('--kb-ind', entry.depth);

        var hasKids = n.children && n.children.length > 0;
        if (hasKids) {
            var fold = document.createElement('button');
            fold.type = 'button';
            fold.className = 'kb-ed-fold' + (n.collapsed ? ' is-collapsed' : '');
            fold.setAttribute('aria-label', n.collapsed ? '展开子节点' : '折叠子节点');
            fold.setAttribute('aria-expanded', n.collapsed ? 'false' : 'true');
            fold.innerHTML = SVG_CHEVRON;
            row.appendChild(fold);
        } else {
            row.appendChild(Object.assign(document.createElement('span'), { className: 'kb-ed-fold-spacer' }));
        }

        if (n.type === 'todo') {
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'kb-ed-check';
            cb.checked = !!n.done;
            cb.setAttribute('aria-label', '标记完成');
            row.appendChild(cb);
        }

        var text = document.createElement('div');
        if (n.type === 'divider') {
            text.className = 'kb-ed-divider';
            text.setAttribute('aria-hidden', 'true');
            row.appendChild(text);
            return row;
        }
        text.className = 'kb-ed-text';
        text.contentEditable = 'true';
        text.spellcheck = false;
        text.dataset.uid = n.uid;
        text.dataset.placeholder = n.children && n.children.length ? '' : '输入内容，Enter 换行';
        text.textContent = n.text || '';
        text.setAttribute('role', 'textbox');
        text.setAttribute('aria-multiline', 'true');
        row.appendChild(text);

        if (n.type === 'todo' && n.due) {
            var due = document.createElement('button');
            due.type = 'button';
            due.className = 'kb-ed-due';
            due.textContent = n.due;
            due.title = '截止日期，点击清除';
            due.setAttribute('aria-label', '清除截止日期 ' + n.due);
            row.appendChild(due);
        }
        return row;
    }

    function renderAll(keepFocus) {
        var active = keepFocus ? collectFocus() : null;
        rowsEl.textContent = '';
        flattenVisible(state.tree, 0).forEach(function(entry) {
            rowsEl.appendChild(buildRow(entry));
        });
        if (active && active.uid) restoreFocus(active);
    }

    function collectFocus() {
        var el = document.activeElement;
        if (el && el.classList && el.classList.contains('kb-ed-text')) {
            return { uid: el.dataset.uid, caret: getCaretOffset(el) };
        }
        return state.pendingFocus;
    }

    function restoreFocus(f) {
        var el = rowsEl.querySelector('.kb-ed-text[data-uid="' + f.uid + '"]');
        if (!el) return;
        el.focus();
        if (f.caret != null) setCaretOffset(el, f.caret);
    }

    function getCaretOffset(el) {
        var sel = window.getSelection();
        if (!sel || !sel.rangeCount) return 0;
        var r = sel.getRangeAt(0);
        if (!el.contains(r.endContainer)) return 0;
        var pre = r.cloneRange();
        pre.selectNodeContents(el);
        pre.setEnd(r.endContainer, r.endOffset);
        return pre.toString().length;
    }

    function setCaretOffset(el, offset) {
        var sel = window.getSelection();
        if (!sel) return;
        var remaining = offset || 0;
        var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
        var node;
        while ((node = walker.nextNode())) {
            var len = node.nodeValue.length;
            if (remaining <= len) {
                var r = document.createRange();
                r.setStart(node, remaining);
                r.collapse(true);
                sel.removeAllRanges();
                sel.addRange(r);
                return;
            }
            remaining -= len;
        }
        var rr = document.createRange();
        rr.selectNodeContents(el);
        rr.collapse(false);
        sel.removeAllRanges();
        sel.addRange(rr);
    }

    // ===================================================== 变更与保存
    function markDirty() {
        state.dirty = true;
        hideBanner(conflictEl);
        scheduleSave();
        scheduleDraft();
    }

    function scheduleSave() {
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(doSave, 800);
    }

    function setStatus(text, cls) {
        statusText.textContent = text;
        statusEl.className = 'kb-ed-status' + (cls ? ' is-' + cls : '');
    }

    function hideBanner(el) { el.classList.remove('is-open'); }
    function showBanner(el) { el.classList.add('is-open'); }

    function doSave() {
        if (!state.dirty || state.saving) { if (state.dirty) state.savingAgain = true; return; }
        state.saving = true;
        clearTimeout(state.retryTimer);
        setStatus('保存中…', 'saving');
        fetch(TREE_URL, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
            body: JSON.stringify({ base_version: state.version, tree: state.tree })
        }).then(function(resp) {
            return resp.json().catch(function() { return { ok: false }; }).then(function(data) {
                return { status: resp.status, data: data };
            });
        }).then(function(result) {
            state.saving = false;
            if (result.status === 200 && result.data.ok) {
                state.version = result.data.version;
                state.dirty = false;
                state.savingAgain = false;
                setStatus('已保存 ' + new Date().toLocaleTimeString(), 'saved');
                try { localStorage.removeItem(LS_KEY); } catch (e) {}
                if (state.dirty) scheduleSave();
                return;
            }
            if (result.status === 409) {
                state.conflictVersion = result.data.version;
                setStatus('保存冲突', 'error');
                showBanner(conflictEl);
                return;
            }
            offlineRetry(result.data && result.data.message);
        }).catch(function() {
            state.saving = false;
            offlineRetry('网络异常');
        });
    }

    function offlineRetry(reason) {
        setStatus((reason ? reason + '，' : '') + '离线，自动重试中…', 'error');
        clearTimeout(state.retryTimer);
        state.retryTimer = setTimeout(doSave, 5000);
    }

    function reloadLatest() {
        fetch(TREE_URL).then(function(r) { return r.json(); }).then(function(data) {
            if (!data || !data.ok) return;
            state.tree = data.tree.length ? data.tree : [emptyNode()];
            state.version = data.version;
            state.dirty = false;
            state.conflictVersion = null;
            hideBanner(conflictEl);
            setStatus('已加载最新版本', 'saved');
            try { localStorage.removeItem(LS_KEY); } catch (e) {}
            renderAll(false);
        }).catch(function() { setStatus('加载失败，请重试', 'error'); });
    }

    function forceSave() {
        if (state.conflictVersion) {
            state.version = state.conflictVersion;
            state.dirty = true;
            state.conflictVersion = null;
            hideBanner(conflictEl);
            setStatus('保存中…', 'saving');
            doSave();
        }
    }

    // ===================================================== 本地草稿（断网不丢）
    function scheduleDraft() {
        clearTimeout(state.draftTimer);
        state.draftTimer = setTimeout(persistDraft, 3000);
    }
    function persistDraft() {
        if (!state.dirty) return;
        try {
            localStorage.setItem(LS_KEY, JSON.stringify({
                version: state.version, tree: state.tree, at: Date.now()
            }));
        } catch (e) { /* 存储满等异常静默 */ }
    }
    function checkDraft() {
        var raw = null;
        try { raw = localStorage.getItem(LS_KEY); } catch (e) {}
        if (!raw) return;
        var draft;
        try { draft = JSON.parse(raw); } catch (e) { return; }
        if (!draft || draft.version !== state.version || !draft.tree) return;
        document.getElementById('kbEdDraftText').textContent =
            '检测到 ' + new Date(draft.at).toLocaleString() +
            ' 的未保存本地草稿（与当前版本一致），可恢复或丢弃。';
        showBanner(draftEl);
    }

    // ===================================================== 键盘与输入
    function textOf(el) { return el.innerText.replace(/\u00a0/g, ' '); }

    function convertTodoPrefix(node, text) {
        var m = TODO_MARK_RE.exec(text);
        if (!m) return false;
        node.type = 'todo';
        node.done = (m[1].toLowerCase() === 'x');
        node.text = m[2];
        node.heading_level = null;
        return true;
    }

    function applyTodoDueOnBlur(node, text) {
        if (node.type !== 'todo') return false;
        var m = DATE_RE.exec(text);
        if (!m) return false;
        var d = new Date(m[1] + 'T00:00:00');
        if (isNaN(d.getTime())) return false;
        node.due = m[1];
        node.text = (m[2] || '').trim();
        return true;
    }

    rowsEl.addEventListener('compositionstart', function() { state.composing = true; });
    rowsEl.addEventListener('compositionend', function(e) {
        state.composing = false;
        var el = e.target.closest('.kb-ed-text');
        if (el) syncTextNode(el, true);
    });

    function syncTextNode(el, allowConvert) {
        var hit = findNode(el.dataset.uid);
        if (!hit) return;
        var text = textOf(el);
        hit.node.text = text;
        if (allowConvert && !state.composing) {
            if (convertTodoPrefix(hit.node, text)) {
                markDirty();
                renderAll(false);
                restoreFocus({ uid: hit.node.uid, caret: 0 });
                return;
            }
        }
        markDirty();
    }

    rowsEl.addEventListener('input', function(e) {
        var el = e.target.closest('.kb-ed-text');
        if (!el) return;
        var hit = findNode(el.dataset.uid);
        if (!hit) return;
        hit.node.text = textOf(el);   // 合成期内仅同步文本，不转换不重渲染
        markDirty();
    });

    rowsEl.addEventListener('focusout', function(e) {
        var el = e.target.closest('.kb-ed-text');
        if (!el || state.composing) return;
        var hit = findNode(el.dataset.uid);
        if (!hit) return;
        var text = textOf(el);
        if (convertTodoPrefix(hit.node, text) || applyTodoDueOnBlur(hit.node, text)) {
            markDirty();
            renderAll(false);
        }
    });

    function insertNodeAfter(hit, node) {
        hit.parentList.splice(hit.index + 1, 0, node);
        return node;
    }

    function moveSibling(hit, delta) {
        var i = hit.index + delta;
        if (i < 0 || i >= hit.parentList.length) return null;
        var tmp = hit.parentList[hit.index];
        hit.parentList[hit.index] = hit.parentList[i];
        hit.parentList[i] = tmp;
        return hit.node;
    }

    rowsEl.addEventListener('click', function(e) {
        var row = e.target.closest('.kb-ed-row');
        if (!row) return;
        var hit = findNode(row.dataset.uid);
        if (!hit) return;
        var node = hit.node;

        var fold = e.target.closest('.kb-ed-fold');
        if (fold) {
            node.collapsed = !node.collapsed;
            renderAll(false);
            return;
        }
        var dueBtn = e.target.closest('.kb-ed-due');
        if (dueBtn) {
            node.due = null;
            markDirty();
            renderAll(false);
            return;
        }
        if (e.target.classList && e.target.classList.contains('kb-ed-check')) {
            node.done = e.target.checked;
            row.classList.toggle('is-done', node.done);
            markDirty();
        }
    });

    rowsEl.addEventListener('keydown', function(e) {
        var el = e.target.closest ? e.target.closest('.kb-ed-text') : null;
        if (!el) return;
        var hit = findNode(el.dataset.uid);
        if (!hit) return;
        var node = hit.node;
        var caret = getCaretOffset(el);

        if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key === 'Enter') {
            // Ctrl+Enter：新建子节点
            e.preventDefault();
            if (blockIfFull()) return;
            if (depthAll([node]) + depthOfPath(node) + 1 > MAX_DEPTH) { flashLimit('已达最大层级 ' + MAX_DEPTH); return; }
            var child = emptyNode();
            node.children.push(child);
            node.collapsed = false;
            markDirty();
            renderAll(false);
            restoreFocus({ uid: child.uid, caret: 0 });
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (blockIfFull()) return;
            var sib = emptyNode();
            insertNodeAfter(hit, sib);   // 插在当前子树之后（同级）
            markDirty();
            renderAll(false);
            restoreFocus({ uid: sib.uid, caret: 0 });
            return;
        }
        if (e.key === 'Enter' && e.shiftKey) {
            // 软换行：插入换行符，不做结构变更
            e.preventDefault();
            document.execCommand('insertLineBreak');
            markDirty();
            return;
        }
        if (e.key === 'Tab') {
            e.preventDefault();
            if (e.shiftKey) {
                if (!hit.parent) return;   // 已是顶层
                var grand = findNode(hit.parent.uid);
                hit.parentList.splice(hit.index, 1);
                grand.parentList.splice(grand.index + 1, 0, node);
            } else {
                if (hit.index === 0) return;   // 没有前兄弟可挂
                var prev = hit.parentList[hit.index - 1];
                if (depthOfPath(node) + 1 + 1 > MAX_DEPTH) { flashLimit('已达最大层级 ' + MAX_DEPTH); return; }
                hit.parentList.splice(hit.index, 1);
                prev.children.push(node);
                prev.collapsed = false;
            }
            markDirty();
            renderAll(false);
            restoreFocus({ uid: node.uid, caret: caret });
            return;
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
            e.preventDefault();
            if (!moveSibling(hit, e.key === 'ArrowUp' ? -1 : 1)) return;
            markDirty();
            renderAll(false);
            restoreFocus({ uid: node.uid, caret: caret });
            return;
        }
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            var rows = flattenVisible(state.tree, 0);
            var pos = -1;
            for (var i = 0; i < rows.length; i++) { if (rows[i].node.uid === node.uid) { pos = i; break; } }
            var next = rows[pos + (e.key === 'ArrowUp' ? -1 : 1)];
            if (next) restoreFocus({ uid: next.node.uid, caret: caret });
            return;
        }
        if (e.key === 'Backspace' && caret === 0) {
            // 行首一律接管：空行删行，非空行不动（防浏览器默认跨块合并/误删）
            e.preventDefault();
            var isEmpty = !textOf(el).length;
            var alone = countAll(state.tree) === 1;
            if (!isEmpty || alone) return;
            hit.parentList.splice(hit.index, 1);
            var rowsAfter = flattenVisible(state.tree, 0);
            var target = hit.index > 0 ? rowsAfter[Math.min(hit.index - 1, rowsAfter.length - 1)] : rowsAfter[0];
            if (!target) { state.tree = [emptyNode()]; renderAll(false); return; }
            markDirty();
            renderAll(false);
            restoreFocus({ uid: target.node.uid, caret: 'end' });
        }
    });

    function depthOfPath(node) {
        // node 所在深度（顶层为 0）：沿父链回溯
        var d = 0, found = null;
        (function walk(nodes, depth) {
            (nodes || []).forEach(function(n) {
                if (n === node) { found = depth; return; }
                if (found == null) walk(n.children, depth + 1);
            });
        })(state.tree, 0);
        return found == null ? 0 : found;
    }

    function blockIfFull() {
        if (countAll(state.tree) >= MAX_NODES) { flashLimit('已达节点上限 ' + MAX_NODES + ' 个'); return true; }
        return false;
    }
    function flashLimit(msg) { setStatus(msg, 'error'); }

    // ===================================================== 导出 Markdown
    function nodeToLines(n, level, out) {
        var ind = new Array(level + 1).join('  ');
        var t = (n.text || '').replace(/\n/g, ' ').trim();
        if (n.type === 'heading') {
            var lv = Math.min(Math.max(n.heading_level || 1, 1), 3);
            out.push(ind + new Array(lv + 1).join('#') + ' ' + t);
        } else if (n.type === 'todo') {
            if (n.due && !DATE_RE.test(t)) t = n.due + ' ' + t;
            out.push(ind + '- ' + (n.done ? '[x]' : '[ ]') + ' ' + t);
        } else if (n.type === 'quote') {
            out.push(ind + '> ' + t);
        } else if (n.type === 'divider') {
            out.push(ind + '---');
        } else if (n.type === 'code') {
            out.push(ind + '```' + t);
            if (n.note) out.push(n.note);
            out.push(ind + '```');
        } else {
            out.push(ind + '- ' + t);
        }
        (n.children || []).forEach(function(c) { nodeToLines(c, level + 1, out); });
    }

    document.getElementById('kbEdExport').addEventListener('click', function() {
        var out = [];
        state.tree.forEach(function(n) { nodeToLines(n, 0, out); });
        var blob = new Blob([out.join('\n')], { type: 'text/markdown;charset=utf-8' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = D_knowledge_base_outline_editor.vars['v2'] + '.md';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function() { URL.revokeObjectURL(a.href); }, 1000);
    });

    // ===================================================== 冲突 / 草稿按钮
    document.getElementById('kbEdReload').addEventListener('click', reloadLatest);
    document.getElementById('kbEdForce').addEventListener('click', forceSave);
    document.getElementById('kbEdDraftRestore').addEventListener('click', function() {
        try {
            var draft = JSON.parse(localStorage.getItem(LS_KEY));
            if (draft && draft.tree) {
                state.tree = draft.tree;
                state.dirty = true;
                renderAll(false);
                setStatus('草稿已恢复', 'saved');
                scheduleSave();
            }
        } catch (e) { setStatus('草稿恢复失败', 'error'); }
        hideBanner(draftEl);
    });
    document.getElementById('kbEdDraftDiscard').addEventListener('click', function() {
        try { localStorage.removeItem(LS_KEY); } catch (e) {}
        hideBanner(draftEl);
    });

    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
            e.preventDefault();
            clearTimeout(state.saveTimer);
            state.dirty = true;
            doSave();
        }
    });
    window.addEventListener('beforeunload', function(e) {
        persistDraft();
        if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
    });

    // ===================================================== 启动
    renderAll(false);
    checkDraft();

    // 工作台 ?node=<uid> 跳转定位：滚动 + 高亮
    var m = /[?&]node=([0-9a-f]{12})/i.exec(location.search);
    if (m) {
        var hit = findNode(m[1]);
        if (hit) {
            var chain = [];
            (function reveal(nodes) {
                (nodes || []).forEach(function(n) {
                    if (n.uid === m[1]) return true;
                    if (reveal(n.children)) { n.collapsed = false; return true; }
                    return false;
                });
            })(state.tree);
            renderAll(false);
            var row = rowsEl.querySelector('.kb-ed-row[data-uid="' + m[1] + '"]');
            if (row) {
                row.classList.add('kb-ed-highlight');
                row.scrollIntoView({ block: 'center' });
                var tEl = row.querySelector('.kb-ed-text');
                if (tEl) { tEl.focus(); setCaretOffset(tEl, 0); }
                setTimeout(function() { row.classList.remove('kb-ed-highlight'); }, 3000);
            }
        }
    }
})();
