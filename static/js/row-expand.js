/* ==========================================================================
   行内展开（列表页首列小三角 → 展开长文本详情行）
   --------------------------------------------------------------------------
   结构约定：数据行首列放 [data-row-toggle] 按钮，紧跟其后的兄弟行是
   <tr class="row-detail" hidden>。按钮只做开合，不做任何业务读写。
   aria-expanded / aria-controls 同步，键盘可达（原生 button）。
   ========================================================================== */
/* 行内展开（列表页「首列小三角 → 展开长文本详情行」）
   结构约定：数据行首列放 [data-row-toggle] 按钮，紧跟其后的兄弟行是
   <tr class="row-detail" hidden>。按钮只做开合，不做任何业务读写。
   aria-expanded / aria-controls 同步，键盘可达（原生 button）。 */
(function () {
    var btns = document.querySelectorAll('[data-row-toggle]');
    if (!btns.length) return;
    Array.prototype.forEach.call(btns, function (btn) {
        var tr = btn.closest('tr');
        var detail = tr && tr.nextElementSibling;
        if (!detail || !detail.classList.contains('row-detail')) return;
        if (detail.id) btn.setAttribute('aria-controls', detail.id);
        btn.setAttribute('aria-expanded', 'false');
        if (!btn.getAttribute('aria-label')) btn.setAttribute('aria-label', '展开详情');
        if (!btn.getAttribute('title')) btn.setAttribute('title', '展开详情');
        btn.addEventListener('click', function () {
            var open = detail.hidden;
            detail.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            btn.setAttribute('aria-label', open ? '收起详情' : '展开详情');
            btn.setAttribute('title', open ? '收起详情' : '展开详情');
        });
    });
})();
