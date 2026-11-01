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
        showToast(item.text, item.level);
    });
});
