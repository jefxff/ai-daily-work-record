document.addEventListener('DOMContentLoaded', function() {
    // 迷你进度条注入（data-pct -> width）
    document.querySelectorAll('.mini-progress').forEach(function(el) {
        var pct = parseInt(el.getAttribute('data-pct'), 10) || 0;
        if (pct >= 100) el.classList.add('done');
        var fill = el.querySelector('.mini-progress-fill');
        if (fill) fill.style.width = Math.min(pct, 100) + '%';
    });
});
