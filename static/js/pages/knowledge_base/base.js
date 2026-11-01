// 右侧栏无内容时隐藏（并取消主区右边距），避免出现空白圆角框
    (function() {
        var sidebar = document.querySelector('.fixed-right-sidebar');
        if (sidebar && sidebar.children.length === 0) {
            sidebar.style.display = 'none';
            var wrapper = document.querySelector('.knowledge-wrapper');
            if (wrapper) wrapper.classList.remove('knowledge-main-with-right');
        }
    })();

    // 分类筛选树：caret 折叠/展开（服务端直出，静态绑定即可）。
    // 必须放在基座而不是列表页的 extra_js——子模板的 extra_js 会整块覆盖
    // 基座同名 block，放在列表页里详情页就拿不到（2026-10-22：详情页
    // 右栏 caret 曾是一排死按钮）。
    (function() {
        document.querySelectorAll('.kb-cat-tree .kb-cat-caret:not(.kb-cat-caret-spacer)').forEach(function (caret) {
            caret.addEventListener('click', function () {
                var open = caret.classList.toggle('is-open');
                caret.setAttribute('aria-expanded', open ? 'true' : 'false');
                var row = caret.closest('.kb-cat-row');
                var node = row && row.closest('.kb-cat-node');
                var kids = node && node.querySelector(':scope > .kb-cat-children');
                if (kids) kids.hidden = !open;
            });
        });
    })();
