/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-ai_api-ai_config" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_ai_api_ai_config = (function () {
  var el = document.getElementById('pageData-ai_api-ai_config');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

document.addEventListener('DOMContentLoaded', function () {
        var providerSelect = document.getElementById('providerSelect');
        var baseUrlInput = document.getElementById('baseUrlInput');
        var defaultsEl = document.getElementById('aiDefaults');
        var defaults = {};
        if (defaultsEl) {
            try { defaults = JSON.parse(defaultsEl.textContent) || {}; } catch (e) { defaults = {}; }
        }
        if (providerSelect && baseUrlInput) {
            providerSelect.addEventListener('change', function () {
                baseUrlInput.placeholder = defaults[providerSelect.value] || '请填写 API 地址';
            });
        }

        var toggleKey = document.getElementById('toggleKey');
        var keyInput = document.getElementById('apiKeyInput');
        if (toggleKey && keyInput) {
            toggleKey.addEventListener('click', function () {
                var showing = keyInput.type === 'text';
                keyInput.type = showing ? 'password' : 'text';
                toggleKey.textContent = showing ? '显示' : '隐藏';
            });
        }

        var testBtn = document.getElementById('testBtn');
        var form = document.getElementById('aiConfigForm');
        if (testBtn && form) {
            testBtn.addEventListener('click', function () {
                var oldText = testBtn.textContent;
                var csrfField = form.querySelector('[name=csrfmiddlewaretoken]');
                testBtn.disabled = true;
                testBtn.textContent = '测试中…';
                fetch(D_ai_api_ai_config.urls['u1'], {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': csrfField ? csrfField.value : '',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: new FormData(form)
                }).then(function (r) {
                    return r.json();
                }).then(function (data) {
                    if (data && data.ok) {
                        window.showToast(data.message || '连接成功', 'success');
                    } else {
                        window.showToast((data && data.message) || '连接失败', 'error');
                    }
                }).catch(function () {
                    window.showToast('测试请求失败，请重试', 'error');
                }).then(function () {
                    testBtn.disabled = false;
                    testBtn.textContent = oldText;
                });
            });
        }
    });
