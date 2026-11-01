document.addEventListener('DOMContentLoaded', function () {
    // 动态样式通过 data-* 注入，避免 style 属性内出现模板语法
    // 月度趋势柱高（视图按最大 38px 设计，此处等比放大适配 150px 绘图区）
    document.querySelectorAll('.m-bar[data-h]').forEach(function (bar) {
        var unit = parseInt(bar.dataset.h, 10) || 3;
        var px = Math.max(4, Math.round(unit / 38 * 150));
        bar.style.height = px + 'px';
    });
    // 状态分布：圆点颜色 + 条形宽度/颜色
    document.querySelectorAll('.status-dot[data-color]').forEach(function (dot) {
        if (dot.dataset.color) dot.style.background = dot.dataset.color;
    });
    document.querySelectorAll('.status-fill[data-pct]').forEach(function (el) {
        el.style.width = el.dataset.pct + '%';
        if (el.dataset.color) el.style.background = el.dataset.color;
    });
    // 供应商条形宽度
    document.querySelectorAll('.supplier-fill[data-pct]').forEach(function (el) {
        el.style.width = el.dataset.pct + '%';
    });
});
