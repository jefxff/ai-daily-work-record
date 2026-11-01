(function () {
    setTimeout(function () {
        var hit = document.querySelector('.lg-row-hit');
        if (hit) hit.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 300);
})();
