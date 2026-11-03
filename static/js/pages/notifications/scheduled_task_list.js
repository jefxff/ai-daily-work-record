/* 定时任务配置弹窗：从 notification_list.js 拆出（页面已独立为 /notifications/tasks/）。
   逻辑与拆分前完全一致，仅换了承载页面。 */
// 定时任务配置弹窗（仅超管）
    (function () {
        var backdrop = document.getElementById('schedConfigBackdrop');
        if (!backdrop) return;
        var form = document.getElementById('schedConfigForm');
        var nameEl = document.getElementById('schedConfigName');
        var enabledEl = document.getElementById('schedEnabled');
        var triggerEl = document.getElementById('schedTriggerType');
        var runTimeField = document.getElementById('schedRunTimeField');
        var intervalField = document.getElementById('schedIntervalField');
        var runTimeEl = document.getElementById('schedRunTime');
        var intervalEl = document.getElementById('schedInterval');
        var paramDays = document.getElementById('schedParamDays');
        var paramSkip = document.getElementById('schedParamSkipHistory');
        var paramPurge = document.getElementById('schedParamPurgeDays');
        var paramCheck = document.getElementById('schedParamCheck');
        var daysEl = document.getElementById('schedDays');
        var skipEl = document.getElementById('schedSkipHistory');
        var purgeEl = document.getElementById('schedPurgeDays');
        var checkEl = document.getElementById('schedCheck');

        function refreshTriggerFields() {
            var isDaily = triggerEl.value === 'daily';
            runTimeField.style.display = isDaily ? '' : 'none';
            intervalField.style.display = isDaily ? 'none' : '';
        }

        // 按任务编码显示对应的参数项
        function setParamFields(code) {
            paramDays.style.display = (code === 'check_expiry_notices' || code === 'check_insurance_renewal') ? '' : 'none';
            paramSkip.style.display = code === 'check_daily_incomplete' ? '' : 'none';
            paramPurge.style.display = code === 'notify_expire' ? '' : 'none';
            paramCheck.style.display = code === 'notify_stats' ? '' : 'none';
        }

        function openTaskConfig(el) {
            form.setAttribute('action', el.getAttribute('data-url'));
            nameEl.textContent = el.getAttribute('data-name') + '（' + el.getAttribute('data-code') + '）';
            enabledEl.checked = !!el.getAttribute('data-enabled');
            triggerEl.value = el.getAttribute('data-trigger') || 'daily';
            runTimeEl.value = el.getAttribute('data-run-time') || '';
            intervalEl.value = el.getAttribute('data-interval') || '';
            daysEl.value = el.getAttribute('data-days') || '';
            skipEl.checked = el.getAttribute('data-skip-history') === '1';
            purgeEl.value = el.getAttribute('data-purge-days') || '';
            checkEl.checked = el.getAttribute('data-check') === '1';
            setParamFields(el.getAttribute('data-code'));
            refreshTriggerFields();
            backdrop.classList.add('show');
        }

        // CSP：不在模板内写 onclick，统一事件委托
        document.addEventListener('click', function (e) {
            var trigger = e.target.closest('[data-task-config]');
            if (trigger) openTaskConfig(trigger);
        });

        triggerEl.addEventListener('change', refreshTriggerFields);
        document.getElementById('schedConfigClose').addEventListener('click', function () { backdrop.classList.remove('show'); });
        document.getElementById('schedConfigCancel').addEventListener('click', function () { backdrop.classList.remove('show'); });
        backdrop.addEventListener('click', function (e) { if (e.target === backdrop) backdrop.classList.remove('show'); });
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape') backdrop.classList.remove('show'); });
    })();
