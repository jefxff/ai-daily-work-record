document.addEventListener('DOMContentLoaded', function () {
    var el = document.getElementById('djangoMessages');
    if (!el) return;
    var items;
    try {
        items = JSON.parse(el.getAttribute('data-items') || '[]');
    } catch (e) {
        items = [];
    }
    items.forEach(function (item) {
        if (window.showToast) {
            window.showToast(item.text, item.level);
        }
    });
});
