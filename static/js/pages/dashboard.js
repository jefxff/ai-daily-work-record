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

    // 待处理事项筛选功能
    const filterButtons = document.querySelectorAll('.filter-btn');
    const pendingItems = document.querySelectorAll('.pending-item');

    filterButtons.forEach(button => {
        button.addEventListener('click', function () {
            // 移除所有按钮的active状态
            filterButtons.forEach(btn => btn.classList.remove('active'));
            // 添加当前按钮的active状态
            this.classList.add('active');

            const filter = this.dataset.filter;

            pendingItems.forEach(item => {
                if (filter === 'all' || item.dataset.type === filter) {
                    item.style.display = 'flex';
                } else {
                    item.style.display = 'none';
                }
            });
        });
    });

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

        new Chart(document.getElementById('chartTrend'), {
            type: 'line',
            data: {
                labels: chartData.trend_labels,
                datasets: [{
                    label: '每日记录',
                    data: chartData.trend_values,
                    borderColor: cPrimary,
                    backgroundColor: withAlpha(cPrimary, 0.13),
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2
                }]
            },
            options: {
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: cText, maxTicksLimit: 8 }, grid: { display: false } },
                    y: { ticks: { color: cText }, grid: { color: cBorder }, beginAtZero: true }
                }
            }
        });
    }
});
