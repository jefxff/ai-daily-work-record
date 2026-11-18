/* 客户台账列表：超级管理员「清空台账」（删全部）。
   仅有超管渲染该按钮；普通用户此脚本直接返回。
   危险操作：需在确认框里手输「清空」二字才放行。 */
document.addEventListener('DOMContentLoaded', function () {
    var purgeBtn = document.getElementById('clPurgeLedger');
    if (!purgeBtn) return;

    purgeBtn.addEventListener('click', function () {
        var total = purgeBtn.dataset.total || '';
        var msg = '将删除全部 ' + total + ' 条客户台账（无视筛选与分页），附件一并清理，不可恢复。\n\n' +
                  '确认请在下框输入「清空」两个字。';
        askConfirm(msg, true, function (typed) {
            submit(purgeBtn.dataset.purgeUrl, { confirm: typed }, purgeBtn);
        });
    });

    /* 用 URLSearchParams 提交：浏览器会自动带上
       Content-Type: application/x-www-form-urlencoded;charset=UTF-8，
       后端 request.POST 才能读到字段（手拼字符串会被当成 text/plain，
       导致视图里 confirm 取到空值）。 */
    function submit(url, fields, btn) {
        btn.disabled = true;
        var body = new URLSearchParams();
        Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
        fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
            body: body,
            credentials: 'same-origin'
        }).then(function (resp) {
            return resp.json().catch(function () {
                throw new Error('响应不是有效 JSON（HTTP ' + resp.status + '）');
            });
        }).then(function (data) {
            if (data && data.ok) {
                window.location.reload();
            } else {
                btn.disabled = false;
                showToast((data && data.message) || '操作失败，请重试');
            }
        }).catch(function (err) {
            btn.disabled = false;
            showToast('操作失败：' + err.message);
        });
    }

    /* 复用页面内联确认框（本页自带，不依赖全局 shell 的 data-confirm 机制）。
       危险操作（清空）额外要求输入确认词才放行。 */
    function askConfirm(message, needTyping, onOk) {
        var backdrop = document.getElementById('clConfirmBackdrop');
        if (!backdrop) { if (window.confirm(message)) onOk('清空'); return; }
        var msgEl = document.getElementById('clConfirmMessage');
        var input = document.getElementById('clConfirmInput');
        var okBtn = document.getElementById('clConfirmOk');
        var cancelBtn = document.getElementById('clConfirmCancel');
        var hint = document.getElementById('clConfirmHint');
        var lastFocused = document.activeElement;

        msgEl.textContent = message;
        if (input) {
            input.value = '';
            input.hidden = !needTyping;
        }
        if (hint) hint.hidden = !needTyping;
        okBtn.disabled = !!needTyping;
        backdrop.classList.add('show');
        (needTyping && input ? input : okBtn).focus();

        function close() {
            backdrop.classList.remove('show');
            okBtn.removeEventListener('click', onConfirm);
            cancelBtn.removeEventListener('click', close);
            input && input.removeEventListener('input', validate);
            backdrop.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKey);
            if (lastFocused && lastFocused.focus) lastFocused.focus();
        }
        function validate() {
            okBtn.disabled = (input.value || '').trim() !== '清空';
        }
        function onConfirm() {
            var typed = needTyping ? (input.value || '').trim() : '';
            if (needTyping && typed !== '清空') return;
            close();
            onOk(typed);
        }
        function onBackdrop(e) { if (e.target === backdrop) close(); }
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
            else if (e.key === 'Enter' && !okBtn.disabled) { e.preventDefault(); onConfirm(); }
        }
        okBtn.addEventListener('click', onConfirm);
        cancelBtn.addEventListener('click', close);
        if (input) {
            input.removeEventListener('input', validate);
            input.addEventListener('input', validate);
        }
        backdrop.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKey);
    }

    /* 优先用 shell.js 暴露的全局 Toast，页面未加载时退化为 alert。 */
    function showToast(text, type) {
        if (typeof window.showToast === 'function') window.showToast(text, type || 'error');
        else window.alert(text);
    }

    function csrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }
});
