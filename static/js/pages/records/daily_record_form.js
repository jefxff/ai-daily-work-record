/* ============================================================================
   每日记录表单：状态 ↔ 进度联动 / 字数计数 / 进度快捷档位
   ----------------------------------------------------------------------------
   原来这段逻辑在 daily_record_form.html 和 daily_record_form_modal.html 里各存
   一份（约 120 行 × 2），改一处必须记得改另一处，两边行为已经出现过细微差异。
   现在只有这一份：

   - 独立页：<script defer> 在解析完成后执行，init(document) 直接命中表单；
   - 列表页弹窗：shell.js 注入 HTML 后会把 script 标签重新创建一次（见
     injectForm 的「执行注入的 script 标签」），本文件随之执行，此时表单已在
     DOM 里，同一个 init 立即绑定。

   幂等保护：表单打 dataset.drReady 标记，重复执行不会重复绑定。
   ========================================================================== */
(function () {
    'use strict';

    var FORM_SELECTOR = 'form.dr-form';

    function init(scope) {
        var form = (scope || document).querySelector(FORM_SELECTOR);
        if (!form || form.dataset.drReady) return;
        form.dataset.drReady = '1';

        var $ = function (sel) { return form.querySelector(sel); };

        var statusSelect = $('#status');
        var progressNum = $('#progress');
        var progressSlider = $('#progressSlider');
        var charCount = $('#charCount');
        var workContent = $('#work_content');
        var progressSection = progressNum ? progressNum.closest('.dr-form-section') : null;
        var progressRow = form.querySelector('.dr-progress-row');

        // ---------- 字数计数 ----------
        if (workContent && charCount) {
            var syncCount = function () { charCount.textContent = workContent.value.length; };
            syncCount();
            workContent.addEventListener('input', syncCount);
        }

        // ---------- 状态药丸 ----------
        var pills = Array.prototype.slice.call(form.querySelectorAll('.dr-status-pill'));

        function activatePill(status) {
            pills.forEach(function (btn) {
                var on = btn.dataset.status === status;
                btn.classList.toggle('is-active', on);
                btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
        }

        // 状态与进度互相推导：完成=100，未完成=0，进行中保留原值（0/100 时落到 50）。
        // 用标记避免两条链路互相触发成死循环。
        var fromStatus = false;

        function clamp(val) {
            var n = parseInt(val, 10);
            if (isNaN(n)) return 0;
            return Math.max(0, Math.min(100, n));
        }

        function paint(val, status) {
            if (progressSection) {
                progressSection.classList.toggle('is-completed', status === '完成' || val >= 100);
                progressSection.classList.toggle('is-unfinished', status === '未完成' && val === 0);
            }
            if (progressNum) progressNum.setAttribute('aria-valuetext', val + '%');
        }

        // 滑块只在「进行中」出现：未完成/完成两态的进度由状态唯一确定，
        // 再给一个可拖的滑杆就是第三重入口（规划书 add-2）
        function syncProgressMode(status) {
            var editing = status === '进行中';
            if (progressSlider) {
                progressSlider.hidden = !editing;
                progressSlider.disabled = !editing;
            }
            if (progressNum) {
                progressNum.readOnly = !editing;
                progressNum.classList.toggle('is-locked', !editing);
            }
            if (progressRow) progressRow.classList.toggle('is-locked', !editing);
        }

        function applyProgress(val, opts) {
            val = clamp(val);
            if (progressNum) progressNum.value = val;
            if (progressSlider) progressSlider.value = val;
            var status = statusSelect ? statusSelect.value : '未完成';
            if (!(opts && opts.keepStatus)) {
                status = val >= 100 ? '完成' : (val <= 0 ? '未完成' : '进行中');
                if (statusSelect) statusSelect.value = status;
                activatePill(status);
            }
            paint(val, status);
        }

        function applyStatus(status) {
            if (statusSelect) statusSelect.value = status;
            activatePill(status);
            var next = progressNum ? progressNum.value : 0;
            if (status === '完成') next = 100;
            else if (status === '未完成') next = 0;
            else {
                var cur = clamp(progressNum ? progressNum.value : 0);
                next = (cur === 0 || cur === 100) ? 50 : cur;
            }
            fromStatus = true;
            applyProgress(next, { keepStatus: true });
            paint(clamp(next), status);
            fromStatus = false;
            syncProgressMode(status);
        }

        pills.forEach(function (btn) {
            btn.addEventListener('click', function () { applyStatus(btn.dataset.status); });
        });

        if (statusSelect) {
            statusSelect.addEventListener('change', function () { applyStatus(statusSelect.value); });
        }

        if (progressNum) {
            progressNum.addEventListener('input', function () {
                if (fromStatus) return;
                applyProgress(clamp(progressNum.value));
            });
            progressNum.addEventListener('blur', function () {
                progressNum.value = clamp(progressNum.value);
                applyProgress(progressNum.value, { keepStatus: true });
            });
        }

        if (progressSlider) {
            progressSlider.addEventListener('input', function () {
                if (fromStatus) return;
                applyProgress(progressSlider.value);
            });
            // 拖动过程中不收起滑杆（否则 pointerup 会丢），松手后再同步显隐
            progressSlider.addEventListener('change', function () {
                if (statusSelect) syncProgressMode(statusSelect.value);
            });
        }

        // ---------- 知识库范围：勾选后才展开 ----------
        var kbToggle = $('#kb_enabled');
        var kbScope = $('#kb_sync_scope');
        if (kbToggle && kbScope) {
            var syncScope = function () { kbScope.hidden = !kbToggle.checked; };
            syncScope();
            kbToggle.addEventListener('change', syncScope);
        }

        // ---------- 初始态 ----------
        var initStatus = statusSelect ? statusSelect.value : '未完成';
        activatePill(initStatus);
        paint(clamp(progressNum ? progressNum.value : 0), initStatus);
        syncProgressMode(initStatus);
    }

    init(document);
    document.addEventListener('DOMContentLoaded', function () { init(document); });
})();
