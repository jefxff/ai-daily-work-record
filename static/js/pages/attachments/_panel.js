/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-attachments-_panel" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_attachments__panel = (function () {
  var el = document.getElementById('pageData-attachments-_panel');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

(function () {
    var btn = document.getElementById('attUploadBtn');
    var input = document.getElementById('attFileInput');
    var cat = document.getElementById('attCategory');
    var names = document.getElementById('attFileNames');
    if (!btn || !input) return;

    // 自定义选文件按钮：回显已选文件名
    input.addEventListener('change', function () {
        if (!names) return;
        if (!input.files.length) {
            names.textContent = '未选择文件';
            names.classList.remove('has-files');
        } else if (input.files.length === 1) {
            names.textContent = input.files[0].name;
            names.classList.add('has-files');
        } else {
            names.textContent = '已选 ' + input.files.length + ' 个文件';
            names.classList.add('has-files');
        }
    });

    btn.addEventListener('click', function () {
        if (!input.files.length) {
            if (window.showToast) window.showToast('请先选择要上传的文件', 'error');
            return;
        }
        var fd = new FormData();
        for (var i = 0; i < input.files.length; i++) fd.append('files', input.files[i]);
        if (cat && cat.value) fd.append('category', cat.value);
        btn.disabled = true;
        fetch(D_attachments__panel.urls['u1'], {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': getCsrfToken() },
            body: fd
        }).then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
          .then(function (res) {
              var j = res.j || {};
              if (window.showToast) window.showToast(j.message || (j.ok ? '上传成功' : '上传失败'), j.ok ? 'success' : 'error');
              if (j.ok) { setTimeout(function () { window.location.reload(); }, 600); }
              else { btn.disabled = false; }
          })
          .catch(function () {
              if (window.showToast) window.showToast('上传请求失败，请重试', 'error');
              btn.disabled = false;
          });
    });

    // 删除：AJAX 成功后移除对应行并更新计数
    document.querySelectorAll('.att-del-btn').forEach(function (b) {
        b.addEventListener('click', function () {
            if (!window.confirm('确认删除附件「' + (b.dataset.name || '') + '」？删除后不可恢复。')) return;
            fetch(b.dataset.delUrl, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': getCsrfToken() }
            }).then(function (r) { return r.json(); })
              .then(function (j) {
                  if (j && j.ok) {
                      var row = document.getElementById('att-row-' + b.dataset.pk);
                      if (row) row.remove();
                      var badge = document.getElementById('attCount');
                      if (badge) {
                          var n = parseInt(badge.textContent, 10) || 0;
                          badge.textContent = Math.max(0, n - 1);
                      }
                      if (window.showToast) window.showToast('附件已删除', 'success');
                  } else if (window.showToast) {
                      window.showToast((j && j.message) || '删除失败', 'error');
                  }
              })
              .catch(function () {
                  if (window.showToast) window.showToast('删除请求失败，请重试', 'error');
              });
        });
    });
})();
