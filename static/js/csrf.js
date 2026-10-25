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
    /**
     * CSRF token 统一出口（UI 反馈 #3/#5）：
     * HTTPS 模式下 cookie 名是 __Host-csrftoken（settings 加固），
     * HTTP 模式是 csrftoken。此前各处只读 csrftoken，
     * 8443 HTTPS 下 fetch 带空 token → 全部 POST 403
     * （金属价格刷新、一键备份、每日记录筛选等均命中）。
     * 新代码一律用 window.getCsrfToken()，不要再直接 getCookie('csrftoken')。
     */
    window.getCsrfToken = function () {
        return window.getCookie('csrftoken')
            || window.getCookie('__Host-csrftoken')
            || '';
    };
})();
