document.addEventListener('DOMContentLoaded', function () {
    // 小批量结果为「不通过」时显示终止原因（必填）
    var result = document.getElementById('id_batch_result');
    var group = document.getElementById('terminateReasonGroup');
    function toggle() {
        if (result && group) group.style.display = result.value === 'fail' ? '' : 'none';
    }
    if (result) { result.addEventListener('change', toggle); toggle(); }
});
