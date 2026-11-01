/* ==========================================================================
   表单校验与提交锁（渐进增强，零模板结构改动）
   --------------------------------------------------------------------------
   启用方式：给 <form> 加 data-validate="1"。
   为什么用白名单而不是"全站接管"：
   - 全站有 117 个含表单的模板、119 处 POST，其中大量是「确认删除」「退出登录」
     这类只有一个按钮的表单，套上校验与"提交中…"反而添乱；
   - 筛选表单是 GET（method=get），校验会把空搜索词当成错误。
   因此只有显式声明的录入表单参与，其余行为完全不变。

   错误提示复用项目原有的 .form-text 结构（不加 Bootstrap 的 invalid-feedback，
   项目里没有那个容器，加了也不会显示任何文案）。
   ========================================================================== */
(function () {
  'use strict';

  var REPEAT_WINDOW = 15000;   // 兜底：网络异常时 15 秒后恢复按钮

  /* 锁按钮：与 base.html 弹窗表单的"保存中…"保持同一套行为 */
  window.lockSubmit = function (btn) {
    if (!btn || btn.disabled) return;
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '提交中…';
    setTimeout(function () {
      btn.disabled = false;
      btn.textContent = btn.dataset.label || '提交';
    }, REPEAT_WINDOW);
  };

  /* 危险动作（删除/注销）不锁文案：它们通常走确认弹窗，按钮文字变化反而吓人 */
  function isDestructive(btn) {
    return /btn-danger|btn-delete/.test(btn.className || '');
  }

  /* 校验入口：整表单校验失败时聚焦首个非法字段并播报。
     暴露给 shell.js 的弹窗表单复用（弹窗走 fetch，submit 事件被它先接管，
     文档级监听排在后面，必须由弹窗处理器主动调用）。 */
  function validateEntryForm(form) {
    if (!form || form.dataset.validate !== '1') return true;
    if (form.checkValidity()) return true;

    form.classList.add('is-invalid-form');
    var first = form.querySelector(':invalid');
    if (first) {
      first.focus({ preventScroll: true });
      if (first.scrollIntoView) first.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    if (window.showToast) window.showToast('请先补全标记为必填的字段', 'error');
    return false;
  }
  window.validateEntryForm = validateEntryForm;

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.dataset.validate !== '1') return;
    if ((form.method || 'get').toLowerCase() !== 'post') return;

    if (!validateEntryForm(form)) {
      e.preventDefault();
      return;
    }

    var btn = form.querySelector('[type="submit"]');
    if (btn && !isDestructive(btn)) window.lockSubmit(btn);
  });

  /* 用户改动过表单后撤掉整体错误态，避免一边填一边满屏红框 */
  document.addEventListener('input', function (e) {
    var form = e.target.form;
    if (form && form.classList.contains('is-invalid-form') && e.target.checkValidity()) {
      form.classList.remove('is-invalid-form');
    }
  }, true);
})();
