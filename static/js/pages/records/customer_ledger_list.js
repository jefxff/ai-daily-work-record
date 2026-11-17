/* 客户台账列表：超级管理员「删除选中」（跨页保留）+「清空台账」（删全部）。
   仅有超管渲染勾选列与按钮；普通用户此脚本直接返回。
   勾选状态存在 sessionStorage，按「当前筛选签名」隔离，翻页 / 排序 / 改每页条数都不丢。 */
document.addEventListener('DOMContentLoaded', function () {
    var checkAll = document.getElementById('clCheckAll');
    var batchBtn = document.getElementById('clBatchDelete');
    var purgeBtn = document.getElementById('clPurgeLedger');
    if (!checkAll || !batchBtn || !purgeBtn) return;

    var counter = document.getElementById('clBatchCount');
    var clearBtn = document.getElementById('clClearSelection');
    var storeKey = 'clSelected::' + selectionScope();

    /* 筛选签名：同一套筛选条件下共享选中集；切换筛选视为另一批数据，互不干扰。 */
    function selectionScope() {
        var params = new URLSearchParams(window.location.search);
        ['page', 'sort', 'dir', 'per_page'].forEach(function (k) { params.delete(k); });
        var parts = [];
        Array.from(params.keys()).sort().forEach(function (k) {
            params.getAll(k).forEach(function (v) { parts.push(k + '=' + v); });
        });
        return parts.join('&') || 'all';
    }

    function loadIds() {
        try {
            var raw = window.sessionStorage.getItem(storeKey);
            var arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr.map(String) : [];
        } catch (e) {
            return [];
        }
    }

    function saveIds(ids) {
        try {
            if (ids.length) window.sessionStorage.setItem(storeKey, JSON.stringify(ids));
            else window.sessionStorage.removeItem(storeKey);
        } catch (e) { /* 隐私模式下 sessionStorage 不可用：退化为仅当前页选中 */ }
    }

    var selected = loadIds();

    function rowChecks() {
        return Array.prototype.slice.call(document.querySelectorAll('.cl-row-check'));
    }

    function isSelected(id) { return selected.indexOf(String(id)) !== -1; }

    function toggle(id, on) {
        id = String(id);
        var at = selected.indexOf(id);
        if (on && at === -1) selected.push(id);
        else if (!on && at !== -1) selected.splice(at, 1);
    }

    function refresh() {
        var checks = rowChecks();
        var pageSelected = 0;
        checks.forEach(function (c) {
            c.checked = isSelected(c.value);
            if (c.checked) pageSelected += 1;
        });
        batchBtn.disabled = selected.length === 0;
        if (counter) counter.textContent = selected.length ? '（' + selected.length + '）' : '';
        if (clearBtn) clearBtn.hidden = selected.length === 0;
        checkAll.checked = checks.length > 0 && pageSelected === checks.length;
        checkAll.indeterminate = pageSelected > 0 && pageSelected < checks.length;
    }

    /* 全选：作用域是「当前页」——跨页已选项保持不动，本页未选的全部加入。 */
    checkAll.addEventListener('change', function () {
        var on = checkAll.checked;
        rowChecks().forEach(function (c) { toggle(c.value, on); });
        saveIds(selected);
        refresh();
    });

    document.addEventListener('change', function (e) {
        var el = e.target;
        if (!el || !el.classList || !el.classList.contains('cl-row-check')) return;
        toggle(el.value, el.checked);
        saveIds(selected);
        refresh();
    });

    document.addEventListener('click', function (e) {
        if (!e.target.closest) return;
        if (e.target.closest('[data-cl-clear-selection]')) {
            selected = [];
            saveIds(selected);
            refresh();
        }
    });

    batchBtn.addEventListener('click', function () {
        if (!selected.length) return;
        var msg = '确定删除选中的 ' + selected.length + ' 条客户台账吗？附件也会一并删除，且不可恢复。';
        askConfirm(msg, false, function () {
            submit(batchBtn.dataset.batchUrl, { ids: selected.join(',') }, batchBtn);
        });
    });

    purgeBtn.addEventListener('click', function () {
        var total = purgeBtn.dataset.total || '';
        var msg = '将删除全部 ' + total + ' 条客户台账（无视筛选与分页），附件一并清理，不可恢复。\n\n' +
                  '确认请在下框输入「清空」两个字。';
        askConfirm(msg, true, function (typed) {
            submit(purgeBtn.dataset.purgeUrl, { confirm: typed }, purgeBtn);
        });
    });

    /* 用 URLSearchParams 提交：浏览器会自动带上
       Content-Type: application/x-www-form-urlencoded;charset=UTF-8，
       后端 request.POST 才能读到字段（手拼字符串会被当成 text/plain，
       导致视图里 ids / confirm 取到空值）。 */
    function submit(url, fields, btn) {
        btn.disabled = true;
        var body = new URLSearchParams();
        Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
        fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
            body: body,
            credentials: 'same-origin'
        }).then(function (resp) {
            return resp.json().catch(function () {
                throw new Error('响应不是有效 JSON（HTTP ' + resp.status + '）');
            });
        }).then(function (data) {
            if (data && data.ok) {
                selected = [];
                saveIds(selected);
                window.location.reload();
            } else {
                btn.disabled = false;
                showToast((data && data.message) || '操作失败，请重试');
            }
        }).catch(function (err) {
            btn.disabled = false;
            showToast('操作失败：' + err.message);
        });
    }

    /* 复用页面内联确认框（本页自带，不依赖全局 shell 的 data-confirm 机制）。
       危险操作（清空）额外要求输入确认词才放行。 */
    function askConfirm(message, needTyping, onOk) {
        var backdrop = document.getElementById('clConfirmBackdrop');
        if (!backdrop) { if (window.confirm(message)) onOk('清空'); return; }
        var msgEl = document.getElementById('clConfirmMessage');
        var input = document.getElementById('clConfirmInput');
        var okBtn = document.getElementById('clConfirmOk');
        var cancelBtn = document.getElementById('clConfirmCancel');
        var hint = document.getElementById('clConfirmHint');
        var lastFocused = document.activeElement;

        msgEl.textContent = message;
        if (input) {
            input.value = '';
            input.hidden = !needTyping;
        }
        if (hint) hint.hidden = !needTyping;
        okBtn.disabled = !!needTyping;
        backdrop.classList.add('show');
        (needTyping && input ? input : okBtn).focus();

        function close() {
            backdrop.classList.remove('show');
            okBtn.removeEventListener('click', onConfirm);
            cancelBtn.removeEventListener('click', close);
            input && input.removeEventListener('input', validate);
            backdrop.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKey);
            if (lastFocused && lastFocused.focus) lastFocused.focus();
        }
        function validate() {
            okBtn.disabled = (input.value || '').trim() !== '清空';
        }
        function onConfirm() {
            var typed = needTyping ? (input.value || '').trim() : '';
            if (needTyping && typed !== '清空') return;
            close();
            onOk(typed);
        }
        function onBackdrop(e) { if (e.target === backdrop) close(); }
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
            else if (e.key === 'Enter' && !okBtn.disabled) { e.preventDefault(); onConfirm(); }
        }
        okBtn.addEventListener('click', onConfirm);
        cancelBtn.addEventListener('click', close);
        if (input) {
            input.removeEventListener('input', validate);
            input.addEventListener('input', validate);
        }
        backdrop.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKey);
    }

    /* 优先用 shell.js 暴露的全局 Toast，页面未加载时退化为 alert。 */
    function showToast(text, type) {
        if (typeof window.showToast === 'function') window.showToast(text, type || 'error');
        else window.alert(text);
    }

    function csrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    refresh();
});
