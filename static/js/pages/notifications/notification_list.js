/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-notifications-notification_list" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_notifications_notification_list = (function () {
  var el = document.getElementById('pageData-notifications-notification_list');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

(function () {
        var backdrop = document.getElementById('notifDetailBackdrop');
        if (!backdrop) return;
        var titleEl = document.getElementById('notifDetailTitle');
        var metaEl = document.getElementById('notifDetailMeta');
        var contentEl = document.getElementById('notifDetailContent');
        var goBtn = document.getElementById('notifDetailGo');
        var csrf = getCsrfToken();
        var unreadCount = parseInt(D_notifications_notification_list.vars['v1'], 10) || 0;
        var hintEl = document.querySelector('.total-hint');

        function setText(el, text) { el.textContent = text == null ? '' : String(text); }

        function addChip(text, cls) {
            var chip = document.createElement('span');
            chip.className = 'meta-chip' + (cls ? ' ' + cls : '');
            setText(chip, text);
            metaEl.appendChild(chip);
        }

        function updateHint() {
            if (hintEl) hintEl.textContent = '共 ' + D_notifications_notification_list.vars['v2'] + ' 条 · 未读 ' + unreadCount + ' 条';
        }

        function closeModal() { backdrop.classList.remove('show'); }

        // 「查看」：打开详情弹窗，未读则标记已读（完整消费响应体，避免 ERR_ABORTED）
        function openNotifDetail(event, el) {
            event.preventDefault();
            var row = el.closest('.notif-item');
            setText(titleEl, el.getAttribute('data-title'));
            setText(contentEl, el.getAttribute('data-content'));
            metaEl.innerHTML = '';
            if (parseInt(el.getAttribute('data-agg'), 10) > 1) {
                addChip('合并 ' + el.getAttribute('data-agg') + ' 条');
            }
            addChip(el.getAttribute('data-category'));
            addChip(el.getAttribute('data-priority'), el.getAttribute('data-priority') === '高' ? 'high' : '');
            var statusChip = document.createElement('span');
            statusChip.className = 'status-badge ' + (el.getAttribute('data-status-badge') || 'default');
            setText(statusChip, el.getAttribute('data-status'));
            metaEl.appendChild(statusChip);
            addChip(el.getAttribute('data-created'));
            var link = el.getAttribute('data-link');
            if (link) { goBtn.setAttribute('href', link); goBtn.hidden = false; }
            else { goBtn.hidden = true; }
            backdrop.classList.add('show');

            if (row && row.classList.contains('unread')) {
                fetch(el.getAttribute('data-read-url'), {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest' },
                    body: new URLSearchParams({})
                }).then(function (r) { return r.text(); }).catch(function () {}).then(function () {
                    row.classList.remove('unread');
                    var dot = row.querySelector('.notif-dot');
                    if (dot) dot.classList.add('read');
                    var form = row.querySelector('.notif-read-form');
                    if (form) form.remove();
                    unreadCount = Math.max(0, unreadCount - 1);
                    updateHint();
                });
            }
        }

        // CSP：不在模板内写 onclick，统一事件委托
        document.addEventListener('click', function (e) {
            var trigger = e.target.closest('[data-notif-detail]');
            if (trigger) openNotifDetail(e, trigger);
        });

        document.getElementById('notifDetailClose').addEventListener('click', closeModal);
        document.getElementById('notifDetailCloseBtn').addEventListener('click', closeModal);
        backdrop.addEventListener('click', function (e) { if (e.target === backdrop) closeModal(); });
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });
        updateHint();
    })();

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
