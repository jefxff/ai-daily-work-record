/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-accounts-api_token" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_accounts_api_token = (function () {
  var el = document.getElementById('pageData-accounts-api_token');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

(function () {
    var genUrl = D_accounts_api_token.urls['u1'];
    // 撤销接口模板：把 /0/ 段替换为实际 Token id
    var revokeUrlTpl = D_accounts_api_token.urls['u2'];
    var csrf = getCsrfToken();

    function fallbackCopy(text, okMsg) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            if (window.showToast) window.showToast(okMsg, 'success');
        } catch (e) {
            if (window.showToast) window.showToast('复制失败，请手动选择复制', 'error');
        }
        document.body.removeChild(ta);
    }

    function copyText(text, okMsg) {
        if (!text) return;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () {
                if (window.showToast) window.showToast(okMsg, 'success');
            }).catch(function () {
                fallbackCopy(text, okMsg);
            });
        } else {
            fallbackCopy(text, okMsg);
        }
    }

    // 历史行「复制」按钮
    document.querySelectorAll('.btn-copy-row').forEach(function (b) {
        b.addEventListener('click', function () {
            copyText(b.getAttribute('data-token'), '已复制 Token');
        });
    });

    // 生成新 Token
    var btn = document.getElementById('btnGenToken');
    var box = document.getElementById('newTokenBox');
    var valEl = document.getElementById('newTokenValue');
    var createdEl = document.getElementById('newTokenCreated');
    var expiresEl = document.getElementById('newTokenExpires');
    var lastToken = '';

    document.getElementById('btnCopyNew').addEventListener('click', function () {
        copyText(lastToken, '已复制 Token');
    });

    function makeCell(label, text) {
        var td = document.createElement('td');
        td.setAttribute('data-label', label);
        td.textContent = text;
        return td;
    }

    function prependRow(d) {
        var tbody = document.getElementById('tokenTbody');
        if (!tbody) return;
        var emptyRow = document.getElementById('tokenEmptyRow');
        if (emptyRow) emptyRow.remove();

        var tr = document.createElement('tr');

        var tdKey = document.createElement('td');
        tdKey.setAttribute('data-label', 'Token 值');
        var span = document.createElement('span');
        span.className = 'token-value';
        span.textContent = d.key;
        tdKey.appendChild(span);
        tr.appendChild(tdKey);

        tr.appendChild(makeCell('创建时间', d.created_at || ''));
        tr.appendChild(makeCell('过期时间', d.expires_at || '—'));

        var tdStatus = document.createElement('td');
        tdStatus.setAttribute('data-label', '状态');
        var badge = document.createElement('span');
        badge.className = 'status-badge ' + (d.status === 'active' ? 'success' : (d.status === 'expired' ? 'warning' : 'danger'));
        badge.textContent = d.status_text || '';
        tdStatus.appendChild(badge);
        tr.appendChild(tdStatus);

        var tdOp = document.createElement('td');
        tdOp.setAttribute('data-label', '操作');
        var copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'btn btn-sm btn-outline-secondary';
        copyBtn.textContent = '复制';
        copyBtn.addEventListener('click', function () { copyText(d.key, '已复制 Token'); });
        tdOp.appendChild(copyBtn);

        // 撤销表单（与后端表单同构；点击由全局 data-confirm 确认弹窗接管）
        if (d.id) {
            var revokeForm = document.createElement('form');
            revokeForm.method = 'post';
            revokeForm.action = revokeUrlTpl.replace('/0/', '/' + d.id + '/');
            revokeForm.className = 'd-inline';
            revokeForm.setAttribute('data-confirm', '确定撤销该 Token 吗？撤销后使用它的调用将立即失败，且不可恢复。');
            var csrfInput = document.createElement('input');
            csrfInput.type = 'hidden';
            csrfInput.name = 'csrfmiddlewaretoken';
            csrfInput.value = csrf;
            revokeForm.appendChild(csrfInput);
            var revokeBtn = document.createElement('button');
            revokeBtn.type = 'submit';
            revokeBtn.className = 'btn btn-sm btn-outline-danger';
            revokeBtn.textContent = '撤销';
            revokeForm.appendChild(revokeBtn);
            tdOp.appendChild(revokeForm);
        }

        tr.appendChild(tdOp);

        tbody.insertBefore(tr, tbody.firstChild);
    }

    if (btn) {
        btn.addEventListener('click', function () {
            var oldText = btn.textContent;
            btn.disabled = true;
            btn.textContent = '生成中…';
            fetch(genUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': getCsrfToken() || csrf, 'X-Requested-With': 'XMLHttpRequest' },
                body: new URLSearchParams({})
            }).then(function (resp) {
                return resp.text().then(function (txt) {
                    var data = null;
                    try { data = JSON.parse(txt); } catch (e) {}
                    return { ok: resp.ok, data: data };
                });
            }).then(function (r) {
                if (r.ok && r.data && r.data.key) {
                    lastToken = r.data.key;
                    valEl.textContent = r.data.key;
                    createdEl.textContent = r.data.created_at || '';
                    expiresEl.textContent = r.data.expires_at || '长期有效';
                    box.hidden = false;
                    prependRow(r.data);
                    if (window.showToast) window.showToast('已生成新的长期 Token', 'success');
                } else {
                    var msg = (r.data && (r.data.detail || r.data.message)) || '生成失败，请稍后重试';
                    if (window.showToast) window.showToast(msg, 'error');
                }
            }).catch(function () {
                if (window.showToast) window.showToast('网络错误，请重试', 'error');
            }).finally(function () {
                btn.disabled = false;
                btn.textContent = oldText;
            });
        });
    }
})();
