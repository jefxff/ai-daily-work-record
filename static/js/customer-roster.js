/* 历史客户自动填（客户账期表单）。
 *
 * 客户名称输入框挂了 datalist（#customer-roster）；input 精确匹配名册后
 * 自动填「客户细分」「业务员」，仍可手改。名册数据由模板用
 * json_script 写入 #customer-roster-data，整页与 AJAX 弹窗两条渲染路径同传。
 * 事件挂在 document 上：弹窗内容是 innerHTML 注入的，委托监听才能覆盖。
 */
(function () {
  'use strict';

  function readRoster() {
    var node = document.getElementById('customer-roster-data');
    if (!node) return [];
    try { return JSON.parse(node.textContent) || []; } catch (err) { return []; }
  }

  document.addEventListener('input', function (e) {
    var target = e.target;
    if (!target || target.id !== 'id_customer_name' || !target.getAttribute('list')) return;
    var roster = readRoster();
    if (!roster.length) return;
    var name = (target.value || '').trim();
    var hit = null;
    for (var i = 0; i < roster.length; i++) {
      if (roster[i].customer_name === name) { hit = roster[i]; break; }
    }
    if (!hit) return;
    var seg = document.getElementById('id_customer_segment');
    if (seg && hit.customer_segment) {
      for (var j = 0; j < seg.options.length; j++) {
        if (seg.options[j].value === hit.customer_segment) { seg.value = hit.customer_segment; break; }
      }
    }
    var sales = document.getElementById('id_salesperson');
    if (sales && hit.salesperson) sales.value = hit.salesperson;
  });
})();
