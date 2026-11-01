(function () {
    // HTTPS 模式下 cookie 名是 __Host-csrftoken（此前只读 csrftoken → 一键备份 403，UI 反馈 #5）
    var csrftoken = window.getCsrfToken ? window.getCsrfToken() : (window.getCookie('csrftoken') || '');

    var $ = function (id) { return document.getElementById(id); };

    /* 高危动作权限：服务端按权限码渲染，与静态表格同一把锁；无权时 JS 不渲染入口 */
    var _bkPerms = $('bkPerms') ? $('bkPerms').dataset : {};
    var BK_PERMS = {
        restore: _bkPerms.restore === '1',
        backup: _bkPerms.backup === '1',
        del: _bkPerms.delete === '1'
    };

    /* URL 统一来自根容器 data-* 属性（服务端用 url 模板标签生成），不再硬编码路径 */
    var U = document.getElementById('backupCenter').dataset;

    /* ---------- 通用 AJAX（完整消费响应体后再做后续动作） ---------- */
    function _parseJsonResp(r) {
        return r.text().then(function (txt) {
            try {
                return JSON.parse(txt);
            } catch (e) {
                throw new Error('服务响应异常（HTTP ' + r.status + '），请稍后重试');
            }
        });
    }
    function api(url, payload) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken,
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify(payload || {})
        }).then(_parseJsonResp);
    }
    function apiGet(url) {
        return fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(_parseJsonResp);
    }
    function toast(msg, type) {
        if (window.showToast) window.showToast(msg, type || 'info');
    }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
    }

    /* ---------- 风险确认弹窗 ---------- */
    var riskCtx = null;
    function openRisk(opts) {
        riskCtx = opts;
        $('riskTitle').textContent = opts.title;
        $('riskBody').innerHTML = opts.body;
        $('riskAckRow').style.display = opts.needAck ? '' : 'none';
        $('riskAck').checked = false;
        $('riskOk').disabled = !!opts.needAck;
        $('riskOk').className = 'btn btn-sm ' + (opts.danger ? 'btn-danger' : 'btn-primary');
        $('riskOk').textContent = opts.okText || '确认执行';
        $('riskBackdrop').classList.add('show');
    }
    $('riskAck').addEventListener('change', function () { $('riskOk').disabled = !this.checked; });
    $('riskCancel').addEventListener('click', function () { $('riskBackdrop').classList.remove('show'); riskCtx = null; });
    $('riskOk').addEventListener('click', function () {
        $('riskBackdrop').classList.remove('show');
        if (riskCtx && riskCtx.onOk) riskCtx.onOk();
        riskCtx = null;
    });

    /* ---------- 任务进度弹窗与轮询 ---------- */
    var pollTimer = null;
    function openTask(title) {
        $('taskTitle').textContent = title;
        $('taskBar').style.width = '0%';
        $('taskPercent').textContent = '0%';
        $('taskStage').textContent = '准备中…';
        $('taskResult').style.display = 'none';
        $('taskLog').style.display = 'none';
        $('taskLog').textContent = '';
        $('taskLogToggle').classList.add('d-none');
        $('taskClose').textContent = '后台运行';
        $('taskBackdrop').classList.add('show');
    }
    function showResult(ok, text) {
        var el = $('taskResult');
        var tokens = getComputedStyle(document.documentElement);
        var bg = ok ? '--c-success-bg' : '--c-danger-bg';
        var line = ok ? '--c-success-line' : '--c-danger-line';
        var fg = ok ? '--c-success' : '--c-danger';
        el.style.display = 'block';
        el.style.background = tokens.getPropertyValue(bg).trim();
        el.style.border = '1px solid ' + tokens.getPropertyValue(line).trim();
        el.style.color = tokens.getPropertyValue(fg).trim();
        el.textContent = text;
    }
    function poll(taskId) {
        if (pollTimer) clearTimeout(pollTimer);
        apiGet(U.urlStatus + '?task=' + taskId).then(function (d) {
            if (!d.ok) { showResult(false, d.message || '任务状态查询失败'); return; }
            var t = d.task;
            $('taskBar').style.width = t.percent + '%';
            $('taskPercent').textContent = t.percent + '%';
            $('taskStage').textContent = t.stage || '';
            if (t.log && t.log.length) {
                $('taskLog').textContent = t.log.join('\n');
                $('taskLogToggle').classList.remove('d-none');
            }
            if (t.status === 'running') {
                pollTimer = setTimeout(function () { poll(taskId); }, 1500);
            } else if (t.status === 'success') {
                showResult(true, t.message);
                $('taskClose').textContent = '关 闭';
                refreshList($('scanDirInput').value);
                loadLogs();
            } else {
                showResult(false, t.message);
                $('taskLogToggle').classList.remove('d-none');
                $('taskLog').style.display = '';
                $('taskClose').textContent = '关 闭';
                loadLogs();
            }
        }).catch(function (e) { showResult(false, '网络错误：' + e); });
    }
    $('taskClose').addEventListener('click', function () {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        $('taskBackdrop').classList.remove('show');
    });
    $('taskLogToggle').addEventListener('click', function () {
        var el = $('taskLog');
        el.style.display = el.style.display === 'none' ? '' : 'none';
    });

    /* ---------- 创建备份 ---------- */
    var radios = document.querySelectorAll('input[name="destOption"]');
    radios.forEach(function (r) {
        r.addEventListener('change', function () {
            $('customDirInput').disabled = !$('destCustom').checked;
        });
    });
    $('btnStartBackup').addEventListener('click', function () {
        var dest = '';
        if ($('destCustom').checked) {
            dest = $('customDirInput').value.trim();
            if (!dest) { toast('请填写自定义备份路径', 'error'); return; }
        }
        openTask('全量备份');
        api(U.urlStart, { dest_dir: dest }).then(function (d) {
            if (d.ok) { poll(d.task_id); }
            else { showResult(false, d.message || '发起备份失败'); }
        }).catch(function (e) { showResult(false, '网络错误：' + e); });
    });

    /* ---------- 恢复 ---------- */
    function bindRowButtons() {
        document.querySelectorAll('.btn-restore').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var tr = btn.closest('tr');
                var path = tr.getAttribute('data-path');
                var name = tr.getAttribute('data-name');
                openRisk({
                    title: '确认恢复数据？',
                    body:
                        '<div class="alert alert-danger py-2 px-3 mb-2 u-fs-82">' +
                        '⚠️ 即将使用备份包 <strong>' + name + '</strong> <strong>全量覆盖</strong>当前系统：</div>' +
                        '<ul class="mb-2 pl-3" style="padding-left:1.1rem;">' +
                        '<li>全部数据库表数据将回退到备份时点，备份之后新增/修改的记录将<b>永久丢失</b>；</li>' +
                        '<li>全部附件文件将被覆盖，备份之后上传的附件将被<b>清除</b>；</li>' +
                        '<li>恢复期间请勿在系统内进行任何操作；</li>' +
                        '<li>恢复前系统会自动保存当前数据的安全快照（backups/safety_snapshots/）。</li></ul>',
                    needAck: true, danger: true, okText: '开始恢复',
                    onOk: function () {
                        openTask('全量恢复');
                        api(U.urlRestore, { path: path }).then(function (d) {
                            if (d.ok) { poll(d.task_id); }
                            else { showResult(false, d.message || '发起恢复失败'); }
                        }).catch(function (e) { showResult(false, '网络错误：' + e); });
                    }
                });
            });
        });
        document.querySelectorAll('.btn-delete-bk').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var tr = btn.closest('tr');
                var path = tr.getAttribute('data-path');
                var name = tr.getAttribute('data-name');
                openRisk({
                    title: '删除备份包',
                    body: '确定删除备份包 <strong>' + name + '</strong> 吗？删除后不可恢复。',
                    needAck: false, danger: true, okText: '删除',
                    onOk: function () {
                        api(U.urlDelete, { path: path }).then(function (d) {
                            if (d.ok) { tr.remove(); checkEmpty(); toast(d.message || '已删除', 'success'); }
                            else { toast(d.message || '删除失败', 'error'); }
                        }).catch(function (e) {
                            toast('删除请求失败：' + (e && e.message ? e.message : e), 'error');
                        });
                    }
                });
            });
        });
    }

    /* ---------- 列表扫描 ---------- */
    function checkEmpty() {
        var tb = $('backupTbody');
        if (!tb.querySelector('tr[data-path]') && !tb.querySelector('#emptyRow')) {
            tb.insertAdjacentHTML('beforeend',
                '<tr id="emptyRow"><td colspan="7" class="text-center text-muted py-4">当前目录暂无备份包</td></tr>');
        }
    }
    function refreshList(dir) {
        var scanBtn = $('scanForm').querySelector('button[type="submit"]');
        var btnText = scanBtn ? scanBtn.textContent : '';
        if (scanBtn) { scanBtn.disabled = true; scanBtn.textContent = '扫描中…'; }
        var restoreBtn = function () {
            if (scanBtn) { scanBtn.disabled = false; scanBtn.textContent = btnText; }
        };
        apiGet(U.urlScan + '?dir=' + encodeURIComponent(dir || '')).then(function (d) {
            restoreBtn();
            if (!d.ok) { toast(d.message || '扫描失败', 'error'); return; }
            $('scannedDirText').textContent = d.dir;
            var tb = $('backupTbody');
            tb.innerHTML = '';
            d.backups.forEach(function (b) {
                tb.insertAdjacentHTML('beforeend',
                    '<tr data-path="' + esc(b.path) + '" data-name="' + esc(b.name) + '">' +
                    '<td data-label="备份包" class="text-break u-bold">' + esc(b.name) +
                    (b.readable === false
                        ? ' <span class="badge bg-danger">无法解密</span>'
                        : (b.schema_version !== 1 ? ' <span class="badge bg-secondary">版本未知</span>' : '')) + '</td>' +
                    '<td data-label="备份时间">' + esc(b.created_at || b.mtime) + '</td>' +
                    '<td data-label="操作人">' + esc(b.created_by || '—') + '</td>' +
                    '<td data-label="大小">' + esc(b.size_text) + '</td>' +
                    '<td data-label="数据库">' + esc(b.db_tables) + ' 表 / ' + esc(b.db_rows) + ' 条</td>' +
                    '<td data-label="附件">' + esc(b.media_files) + ' 个</td>' +
                    '<td data-label="操作">' +
                        (b.readable === false ? '' :
                            (BK_PERMS.restore ? '<button type="button" class="btn btn-sm btn-outline-danger btn-restore">恢复</button> ' : '') +
                            (BK_PERMS.backup ? '<a class="btn btn-sm btn-outline-secondary btn-download" href="' + U.urlDownload + '?path=' + encodeURIComponent(b.path) + '" download="' + esc(b.name) + '">下载</a> ' : '')) +
                        (BK_PERMS.del ? '<button type="button" class="btn btn-sm btn-outline-secondary btn-delete-bk">删除</button>' : '') +
                    '</td></tr>');
            });
            if (!d.backups.length) {
                tb.insertAdjacentHTML('beforeend',
                    '<tr id="emptyRow"><td colspan="7" class="text-center text-muted py-4">当前目录暂无备份包</td></tr>');
            }
            bindRowButtons();
            toast(d.backups.length
                ? '扫描完成：发现 ' + d.backups.length + ' 个备份包'
                : '扫描完成：当前目录暂无备份包', d.backups.length ? 'success' : 'info');
        }).catch(function (e) {
            restoreBtn();
            toast('扫描失败：' + (e && e.message ? e.message : e), 'error');
        });
    }
    $('scanForm').addEventListener('submit', function (e) {
        e.preventDefault();
        refreshList($('scanDirInput').value.trim());
    });

    bindRowButtons();

    /* ---------- 操作记录模块 ---------- */
    var logState = { page: 1, pageSize: 10, opType: '' };

    function logQueryUrl() {
        var p = new URLSearchParams();
        p.set('page', logState.page);
        p.set('page_size', logState.pageSize);
        if (logState.opType) p.set('op_type', logState.opType);
        var s = $('logStart').value.trim(), e = $('logEnd').value.trim();
        if (s) p.set('start', s);
        if (e) p.set('end', e);
        return U.urlOplogs + '?' + p.toString();
    }

    function loadLogs() {
        apiGet(logQueryUrl()).then(function (d) {
            if (!d.ok) { toast(d.message || '操作记录加载失败', 'error'); return; }
            $('logCountAll').textContent = d.stats.all;
            $('logCountBackup').textContent = d.stats.backup;
            $('logCountRestore').textContent = d.stats.restore;
            renderLogs(d.items);
            renderLogPager(d);
        }).catch(function (e) {
            toast('操作记录加载失败：' + (e && e.message ? e.message : e), 'error');
        });
    }

    function renderLogs(items) {
        var tb = $('logTbody');
        tb.innerHTML = '';
        if (!items.length) {
            tb.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">暂无操作记录</td></tr>';
            return;
        }
        items.forEach(function (x, idx) {
            var typeBadge = x.op_type === 'backup'
                ? '<span class="badge bg-primary">备份导出</span>'
                : '<span class="badge bg-warning text-dark">数据恢复</span>';
            var resultBadge = x.success
                ? '<span class="badge bg-success">成功</span>'
                : '<span class="badge bg-danger">失败</span>';
            var shortDetail = x.detail && x.detail.length > 40 ? x.detail.slice(0, 40) + '…' : (x.detail || '');
            tb.insertAdjacentHTML('beforeend',
                '<tr>' +
                '<td data-label="操作类型">' + typeBadge + '</td>' +
                '<td data-label="操作时间" class="text-nowrap">' + esc(x.created_at) + '</td>' +
                '<td data-label="操作人">' + esc(x.operator) + '</td>' +
                '<td data-label="操作文件标识" class="text-break">' + (esc(x.file_name) || '—') + '</td>' +
                '<td data-label="操作结果">' + resultBadge + '</td>' +
                '<td data-label="操作详情" class="text-break text-muted" style="max-width:280px;">' + esc(shortDetail) + '</td>' +
                '<td data-label="操作"><button type="button" class="btn btn-sm btn-outline-secondary btn-log-detail" data-idx="' + idx + '">详情</button></td>' +
                '</tr>');
        });
        tb.querySelectorAll('.btn-log-detail').forEach(function (btn) {
            btn.addEventListener('click', function () {
                showLogDetail(items[parseInt(btn.getAttribute('data-idx'), 10)]);
            });
        });
    }

    function renderLogPager(d) {
        $('logTotalText').textContent = '共 ' + d.total + ' 条 · 第 ' + d.page + ' / ' + (d.pages || 1) + ' 页';
        var ul = $('logPagination');
        ul.innerHTML = '';
        var pages = d.pages || 1;
        function mk(label, page, disabled, active) {
            return '<li class="page-item' + (disabled ? ' disabled' : '') + (active ? ' active' : '') + '">' +
                '<a class="page-link" href="#" data-page="' + page + '">' + label + '</a></li>';
        }
        var win = 2, sp = Math.max(1, d.page - win), ep = Math.min(pages, d.page + win);
        ul.insertAdjacentHTML('beforeend', mk('«', d.page - 1, d.page <= 1, false));
        if (sp > 1) {
            ul.insertAdjacentHTML('beforeend', mk('1', 1, false, d.page === 1));
            if (sp > 2) ul.insertAdjacentHTML('beforeend', '<li class="page-item disabled"><span class="page-link">…</span></li>');
        }
        for (var p = sp; p <= ep; p++) ul.insertAdjacentHTML('beforeend', mk(p, p, false, p === d.page));
        if (ep < pages) {
            if (ep < pages - 1) ul.insertAdjacentHTML('beforeend', '<li class="page-item disabled"><span class="page-link">…</span></li>');
            ul.insertAdjacentHTML('beforeend', mk(pages, pages, false, false));
        }
        ul.insertAdjacentHTML('beforeend', mk('»', d.page + 1, d.page >= pages, false));
        ul.querySelectorAll('a.page-link').forEach(function (a) {
            a.addEventListener('click', function (e) {
                e.preventDefault();
                var pg = parseInt(a.getAttribute('data-page'), 10);
                if (pg >= 1 && pg <= pages && pg !== logState.page) { logState.page = pg; loadLogs(); }
            });
        });
    }

    function showLogDetail(x) {
        var resultVal = x.success
            ? '<span class="badge bg-success">成功</span>'
            : '<span class="badge bg-danger">失败</span>';
        var rows = [
            ['操作类型', esc(x.op_type_text)],
            ['操作时间', esc(x.created_at)],
            ['操作人', esc(x.operator)],
            ['操作文件标识', esc(x.file_name) || '—'],
            ['操作结果', resultVal],
            ['文件完整路径', esc(x.file_path) || '—'],
            ['操作详情', esc(x.detail) || '—'],
        ];
        $('logDetailBody').innerHTML = rows.map(function (r) {
            return '<div class="detail-row"><div class="detail-label">' + r[0] +
                '</div><div class="detail-value" style="word-break:break-all;">' + r[1] + '</div></div>';
        }).join('');
        $('logDetailBackdrop').classList.add('show');
    }

    $('logDetailClose').addEventListener('click', function () { $('logDetailBackdrop').classList.remove('show'); });
    $('logDetailOk').addEventListener('click', function () { $('logDetailBackdrop').classList.remove('show'); });
    $('logDetailBackdrop').addEventListener('click', function (e) {
        if (e.target === this) this.classList.remove('show');
    });

    // 类型 tab 切换
    $('logTabs').querySelectorAll('.nav-link').forEach(function (btn) {
        btn.addEventListener('click', function () {
            $('logTabs').querySelectorAll('.nav-link').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            logState.opType = btn.getAttribute('data-op-type');
            logState.page = 1;
            loadLogs();
        });
    });

    // 时间范围查询 / 重置
    $('logFilterForm').addEventListener('submit', function (e) {
        e.preventDefault();
        logState.page = 1;
        loadLogs();
    });
    $('btnLogReset').addEventListener('click', function () {
        $('logStart').value = '';
        $('logEnd').value = '';
        $('logTabs').querySelectorAll('.nav-link').forEach(function (b) { b.classList.remove('active'); });
        var first = $('logTabs').querySelector('.nav-link');
        if (first) first.classList.add('active');
        logState.opType = '';
        logState.page = 1;
        loadLogs();
    });

    // 每页条数
    $('logPageSize').addEventListener('change', function () {
        logState.pageSize = parseInt(this.value, 10) || 10;
        logState.page = 1;
        loadLogs();
    });

    loadLogs();  // 页面加载时自动拉取最新操作记录
})();
