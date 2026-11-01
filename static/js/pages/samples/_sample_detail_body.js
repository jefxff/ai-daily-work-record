(function () {
    // 供应商类型 AJAX 切换（弹窗注入时 DOM 已就绪，直接执行）
    var sel = document.getElementById('supplierTypeSelect');
    if (!sel) return;
    var original = sel.value;
    sel.addEventListener('change', function () {
        if (sel.value === original) return;
        var url = sel.dataset.url;
        var fd = new FormData();
        fd.append('supplier_type', sel.value);
        fd.append('csrfmiddlewaretoken', getCsrfToken());
        fetch(url, { method: 'POST', body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    if (window.showToast) window.showToast('供应商类型已更新', 'success');
                    setTimeout(function(){ window.location.reload(); }, 600);
                }
                else {
                    if (window.showToast) window.showToast(data.message || data.error || '更新失败', 'error');
                    sel.value = original;
                }
            })
            .catch(function () {
                if (window.showToast) window.showToast('网络错误，更新失败', 'error');
                sel.value = original;
            });
    });
})();
