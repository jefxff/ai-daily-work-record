(function() {
    var customerType = document.getElementById('id_customer_type');
    var assessmentSection = document.getElementById('assessment-section');
    var assessmentCheckbox = document.getElementById('id_has_assessment_period');
    var assessmentFields = document.getElementById('assessment-fields');

    function toggleSection() {
        // 仅普通客户显示业绩考核模块
        if (customerType && customerType.value === '普通客户') {
            assessmentSection.style.display = '';
        } else {
            assessmentSection.style.display = 'none';
            assessmentCheckbox.checked = false;
            assessmentFields.style.display = 'none';
        }
    }

    function toggleFields() {
        assessmentFields.style.display = assessmentCheckbox.checked ? '' : 'none';
    }

    if (customerType) {
        customerType.addEventListener('change', toggleSection);
        toggleSection();
    }
    if (assessmentCheckbox) {
        assessmentCheckbox.addEventListener('change', toggleFields);
        toggleFields();
    }
})();
