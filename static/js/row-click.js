/* ==========================================================================
   行点击进详情（渐进增强）
   --------------------------------------------------------------------------
   启用方式：模板在数据行上声明 <tr data-href="{% url 'xxx_detail' o.pk %}">。

   为什么用显式 data-href 而不是"全站扫描详情链接"：
   - 项目里的详情地址有三种写法：/daily_record/detail/1/、/samples/1/、
     /archives/1/，按 "/detail/" 推断会漏掉后两种；
   - 隐式推断会让 56 个表格页同时改变行为，包括弹窗内表格、无详情页的统计表。

   排除项（点这些不跳转）：
   链接、按钮、表单控件、label、details/summary（table-tools.js 的「更多」菜单）、
   [data-row-toggle]（客诉列表的展开行）、[data-no-row-click]（模板自选）。
   ========================================================================== */
(function () {
  'use strict';

  var SKIP = [
    'a', 'button', 'input', 'select', 'textarea', 'label',
    'summary', 'details', 'form',
    '[data-row-toggle]', '[data-no-row-click]', '.act-more', '.col-settings-panel'
  ].join(',');

  function targetRow(el) {
    return el && el.closest ? el.closest('tr[data-href]') : null;
  }

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var row = targetRow(e.target);
    if (!row || e.target.closest(SKIP)) return;
    // 正在选文字复制时不要跳走
    var sel = window.getSelection && String(window.getSelection());
    if (sel && sel.trim()) return;
    window.location.assign(row.dataset.href);
  });

  // 键盘可达：Tab 到行（tr[tabindex] 或行内按钮）后按 Enter
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var row = targetRow(document.activeElement);
    if (!row) return;
    if (document.activeElement !== row && document.activeElement.closest(SKIP)) return;
    e.preventDefault();
    window.location.assign(row.dataset.href);
  });

  // 可点击的行进入 Tab 序列，否则键盘用户只能用鼠标
  Array.prototype.forEach.call(document.querySelectorAll('tr[data-href]'), function (row) {
    if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', '0');
    if (!row.hasAttribute('aria-label')) {
      var first = row.querySelector('td');
      var text = first ? (first.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (text) row.setAttribute('aria-label', '打开 ' + text.slice(0, 30));
    }
  });
})();
