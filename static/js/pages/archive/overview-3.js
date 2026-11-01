/* 台账排序弹窗：置顶/上移/下移，成功后同步移动弹窗行与总览页卡片 */
(function () {
    var backdrop = document.getElementById('ledgerSortBackdrop');
    var openBtn = document.getElementById('btnLedgerSort');
    if (!backdrop || !openBtn) return;

    function moveEl(el, direction, selector) {
        var parent = el.parentNode;
        if (direction === 'top') {
            var first = parent.querySelector(selector);
            if (first && first !== el) parent.insertBefore(el, first);
            else return false;
        } else if (direction === 'up') {
            var prev = el.previousElementSibling;
            while (prev && !prev.matches(selector)) prev = prev.previousElementSibling;
            if (prev) parent.insertBefore(el, prev);
            else return false;
        } else {
            var next = el.nextElementSibling;
            while (next && !next.matches(selector)) next = next.nextElementSibling;
            if (next) parent.insertBefore(next, el);
            else return false;
        }
        return true;
    }
    function toast(msg, type) {
        if (window.showToast) window.showToast(msg, type);
    }

    openBtn.addEventListener('click', function () { backdrop.classList.add('show'); });
    document.getElementById('ledgerSortClose').addEventListener('click', function () { backdrop.classList.remove('show'); });
    backdrop.addEventListener('click', function (e) {
        if (e.target === backdrop) backdrop.classList.remove('show');
    });

    backdrop.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-ledger-move]');
        if (!btn) return;
        var row = btn.closest('.ls-row');
        var direction = btn.getAttribute('data-ledger-move');
        if (!row || !row.dataset.ledgerId) return;
        var ledgerId = row.dataset.ledgerId;
        btn.disabled = true;
        fetch(row.getAttribute('data-move-url'), {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            body: 'direction=' + direction
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data.ok) throw new Error(data.message || '排序失败');
            var movedRow = moveEl(row, direction, '.ls-row[data-ledger-id]');
            var card = document.querySelector('.lg-card[data-ledger-id="' + ledgerId + '"]');
            var movedCard = card ? moveEl(card, direction, '.lg-card[data-ledger-id]') : true;
            if (!movedRow && !movedCard) toast(direction === 'down' ? '已是最后' : '已在最前', 'info');
            else toast('排序已更新', 'success');
        })
        .catch(function (err) { toast(err.message || '排序失败，请重试', 'error'); })
        .finally(function () { btn.disabled = false; });
    });
})();
