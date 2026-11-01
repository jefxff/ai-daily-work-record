/* 「导出所选」：收集勾选的台账 pk 动态填入隐藏表单提交（卡片内已有删除 POST 表单，不能嵌套包裹） */
(function () {
    var btn = document.getElementById('btnBatchExport');
    var form = document.getElementById('batchExportForm');
    if (!btn || !form) return;
    btn.addEventListener('click', function () {
        var checked = Array.prototype.slice.call(document.querySelectorAll('.lg-check:checked'));
        if (!checked.length) {
            if (window.showToast) window.showToast('请先勾选要导出的台账', 'error');
            return;
        }
        form.querySelectorAll('input[name="ledgers"]').forEach(function (i) { i.remove(); });
        checked.forEach(function (c) {
            var inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = 'ledgers';
            inp.value = c.value;
            form.appendChild(inp);
        });
        form.submit();
    });
})();
