document.addEventListener('DOMContentLoaded', function () {
    var ff = document.querySelector('.filter-form');
    if (!ff) return;
    // 提交前禁用空值控件，保持 URL 干净
    function cleanSubmit() {
        ff.querySelectorAll('input[name], select[name]').forEach(function (el) {
            if (el.type === 'checkbox') { el.disabled = !el.checked; }
            else { el.disabled = !el.value; }
        });
        ff.submit();
    }
    ff.addEventListener('submit', function (e) { e.preventDefault(); cleanSubmit(); });
    // 下拉/日期变更后自动查询
    ff.querySelectorAll('select').forEach(function (s) {
        s.addEventListener('change', cleanSubmit);
    });
    ff.querySelectorAll('input[type="date"]').forEach(function (d) {
        d.addEventListener('change', cleanSubmit);
    });
    // 含已删除勾选后自动查询
    ff.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
        cb.addEventListener('change', cleanSubmit);
    });
});
