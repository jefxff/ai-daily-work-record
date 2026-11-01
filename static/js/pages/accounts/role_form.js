var checkAll = document.getElementById('checkAll');
    var uncheckAll = document.getElementById('uncheckAll');
    if (checkAll) checkAll.addEventListener('click', function () {
        document.querySelectorAll('.perm-cb').forEach(function (cb) { cb.checked = true; });
    });
    if (uncheckAll) uncheckAll.addEventListener('click', function () {
        document.querySelectorAll('.perm-cb').forEach(function (cb) { cb.checked = false; });
    });
