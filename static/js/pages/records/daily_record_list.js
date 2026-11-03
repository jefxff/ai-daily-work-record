document.addEventListener('DOMContentLoaded', function() {
    // 迷你进度条注入（data-pct -> width）
    document.querySelectorAll('.mini-progress').forEach(function(el) {
        var pct = parseInt(el.getAttribute('data-pct'), 10) || 0;
        if (pct >= 100) el.classList.add('done');
        var fill = el.querySelector('.mini-progress-fill');
        if (fill) fill.style.width = Math.min(pct, 100) + '%';
    });

    // ---------- 批量选择与批量操作（规划书 drl-3） ----------
    var checkAll = document.getElementById('drCheckAll');
    var batchBar = document.getElementById('drBatchBar');
    if (!checkAll || !batchBar) return;

    var batchCount = document.getElementById('drBatchCount');
    var batchUrl = batchBar.dataset.batchUrl;
    var exportUrl = batchBar.dataset.exportUrl;

    function rowChecks() {
        return Array.prototype.slice.call(document.querySelectorAll('.dr-row-check'));
    }
    function selectedIds() {
        return rowChecks().filter(function (c) { return c.checked; })
            .map(function (c) { return c.value; });
    }
    function refresh() {
        var checks = rowChecks();
        var sel = rowChecks().filter(function (c) { return c.checked; });
        if (batchCount) batchCount.textContent = String(sel.length);
        batchBar.hidden = sel.length === 0;
        if (checkAll) {
            checkAll.checked = checks.length > 0 && sel.length === checks.length;
            checkAll.indeterminate = sel.length > 0 && sel.length < checks.length;
        }
    }

    checkAll.addEventListener('change', function () {
        var on = checkAll.checked;
        rowChecks().forEach(function (c) { c.checked = on; });
        refresh();
    });
    document.addEventListener('change', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('dr-row-check')) {
            refresh();
        }
    });

    batchBar.addEventListener('click', function (e) {
        if (e.target.closest('[data-batch-clear]')) {
            rowChecks().forEach(function (c) { c.checked = false; });
            refresh();
            return;
        }
        var btn = e.target.closest('[data-batch]');
        if (!btn) return;
        var ids = selectedIds();
        if (!ids.length) return;
        var action = btn.dataset.batch;
        var confirmMsg = btn.dataset.confirm;
        if (confirmMsg && !window.confirm(confirmMsg)) return;

        if (action === 'export') {
            window.location.assign(exportUrl + '?ids=' + encodeURIComponent(ids.join(',')));
            return;
        }

        btn.disabled = true;
        var body = new URLSearchParams();
        body.append('action', action);
        body.append('ids', ids.join(','));
        fetch(batchUrl, {
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
                window.location.reload();
            } else {
                btn.disabled = false;
                window.alert((data && data.message) || '操作失败，请重试');
            }
        }).catch(function (err) {
            btn.disabled = false;
            window.alert('操作失败：' + err.message);
        });
    });

    function csrfToken() {
        var m = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (m) return m.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }
});
