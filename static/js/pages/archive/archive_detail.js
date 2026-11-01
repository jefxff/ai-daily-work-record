/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-archive-archive_detail" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_archive_archive_detail = (function () {
  var el = document.getElementById('pageData-archive-archive_detail');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

/* 附件上传：FormData 多文件 AJAX 提交，成功后刷新展示 */
(function () {
    var btn = document.getElementById('btnUploadFiles');
    var input = document.getElementById('arcUploadInput');
    if (!btn || !input) return;
    btn.addEventListener('click', function () {
        if (!input.files.length) {
            if (window.showToast) window.showToast('请先选择要上传的文件', 'error');
            return;
        }
        var fd = new FormData();
        for (var i = 0; i < input.files.length; i++) fd.append('files', input.files[i]);
        btn.disabled = true;
        fetch(D_archive_archive_detail.urls['u1'], {
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
})();
