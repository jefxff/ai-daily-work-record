/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-records-_metal_price_detail_modal" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_records__metal_price_detail_modal = (function () {
  var el = document.getElementById('pageData-records-_metal_price_detail_modal');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

(function(){
    var el = document.getElementById('mpPriceChart');
    if (!el || typeof Chart === 'undefined') return;
    var labels = JSON.parse(document.getElementById('mpChartLabels').textContent);
    var data = JSON.parse(document.getElementById('mpChartData').textContent);
    new Chart(el.getContext('2d'), {
        type: 'line',
        data: { labels: labels, datasets: [{ label: D_records__metal_price_detail_modal.vars['v1'], data: data, borderColor: '#1890ff', backgroundColor: 'rgba(24, 144, 255, 0.15)', borderWidth: 2, pointRadius: 3, fill: true, tension: 0.4 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true } }, scales: { x: { ticks: { color: '#8c8c8c' } }, y: { ticks: { color: '#8c8c8c' } } } }
    });
})();
