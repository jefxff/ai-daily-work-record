// 阅读进度条宽度（data 属性传入，避免 style 内嵌模板标签）
    document.querySelectorAll(".read-progress > div[data-w]").forEach(function (bar) {
        bar.style.width = (parseInt(bar.dataset.w, 10) || 0) + "%";
    });
