document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('copyReport');
        var source = document.getElementById('reportMdSource');
        if (!btn || !source) { return; }
        btn.addEventListener('click', function () {
            var text = source.textContent;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function () {
                    window.showToast('已复制到剪贴板', 'success');
                }, function () {
                    window.showToast('复制失败，请手动选择文本', 'error');
                });
            } else {
                window.showToast('当前浏览器不支持自动复制，请手动选择文本', 'error');
            }
        });
    });
