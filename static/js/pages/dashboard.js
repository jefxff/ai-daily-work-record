/* 数据看板页面脚本（由 dashboard.html 内联脚本抽离，行为保持不变）。 */
document.addEventListener('DOMContentLoaded', function () {
    // 页面加载时设置当前时间
    function setCurrentTime() {
        const updateTime = document.getElementById('updateTime');
        if (updateTime) {
            const now = new Date();
            const formattedTime = now.toLocaleString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }).replace(/\//g, '-');
            updateTime.textContent = formattedTime;
        }
    }

    // 页面加载完成后设置当前时间
    document.addEventListener('DOMContentLoaded', setCurrentTime);

    // 刷新数据功能
    const refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function () {
            // 显示加载状态
            this.textContent = '刷新中...';
            this.disabled = true;

            // 重新加载页面以获取最新数据
            setTimeout(function () {
                location.reload();
            }, 500);
        });
    }

    // 定时刷新时间显示（每秒更新）
    setInterval(setCurrentTime, 1000);

    // 待处理明细已外移至待办工作台（规划书 db-5），看板只展示聚合计数，
    // 原页内筛选按钮与明细展开逻辑随之移除。

    // 查看全部入口已改为跳转待办工作台（见模板底部链接），不再需要本地展开逻辑

    // ===== P5 第二步：图表区（颜色取自设计令牌，换肤自动跟随） =====
    const chartDataEl = document.getElementById('dashboardChartData');
    if (chartDataEl && typeof Chart !== 'undefined') {
        const chartData = JSON.parse(chartDataEl.textContent);
        const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        const cPrimary = cssVar('--c-primary');
        const cText = cssVar('--c-text-secondary');
        const cBorder = cssVar('--c-border');
        // Chart.js 不接受 CSS 变量字符串，这里统一解析为 rgba
        const withAlpha = (color, alpha) => {
            const hex = (color || '').trim();
            if (/^#[0-9a-f]{6}$/i.test(hex)) {
                const r = parseInt(hex.slice(1, 3), 16);
                const g = parseInt(hex.slice(3, 5), 16);
                const b = parseInt(hex.slice(5, 7), 16);
                return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
            }
            return hex;
        };

        new Chart(document.getElementById('chartModules'), {
            type: 'bar',
            data: {
                labels: chartData.module_labels,
                datasets: [{
                    label: '记录数',
                    data: chartData.module_values,
                    backgroundColor: cPrimary,
                    borderRadius: 4
                }]
            },
            options: {
                indexAxis: 'y',
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: cText }, grid: { color: cBorder } },
                    y: { ticks: { color: cText }, grid: { display: false } }
                }
            }
        });

        // 小量级不自欺（规划书 db-4）：日新增 ≤10 条时，面积图会把 7 条
        // 数据撑满 230px 高的画布，视觉上像剧烈波动。改纯点 + 数值标注，
        // 不铺面积；量级起来后（>10）才恢复面积图。数值标注用内联插件画，
        // 不为此引入 chartjs-plugin-datalabels 依赖。
        var trendMax = Math.max.apply(null, (chartData.trend_values || [0]).concat([0]));
        var trendSmall = trendMax <= 10;
        var trendValueLabels = {
            id: 'trendValueLabels',
            font: { size: 10 },
            afterDatasetsDraw: function (chart) {
                var meta = chart.getDatasetMeta(0);
                if (!meta || chart.chartArea.width / meta.data.length < 18) return; // 点太密时标注会互相叠
                var ctx = chart.ctx;
                ctx.save();
                ctx.fillStyle = cText;
                ctx.font = '10px ' + (chart.options.font && chart.options.font.family || 'sans-serif');
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                meta.data.forEach(function (el, i) {
                    var v = chart.data.datasets[0].data[i];
                    if (v > 0) ctx.fillText(String(v), el.x, el.y - 5);
                });
                ctx.restore();
            }
        };
        new Chart(document.getElementById('chartTrend'), {
            type: 'line',
            data: {
                labels: chartData.trend_labels,
                datasets: [{
                    label: '每日记录',
                    data: chartData.trend_values,
                    borderColor: cPrimary,
                    backgroundColor: trendSmall ? cPrimary : withAlpha(cPrimary, 0.13),
                    fill: !trendSmall,
                    tension: 0.3,
                    pointRadius: trendSmall ? 3 : 2,
                    pointHoverRadius: trendSmall ? 5 : 4
                }]
            },
            options: {
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) { return ctx.parsed.y + ' 条'; }
                        }
                    }
                },
                scales: {
                    x: { ticks: { color: cText, maxTicksLimit: 8 }, grid: { display: false } },
                    y: {
                        ticks: { color: cText, precision: 0 },
                        grid: { color: cBorder },
                        beginAtZero: true,
                        suggestedMax: trendSmall ? trendMax + 1 : undefined
                    }
                }
            },
            plugins: trendSmall ? [trendValueLabels] : []
        });
    }
});
