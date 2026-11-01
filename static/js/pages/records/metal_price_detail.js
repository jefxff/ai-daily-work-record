/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-records-metal_price_detail" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_records_metal_price_detail = (function () {
  var el = document.getElementById('pageData-records-metal_price_detail');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

// 刷新价格功能（保持原逻辑，仅挂到新按钮位置）
    const refreshPriceBtnEl = document.getElementById('refreshPriceBtn');
    if (refreshPriceBtnEl) refreshPriceBtnEl.addEventListener('click', function() {
        const btn = this;
        const messageDiv = document.getElementById('refreshMessage');

        btn.classList.add('loading');
        btn.disabled = true;

        fetch(D_records_metal_price_detail.urls['u1'], {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json'
            }
        })
        .then(response => response.json())
        .then(data => {
            btn.classList.remove('loading');
            btn.disabled = false;

            if (!messageDiv) { if (data.success) window.location.reload(); return; }
            messageDiv.style.display = 'block';
            if (data.success) {
                messageDiv.className = 'alert alert-success';
                messageDiv.textContent = data.message;
                setTimeout(() => { window.location.reload(); }, 1500);
            } else {
                messageDiv.className = 'alert alert-error';
                messageDiv.textContent = data.message;
            }
            setTimeout(() => { messageDiv.style.display = 'none'; }, 5000);
        })
        .catch(error => {
            btn.classList.remove('loading');
            btn.disabled = false;

            if (messageDiv) {
                messageDiv.style.display = 'block';
                messageDiv.className = 'alert alert-error';
                messageDiv.textContent = '刷新价格时出错，请稍后重试';
                setTimeout(() => { messageDiv.style.display = 'none'; }, 5000);
            }
            console.error('刷新价格错误:', error);
        });
    });

    // —— 价格走势图：主题感知 + 期初价参考线 ——
    // 颜色全部在 build() 时从 CSS 变量取值：明暗切换（MutationObserver 监听
    // <html data-theme>）时销毁重建，图表永远和页面主题同色，不再出现
    // 「暗色主题下图例文字仍是近黑色」的硬编码问题。
    (function () {
        const el = document.getElementById('priceChart');
        const labels = JSON.parse(document.getElementById('mpChartLabels').textContent);
        const data = JSON.parse(document.getElementById('mpChartData').textContent);
        if (!el || typeof Chart === 'undefined' || !data.length) return;

        let chart = null;

        function cssVar(name, fallback) {
            const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
            return v || fallback;
        }

        function build() {
            const dark = document.documentElement.getAttribute('data-theme') === 'dark';
            const primary = cssVar('--c-primary', '#096dd9');
            const fill = cssVar('--c-primary-subtle', 'rgba(24,144,255,0.15)');
            const grid = cssVar('--c-divider', '#f0f0f0');
            const tick = cssVar('--c-text-tertiary', '#8c8c8c');
            const mono = cssVar('--font-mono', 'monospace');

            if (chart) chart.destroy();
            chart = new Chart(el.getContext('2d'), {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: D_records_metal_price_detail.vars['v1'],
                            data: data,
                            borderColor: primary,
                            backgroundColor: fill,
                            borderWidth: 2.5,
                            pointRadius: 0,
                            pointHoverRadius: 5,
                            pointHoverBackgroundColor: primary,
                            pointHoverBorderColor: cssVar('--c-surface', '#fff'),
                            pointHoverBorderWidth: 2,
                            fill: true,
                            tension: 0.35
                        },
                        {
                            // 期初价参考线：一条水平虚线，30 天里是涨是跌一眼可判
                            label: '期初价',
                            data: Array(labels.length).fill(data[0]),
                            borderColor: tick,
                            borderDash: [6, 6],
                            borderWidth: 1,
                            pointRadius: 0,
                            pointHoverRadius: 0,
                            fill: false,
                            tension: 0
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },   // 图例改为标题行的 HTML 版本，可随主题换色
                        tooltip: {
                            backgroundColor: cssVar('--c-surface-inverse', '#1f2329'),
                            titleColor: cssVar('--c-text-inverse', '#fff'),
                            bodyColor: cssVar('--c-text-inverse', '#fff'),
                            borderColor: cssVar('--c-border', '#333'),
                            borderWidth: 1,
                            padding: 10,
                            titleFont: { family: mono },
                            bodyFont: { family: mono, size: 13 },
                            callbacks: {
                                title: (items) => items[0].label,
                                label: (ctx) =>
                                    ctx.datasetIndex === 0
                                        ? ' 价格 ' + ctx.parsed.y.toLocaleString() + ' 元'
                                        : ' 期初 ' + ctx.parsed.y.toLocaleString() + ' 元'
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: grid },
                            ticks: { color: tick, font: { family: mono, size: 11 }, maxRotation: 0, autoSkip: true }
                        },
                        y: {
                            grid: { color: grid },
                            ticks: {
                                color: tick,
                                font: { family: mono, size: 11 },
                                callback: (value) => value.toLocaleString()
                            }
                        }
                    }
                }
            });
        }

        build();

        // 明暗切换时重建图表（shell.js 直接改 data-theme，不发事件，故用观察器）
        new MutationObserver(build).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['data-theme']
        });
    })();
