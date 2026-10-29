/**
 * 日志中心页面交互：
 * 1) 筛选下拉（含 Esc 关闭、点击外部关闭、aria-expanded 同步）；
 * 2) 「采集最新」按钮：POST 采集接口，成功后刷新页面并提示结果。
 */
(function () {
    'use strict';

    function toast(message, type) {
        if (window.showToast) window.showToast(message, type || 'success');
    }

    function initFilter() {
        var btn = document.getElementById('filterBtn');
        var panel = document.getElementById('filterDropdown');
        if (!btn || !panel) return;

        function setOpen(open) {
            panel.classList.toggle('show', open);
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        btn.addEventListener('click', function (event) {
            event.stopPropagation();
            setOpen(!panel.classList.contains('show'));
        });
        document.addEventListener('click', function (event) {
            if (!panel.contains(event.target) && !btn.contains(event.target)) setOpen(false);
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') setOpen(false);
        });
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
        initFilter();
        initIngest();
    });
})();
