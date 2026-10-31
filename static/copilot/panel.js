/* AI 助手前端：轮询执行、渲染引用、降级提示。
   安全约定：回答正文一律用服务端下发的 ``answer_html``（markdown 渲染 +
   bleach 白名单净化），前端只做 innerHTML；绝不拿模型原文直接拼 HTML。
   引用链接只接受服务端下发的站内路径。 */
(function () {
    'use strict';
    var panel = document.getElementById('copilotPanel');
    if (!panel) return;

    var fab = document.getElementById('copilotFab');
    var log = document.getElementById('copilotLog');
    var form = document.getElementById('copilotForm');
    var input = document.getElementById('copilotInput');
    var sendBtn = document.getElementById('copilotSend');
    var hint = document.getElementById('copilotHint');
    var badge = document.getElementById('copilotBadge');
    var examplesBox = document.getElementById('copilotExamples');
    var newBtn = document.getElementById('copilotNew');
    var resizeHandle = document.getElementById('copilotResize');

    var state = { conversationId: null, busy: false, pos: null, pinned: false, size: null };
    var pinBtn = document.getElementById('copilotPin');
    var examplesCache = [];
    var moduleLabels = {};      // 步骤行里显示"查询列表 · 试样试产"，用户才知道查的是哪块
    var TOOL_LABELS = {
        list_modules: '查看可用模块',
        schema_of_module: '读取字段说明',
        global_search: '跨模块搜索',
        query_records: '查询列表',
        get_record: '查看详情',
        aggregate_records: '统计汇总',
        list_attachments: '查询附件',
        search_knowledge: '检索知识库',
        keyword_fallback: '关键词兜底检索',
        egress_blocked: '外发策略拦截'
    };
    var POLL_INTERVAL = 800;
    var POLL_MAX = 150;

    function csrf() {
        return (window.getCsrfToken && window.getCsrfToken()) || '';
    }

    function request(url, options) {
        options = options || {};
        return fetch(url, {
            method: options.method || 'GET',
            credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json',
                'X-CSRFToken': csrf()
            },
            body: options.body ? JSON.stringify(options.body) : undefined
        }).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
                if (!resp.ok || !data.ok) {
                    var err = new Error(data.message || ('请求失败（' + resp.status + '）'));
                    err.status = resp.status;
                    err.code = data.error;
                    throw err;
                }
                return data.data;
            });
        });
    }

    function addMessage(role, text) {
        var div = document.createElement('div');
        div.className = 'copilot-msg is-' + role;
        div.textContent = text;
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;
        return div;
    }

    function setBusy(busy) {
        state.busy = busy;
        sendBtn.disabled = busy;
        sendBtn.textContent = busy ? '处理中' : '发送';
        input.disabled = busy;
    }

    function renderSteps(bubble, steps) {
        if (!steps || !steps.length) return;
        var box = bubble.querySelector('.copilot-steps');
        if (!box) {
            box = document.createElement('div');
            box.className = 'copilot-steps';
            bubble.appendChild(box);
        }
        box.textContent = '';
        steps.forEach(function (s) {
            var line = document.createElement('div');
            var label = TOOL_LABELS[s.tool] || s.tool;
            if (s.module && moduleLabels[s.module]) {
                label += ' · ' + moduleLabels[s.module];
            }
            if (s.state) {
                // 状态型查询（已过期/借出中）写进步骤行，用户一看就知道筛了什么
                label += ' · ' + s.state;
            }
            if (s.ok) {
                line.textContent = '· ' + label + '（' + (s.rows || 0) + ' 条）';
            } else {
                line.textContent = '· ' + label + '（' + (s.error || '未命中') + '）';
                line.className = 'is-deny';
            }
            box.appendChild(line);
        });
    }

    function renderRefs(bubble, refs) {
        if (!refs || !refs.length) return;
        var box = document.createElement('div');
        box.className = 'copilot-refs';
        refs.forEach(function (ref) {
            var isLink = ref.url && ref.url.charAt(0) === '/';
            var item = document.createElement(isLink ? 'a' : 'span');
            item.className = 'copilot-ref' + (isLink ? '' : ' copilot-ref-static');
            if (isLink) item.href = ref.url;
            var module = document.createElement('span');
            module.className = 'copilot-ref-module';
            module.textContent = ref.module_label || ref.module || '';
            var label = document.createElement('span');
            label.className = 'copilot-ref-label';
            label.textContent = ref.label || ('#' + (ref.id || ''));
            item.appendChild(module);
            item.appendChild(label);
            box.appendChild(item);
        });
        bubble.appendChild(box);
    }

    /* 回答正文：优先用服务端渲染好的 markdown（answer_html），
       没有时退回纯文本——老数据/降级路径不该因为缺字段变成空白。
       注意先清空再插入，否则"正在处理…"的占位会和正文叠在一起。 */
    function setAnswer(bubble, html, plain) {
        bubble.textContent = '';
        if (html) {
            var body = document.createElement('div');
            body.className = 'copilot-md';
            bubble.classList.add('has-md');   // 有 HTML 就别再按纯文本保留空白
            body.innerHTML = html;
            // markdown 里的站内链接：别把用户带出面板到新标签页
            body.querySelectorAll('a[href^="/"]').forEach(function (a) {
                a.removeAttribute('target');
                a.removeAttribute('rel');
            });
            // 表格外面套一层滚动容器：列多时不撑破 360px 面板
            body.querySelectorAll('table').forEach(function (table) {
                var wrap = document.createElement('div');
                wrap.className = 'copilot-table-wrap';
                table.parentNode.insertBefore(wrap, table);
                wrap.appendChild(table);
            });
            bubble.appendChild(body);
            return;
        }
        bubble.classList.remove('has-md');
        bubble.textContent = plain || '（没有拿到回答）';
    }

    function finalize(bubble, data) {
        setAnswer(bubble, data.answer_html, data.answer);
        bubble.classList.toggle('is-degraded', data.error_code === 'DEGRADED');
        if (data.error_code === 'DEGRADED') {
            hint.textContent = 'AI 模型不可用，结果来自关键词检索，仅供参考。';
        } else {
            hint.textContent = '';
        }
        renderSteps(bubble, data.steps);
        renderRefs(bubble, data.refs);
        setBusy(false);
        log.scrollTop = log.scrollHeight;
    }

    function poll(runId, bubble) {
        var tries = 0;
        function tick() {
            if (tries++ >= POLL_MAX) {
                bubble.textContent = '等待超时，请重新提问。';
                bubble.classList.add('is-error');
                setBusy(false);
                return;
            }
            window.setTimeout(function () {
                request('/copilot/run/' + runId + '/').then(function (data) {
                    if (data.finished) {
                        finalize(bubble, data);
                        return;
                    }
                    bubble.textContent = '正在处理…';
                    renderSteps(bubble, data.steps);
                    tick();
                }).catch(function (err) {
                    bubble.textContent = '出错了：' + err.message;
                    bubble.classList.add('is-error');
                    setBusy(false);
                });
            }, POLL_INTERVAL);
        }
        tick();
    }

    function send(text) {
        if (state.busy) return;
        text = (text || '').trim();
        if (text.length < 2) {
            hint.textContent = '请至少输入两个字';
            return;
        }
        addMessage('user', text);
        var bubble = addMessage('assistant', '正在思考…');
        examplesBox.textContent = '';
        setBusy(true);
        hint.textContent = '';

        request('/copilot/ask/', {
            method: 'POST',
            body: { question: text, conversation_id: state.conversationId }
        }).then(function (data) {
            if (data.conversation_id) state.conversationId = data.conversation_id;
            if (data.sync && data.finished) {
                finalize(bubble, data);
                return;
            }
            poll(data.run_id, bubble);
        }).catch(function (err) {
            bubble.classList.add('is-error');
            bubble.textContent = err.code === 'RATE_LIMITED'
                ? err.message
                : '出错了：' + err.message;
            setBusy(false);
        });
    }

    function renderExamples(examples) {
        examplesBox.textContent = '';
        (examples || []).slice(0, 4).forEach(function (text) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'copilot-example';
            btn.textContent = text;
            btn.addEventListener('click', function () {
                examplesBox.textContent = '';
                send(text);
                input.value = '';
            });
            examplesBox.appendChild(btn);
        });
    }

    /* 历史恢复：面板在每个页面都被重新加载，状态不会自己留下来。
       每次打开都向服务端取该用户最近一条会话，让用户看到"接着上次聊"，
       而不是每跳一个页面就清空。 */
    function renderHistory(messages) {
        log.textContent = '';
        (messages || []).forEach(function (m) {
            if (m.role === 'user') {
                addMessage('user', m.content);
                return;
            }
            if (m.role !== 'assistant') return;
            var bubble = addMessage('assistant', '');
            setAnswer(bubble, m.content_html, m.content);
            bubble.classList.toggle('is-degraded', m.error_code === 'DEGRADED');
            renderSteps(bubble, m.steps);
            renderRefs(bubble, m.refs);
        });
        log.scrollTop = log.scrollHeight;
    }

    function restoreHistory() {
        request('/copilot/conversation/latest/').then(function (data) {
            var messages = (data && data.messages) || [];
            if (!messages.length) return;
            state.conversationId = data.conversation_id;
            renderHistory(messages);
            examplesBox.textContent = '';
            if (panel.hidden) hint.textContent = '已载入上次对话（' + messages.length + ' 条），可继续追问。';
        }).catch(function () { /* 取不到历史不影响新提问 */ });
    }

    function startNewChat() {
        state.conversationId = null;
        log.textContent = '';
        hint.textContent = '';
        renderExamples(examplesCache);
        input.focus();
    }

    /* ---------- 面板位置：拖动 / 钉住 ----------
       面板默认钉死在右下角，27 寸屏上离视线很远，用起来要扭着头。这里让
       标题栏当拖动把手，位置记进 localStorage（每台机器各自记），换页、刷新
       都还在；「钉住」把位置锁死，避免聊天时手一抖把面板拖飞。 */
    var POS_KEY = 'copilot:panel-pos:v1';
    var EDGE = 12;              // 贴边留白，也是拖动结束时的吸附阈值
    var FALLBACK_W = 560;
    var FALLBACK_H = 760;
    var MIN_W = 320;
    var MIN_H = 360;

    function panelSize() {
        // hidden 时 offsetWidth 是 0，给个兜底尺寸，否则 clamp 会把面板挤到左上角
        return {
            w: panel.offsetWidth || FALLBACK_W,
            h: panel.offsetHeight || FALLBACK_H
        };
    }

    function clampPos(left, top) {
        var size = panelSize();
        var maxLeft = Math.max(EDGE, window.innerWidth - size.w - EDGE);
        var maxTop = Math.max(EDGE, window.innerHeight - size.h - EDGE);
        return {
            left: Math.min(Math.max(EDGE, left), maxLeft),
            top: Math.min(Math.max(EDGE, top), maxTop)
        };
    }

    function applyPos(left, top) {
        state.pos = { left: left, top: top };
        panel.style.setProperty('--copilot-left', left + 'px');
        panel.style.setProperty('--copilot-top', top + 'px');
        panel.classList.add('is-floating');
    }

    function clearPos() {
        state.pos = null;
        panel.style.removeProperty('--copilot-left');
        panel.style.removeProperty('--copilot-top');
        panel.classList.remove('is-floating');
    }

    function savePos() {
        try {
            if (!state.pos && !state.size) {
                window.localStorage.removeItem(POS_KEY);
                return;
            }
            var data = { pinned: state.pinned };
            if (state.pos) {
                data.left = Math.round(state.pos.left);
                data.top = Math.round(state.pos.top);
            }
            if (state.size) {
                data.w = Math.round(state.size.w);
                data.h = Math.round(state.size.h);
            }
            window.localStorage.setItem(POS_KEY, JSON.stringify(data));
        } catch (err) { /* 隐私模式 / 禁用存储：位置不记忆，功能照用 */ }
    }

    /* ---------- 面板尺寸 ----------
       默认 560×760 是按"回答里会出表格"定的：400×560 时三列表格必横向滚。
       右下角把手可拖，尺寸同样记 localStorage；双击把手恢复默认。 */
    function applySize(w, h) {
        if (!w || !h) {
            panel.style.removeProperty('--copilot-w');
            panel.style.removeProperty('--copilot-h');
            state.size = null;
            return;
        }
        var maxW = Math.max(MIN_W, window.innerWidth - 32);
        var maxH = Math.max(MIN_H, window.innerHeight - 80);
        var width = Math.min(Math.max(MIN_W, Math.round(w)), maxW);
        var height = Math.min(Math.max(MIN_H, Math.round(h)), maxH);
        state.size = { w: width, h: height };
        panel.style.setProperty('--copilot-w', width + 'px');
        panel.style.setProperty('--copilot-h', height + 'px');
    }

    function setupResize() {
        if (!resizeHandle) return;
        var drag = null;

        resizeHandle.addEventListener('pointerdown', function (event) {
            if (event.pointerType === 'mouse' && event.button !== 0) return;
            var rect = panel.getBoundingClientRect();
            drag = {
                id: event.pointerId,
                x: event.clientX, y: event.clientY,
                w: rect.width, h: rect.height
            };
            panel.classList.add('is-resizing');
            document.body.classList.add('copilot-resizing');
            if (resizeHandle.setPointerCapture) resizeHandle.setPointerCapture(event.pointerId);
            event.preventDefault();
            event.stopPropagation();     // 别让标题栏的拖动逻辑抢到
        });

        resizeHandle.addEventListener('pointermove', function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            applySize(drag.w + (event.clientX - drag.x), drag.h + (event.clientY - drag.y));
            // 变宽后可能顶出视口右侧：位置同步夹一次
            if (state.pos) {
                var p = clampPos(state.pos.left, state.pos.top);
                applyPos(p.left, p.top);
            }
            event.preventDefault();
        });

        function endResize(event) {
            if (!drag) return;
            drag = null;
            panel.classList.remove('is-resizing');
            document.body.classList.remove('copilot-resizing');
            savePos();
            if (event && event.pointerId !== undefined && resizeHandle.releasePointerCapture) {
                try { resizeHandle.releasePointerCapture(event.pointerId); } catch (err) { /* 已释放 */ }
            }
        }

        resizeHandle.addEventListener('pointerup', endResize);
        resizeHandle.addEventListener('pointercancel', endResize);

        // 双击把手 = 尺寸回默认（位置保持不变，免得"复位"把面板甩回右下角）
        resizeHandle.addEventListener('dblclick', function (event) {
            event.stopPropagation();
            applySize(null, null);
            savePos();
        });
    }

    function loadPos() {
        var raw = null;
        try { raw = window.localStorage.getItem(POS_KEY); } catch (err) { return; }
        if (!raw) return;
        var saved = null;
        try { saved = JSON.parse(raw); } catch (err) { return; }
        if (!saved || typeof saved !== 'object') return;
        // 先恢复尺寸再夹位置：尺寸决定了"最多能放在哪"
        if (typeof saved.w === 'number' && typeof saved.h === 'number') {
            applySize(saved.w, saved.h);
        }
        if (typeof saved.left === 'number' && typeof saved.top === 'number') {
            var p = clampPos(saved.left, saved.top);   // 换过屏幕/缩放过窗口，夹回可视区
            applyPos(p.left, p.top);
        }
        setPinned(!!saved.pinned);
    }

    function setPinned(pinned) {
        state.pinned = !!pinned;
        panel.classList.toggle('is-locked', state.pinned);
        if (pinBtn) {
            pinBtn.classList.toggle('is-active', state.pinned);
            pinBtn.setAttribute('aria-pressed', state.pinned ? 'true' : 'false');
            pinBtn.title = state.pinned
                ? '已钉住：位置锁死，标题栏不能再拖（点一下解除）'
                : '钉住位置（拖动标题栏可移动，双击标题栏复位）';
        }
        savePos();
    }

    function resetPos() {
        clearPos();
        state.pinned = false;
        setPinned(false);      // 内部会 savePos；此时没有位置 → 顺手清掉 localStorage
    }

    function setupDrag() {
        var header = panel.querySelector('.copilot-header');
        if (!header) return;
        var drag = null;

        function inButton(node) {
            return !!(node && node.closest && node.closest('button'));
        }

        header.addEventListener('pointerdown', function (event) {
            if (event.pointerType === 'mouse' && event.button !== 0) return;
            if (state.pinned || inButton(event.target)) return;
            var rect = panel.getBoundingClientRect();
            // 先固化成 left/top 定位：否则第一次拖动时面板会从右下角"跳"到鼠标下
            applyPos(rect.left, rect.top);
            drag = {
                id: event.pointerId,
                x: event.clientX, y: event.clientY,
                left: rect.left, top: rect.top,
                moved: false
            };
            panel.classList.add('is-dragging');
            document.body.classList.add('copilot-dragging');
            if (header.setPointerCapture) header.setPointerCapture(event.pointerId);
            event.preventDefault();
        });

        header.addEventListener('pointermove', function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            var dx = event.clientX - drag.x;
            var dy = event.clientY - drag.y;
            if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
            var p = clampPos(drag.left + dx, drag.top + dy);
            applyPos(p.left, p.top);
        });

        function endDrag(event) {
            if (!drag) return;
            if (drag.moved && state.pos) {
                // 拖到顶边就吸附到顶部——「置顶」最顺手的用法，不用对齐像素
                if (state.pos.top <= EDGE * 2) applyPos(state.pos.left, EDGE);
                savePos();
            }
            drag = null;
            panel.classList.remove('is-dragging');
            document.body.classList.remove('copilot-dragging');
            if (header.releasePointerCapture && event && event.pointerId !== undefined) {
                try { header.releasePointerCapture(event.pointerId); } catch (err) { /* 已释放 */ }
            }
        }

        header.addEventListener('pointerup', endDrag);
        header.addEventListener('pointercancel', endDrag);

        // 双击标题栏 = 回到默认右下角（拖飞了不必去翻 localStorage）
        header.addEventListener('dblclick', function (event) {
            if (inButton(event.target)) return;
            resetPos();
        });
    }

    function openPanel() {
        panel.hidden = false;
        restorePos();
        log.scrollTop = log.scrollHeight;   // 面板隐藏时设置滚动无效，展开后补一次
        input.focus();
    }

    function restorePos() {
        if (!state.pos) return;
        var p = clampPos(state.pos.left, state.pos.top);
        applyPos(p.left, p.top);
    }

    function closePanel() {
        panel.hidden = true;
        fab.focus();
    }

    fab.addEventListener('click', function () {
        if (panel.hidden) openPanel(); else closePanel();
    });
    document.getElementById('copilotClose').addEventListener('click', closePanel);
    if (newBtn) newBtn.addEventListener('click', startNewChat);

    if (pinBtn) {
        pinBtn.addEventListener('click', function () {
            if (!state.pinned && !state.pos) {
                // 面板还在默认右下角：先固化当前位置再钉住，否则"钉住"看不出效果
                var rect = panel.getBoundingClientRect();
                applyPos(rect.left, rect.top);
            }
            setPinned(!state.pinned);
        });
    }

    window.addEventListener('resize', function () {
        if (panel.hidden) return;
        if (state.size) applySize(state.size.w, state.size.h);   // 窗口变小 → 面板收回视口
        if (!state.pos) return;
        var p = clampPos(state.pos.left, state.pos.top);
        applyPos(p.left, p.top);
    });

    document.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === 'k') {
            event.preventDefault();
            openPanel();
        }
        if (event.key === 'Escape' && !panel.hidden) closePanel();
    });

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        var text = input.value;
        input.value = '';
        send(text);
    });

    input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            if (form.requestSubmit) {
                form.requestSubmit();
            } else {
                form.dispatchEvent(new Event('submit', { cancelable: true }));
            }
        }
    });

    /* 云端模式下的说明：必须写清"用的是谁的配置"。
       最坑的一种情况是「对话问答」没单独配置，Copilot 借用了「文本润色」那行，
       用户在配置页只看到润色、以为改不动，徽章却一直显示云端。 */
    function cloudTip(data, who) {
        var tip = '当前模型：' + (who || '未知') + '（云端，数据会离开本机）。';
        if (data.borrowed) {
            tip += '「对话问答」还没有单独配置，现在借用的是「'
                + (data.borrowed_from || '其它用途') + '」的模型 —— '
                + '去「系统管理 → AI 配置 → 对话问答」单独配置并勾选「本地模型」即可切换。';
        } else {
            tip += '要查敏感数据，请去「系统管理 → AI 配置 → 对话问答」换成本地模型并勾选「本地模型」。';
        }
        if (data.blocked && data.blocked.length) {
            tip += '受外发策略限制，这些模块暂时查不到：'
                + data.blocked.slice(0, 6).join('、') + '。';
        }
        return tip;
    }

    function boot() {
        setupDrag();
        setupResize();
        loadPos();
        request('/copilot/bootstrap/').then(function (data) {
            fab.hidden = false;
            badge.hidden = false;
            var who = data.configured
                ? ((data.provider_label || data.provider || '') + ' / ' + (data.model || '未指定模型'))
                : '';
            if (who) {
                badge.title = '当前模型：' + who + (data.is_local ? '（本地）' : '（云端）');
            }
            if (!data.configured) {
                badge.textContent = '未配置模型';
                hint.textContent = '管理员尚未配置 AI 模型，暂时只能用关键词检索。';
            } else if (data.is_local) {
                badge.textContent = '本地模型';
                badge.classList.remove('is-cloud');
            } else {
                badge.textContent = data.borrowed ? '云端模型 · 借用' : '云端模型';
                badge.classList.add('is-cloud');
                hint.textContent = cloudTip(data, who);
            }
            if (!data.modules || !data.modules.length) {
                hint.textContent = '当前账号没有可查询的业务模块权限。';
            }
            examplesCache = data.examples || [];
            moduleLabels = {};
            (data.modules || []).forEach(function (m) { moduleLabels[m.key] = m.label; });
            renderExamples(examplesCache);
            restoreHistory();
        }).catch(function () {
            fab.hidden = true;   // 无 copilot 权限：不显示入口
        });
    }

    if (document.readyState === 'complete') {
        boot();
    } else {
        window.addEventListener('load', boot);
    }
})();
