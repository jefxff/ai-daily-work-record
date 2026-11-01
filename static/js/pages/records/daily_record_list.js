document.addEventListener('DOMContentLoaded', function() {
        // 迷你进度条注入（data-pct -> width）
        document.querySelectorAll('.mini-progress').forEach(function(el) {
            var pct = parseInt(el.getAttribute('data-pct'), 10) || 0;
            if (pct >= 100) el.classList.add('done');
            var fill = el.querySelector('.mini-progress-fill');
            if (fill) fill.style.width = Math.min(pct, 100) + '%';
        });

        // 批量沉淀到知识库（仅 POST；一律私有，D5）
        var kbSyncBtn = document.getElementById('kbSyncBatchBtn');
        if (kbSyncBtn) {
            kbSyncBtn.addEventListener('click', function () {
                if (!window.confirm('把当前所有未沉淀的每日记录沉淀到知识库？\n批量沉淀的记录一律「仅自己可见」，之后可在知识库里逐篇调整。')) return;
                kbSyncBtn.disabled = true;
                var fd = new FormData();
                fd.append('visibility', 'private');
                fetch(kbSyncBtn.dataset.url, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': getCsrfToken() || getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
                    body: fd,
                    keepalive: true
                })
                .then(function (resp) { return resp.json().then(function (data) { return { ok: resp.ok, data: data }; }); })
                .then(function (result) {
                    if (result.ok && result.data.ok) {
                        if (window.showToast) window.showToast(result.data.message, 'success');
                        // 完整消费响应体后再刷新，避免 ERR_ABORTED
                        setTimeout(function () { window.location.reload(); }, 900);
                    } else {
                        if (window.showToast) window.showToast((result.data && result.data.message) || '沉淀失败，请重试', 'error');
                        kbSyncBtn.disabled = false;
                    }
                })
                .catch(function () {
                    if (window.showToast) window.showToast('网络异常，请重试', 'error');
                    kbSyncBtn.disabled = false;
                });
            });
        }
    });
