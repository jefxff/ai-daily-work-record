(function () {
    document.addEventListener('DOMContentLoaded', function () {
        // 迷你趋势条 / 进度条：模板值经 data-* 传递，避免内联 style 出现模板语法
        document.querySelectorAll('.home-spark__bar[data-h]').forEach(function (bar) {
            bar.style.height = bar.dataset.h + 'px';
        });
        document.querySelectorAll('.home-progress__bar i[data-w]').forEach(function (bar) {
            bar.style.width = bar.dataset.w + '%';
        });
    });
})();
