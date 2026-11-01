// 显示加载状态
    function showLoading() {
        const loadingOverlay = document.getElementById('loadingOverlay');
        if (loadingOverlay) {
            loadingOverlay.style.display = 'flex';

            // 3秒后自动隐藏加载状态
            setTimeout(function() {
                loadingOverlay.style.display = 'none';
                window.showToast('下载开始，文件正在生成...', 'success');
            }, 1000);
        }
    }

    // CSP：不在模板内写 onclick，统一事件委托
    document.addEventListener('click', function (e) {
        if (e.target.closest('[data-export-loading]')) showLoading();
    });
