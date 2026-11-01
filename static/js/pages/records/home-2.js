// 未完成记录：在当前面板内展开全部记录（AJAX 追加），再次点击收起
(function () {
    var toggle = document.getElementById('homeDailyToggle');
    var list = document.getElementById('homeDailyRows');
    if (!toggle || !list) return;

    var url = toggle.getAttribute('data-url');
    var initial = parseInt(toggle.getAttribute('data-initial'), 10) || 0;
    var defaultLabel = toggle.textContent;
    var expanded = false;
    var loading = false;
    var appended = [];

    toggle.addEventListener('click', function (e) {
        e.preventDefault();
        if (loading) return;

        if (expanded) {
            appended.forEach(function (li) { li.remove(); });
            appended = [];
            expanded = false;
            toggle.setAttribute('aria-expanded', 'false');
            toggle.textContent = defaultLabel;
            return;
        }

        loading = true;
        toggle.textContent = '加载中…';
        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store' })
            .then(function (resp) {
                if (!resp.ok) throw new Error('bad status');
                return resp.json();
            })
            .then(function (data) {
                if (!data || !data.ok || !data.html) throw new Error('bad payload');
                var holder = document.createElement('div');
                holder.innerHTML = data.html;
                var items = Array.prototype.slice.call(holder.querySelectorAll('li.home-rows__item'));
                items.slice(initial).forEach(function (li) {
                    list.appendChild(li);
                    appended.push(li);
                });
                expanded = true;
                toggle.setAttribute('aria-expanded', 'true');
                toggle.textContent = '收起 ↑';
            })
            .catch(function () {
                toggle.textContent = defaultLabel;
                if (window.showToast) window.showToast('加载未完成记录失败，请重试', 'error');
            })
            .then(function () { loading = false; });
    });
})();
