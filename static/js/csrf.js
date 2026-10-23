/**
 * 全站共享工具：读取 Cookie（主要是 csrftoken）。
 * 此前 6 个模板各自复制了一份 getCookie，行为一致但难维护；
 * 统一到本文件后，模板只需直接调用 window.getCookie。
 */
(function () {
    if (typeof window.getCookie === 'function') return;
    window.getCookie = function (name) {
        var value = null;
        if (document.cookie && document.cookie !== '') {
            document.cookie.split(';').forEach(function (raw) {
                var parts = raw.trim().split('=');
                if (parts[0] === name) value = decodeURIComponent(parts.slice(1).join('='));
            });
        }
        return value;
    };
})();
