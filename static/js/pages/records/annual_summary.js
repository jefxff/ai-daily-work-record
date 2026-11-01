document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('copyShareText');
        var pre = document.getElementById('shareText');
        if (!btn || !pre) { return; }
        btn.addEventListener('click', function () {
            var text = pre.textContent;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function () {
                    window.showToast('已复制', 'success');
                }, function () {
                    window.showToast('复制失败，请手动选择文本', 'error');
                });
            } else {
                window.showToast('当前浏览器不支持自动复制，请手动选择文本', 'error');
            }
        });
    });
