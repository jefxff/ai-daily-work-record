document.addEventListener('DOMContentLoaded', function () {
    // 评审结论为「不通过」时显示终止原因（必填）
    var conclusion = document.getElementById('id_review_conclusion');
    var group = document.getElementById('terminateReasonGroup');
    function toggle() {
        if (conclusion && group) group.style.display = conclusion.value === 'fail' ? '' : 'none';
    }
    if (conclusion) { conclusion.addEventListener('change', toggle); toggle(); }
});
