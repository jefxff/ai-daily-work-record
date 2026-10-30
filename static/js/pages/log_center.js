/**
 * 日志中心页面交互：
 * 1) 「采集最新」按钮：POST 采集接口，成功后刷新页面并提示结果。
 *    （筛选下拉的开合/Esc/点击外部关闭已收进 base.html 的公共脚本，此处不再重复绑定）
 */
(function () {
    'use strict';

    function toast(message, type) {
        if (window.showToast) window.showToast(message, type || 'success');
    }

    function initIngest() {
        var btn = document.getElementById('logIngestBtn');
        if (!btn) return;

        btn.addEventListener('click', function () {
            if (btn.disabled) return;
            var url = btn.getAttribute('data-url');
            var label = btn.textContent;
            btn.disabled = true;
            btn.textContent = '采集中…';

            fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': window.getCsrfToken ? window.getCsrfToken() : ''
                }
            }).then(function (response) {
                return response.json().then(function (body) {
                    return { ok: response.ok, body: body };
                }).catch(function () {
                    return { ok: false, body: { message: '服务返回异常，请稍后重试。' } };
                });
            }).then(function (result) {
                if (!result.ok || !result.body.ok) {
                    throw new Error(result.body.message || '采集失败，请稍后重试。');
                }
                toast(result.body.message || '采集完成', 'success');
                setTimeout(function () { window.location.reload(); }, 600);
            }).catch(function (error) {
                toast(error.message || '采集失败，请稍后重试。', 'error');
                btn.disabled = false;
                btn.textContent = label;
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initIngest();
    });
})();
