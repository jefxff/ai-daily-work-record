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
    var historyBtn = document.getElementById('copilotHistoryBtn');
    var historyBox = document.getElementById('copilotHistory');
    var historyList = document.getElementById('copilotHistoryList');
    var historyClose = document.getElementById('copilotHistoryClose');

    var state = {
        conversationId: null, busy: false, pos: null, pinned: false, size: null,
        examples: [],          // 首屏示例，历史/新对话之间来回切时复用
        lastRun: null          // 当前 run 的 id，流式/轮询共用
    };
    // 点踩原因：和服务端 CopilotFeedback.Reason 的取值一一对应（前端只发 code）
    var FEEDBACK_REASONS = [
        { code: 'wrong_module', label: '答错模块' },
        { code: 'wrong_number', label: '数字不对' },
        { code: 'missing', label: '没有这条' },
        { code: 'slow', label: '太慢' }
    ];
    var pinBtn = document.getElementById('copilotPin');
    var draftAllowed = false;   // bootstrap 回传：无知识库新建权限时不显示「存草稿」
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
        bubble.dataset.hasRefs = refs && refs.length ? '1' : '';
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
        // 复制/导出都用这份 Markdown 原文，别从渲染后的 DOM 反推（表格会丢）
        bubble.dataset.plain = plain || '';
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

    /* ---------- 每条回答的操作：复制 / 有用 / 没用 ----------
       反馈只有做到"一次点击"才会有人用：内网工具没人愿意为一句"答错了"
       多点三次。所以点赞踩直接提交，点踩才额外给一行原因（也可不选）。 */
    function copyText(text) {
        // http 页面（内网常见）没有 navigator.clipboard，必须留 execCommand 兜底
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            var area = document.createElement('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.top = '-1000px';
            document.body.appendChild(area);
            area.select();
            try {
                document.execCommand('copy') ? resolve() : reject(new Error('copy failed'));
            } catch (err) {
                reject(err);
            } finally {
                document.body.removeChild(area);
            }
        });
    }

    function markVote(bubble, vote) {
        bubble.querySelectorAll('.copilot-act[data-vote]').forEach(function (btn) {
            btn.classList.toggle('is-on', Number(btn.dataset.vote) === vote);
        });
    }

    function submitFeedback(bubble, messageId, vote, reason) {
        request('/copilot/feedback/', {
            method: 'POST',
            body: { message_id: messageId, vote: vote, reason: reason || '' }
        }).then(function () {
            markVote(bubble, vote);
            var reasons = bubble.querySelector('.copilot-reasons');
            if (reasons) reasons.hidden = true;
            hint.textContent = vote === 1 ? '已记录：这条回答有用。' : '已记录，我们会把它纳入错例复盘。';
        }).catch(function (err) {
            hint.textContent = '反馈没提交上：' + err.message;
        });
    }

    /* 导出要走的是"文件下载"而不是 JSON 接口，所以这里不能用上面的 request()：
       它把响应当 JSON 解析。文件名从 Content-Disposition 里取，取不到给兜底。 */
    function fileNameFrom(disposition) {
        var text = disposition || '';
        var hit = /filename\*=UTF-8''([^;]+)/.exec(text) || /filename="?([^";]+)"?/.exec(text);
        if (!hit) return '';
        try { return decodeURIComponent(hit[1]); } catch (err) { return hit[1]; }
    }

    function downloadAnswer(bubble, button) {
        var label = button.textContent;
        button.disabled = true;
        button.textContent = '生成中…';
        fetch('/copilot/message/export/', {
            method: 'POST', credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json',
                'X-CSRFToken': csrf()
            },
            body: JSON.stringify({ message_id: Number(bubble.dataset.messageId) })
        }).then(function (resp) {
            if (!resp.ok) {
                // 失败时服务端回的是 JSON（{"ok":false,"message":...}），要照常读出来
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    throw new Error(data.message || ('导出失败（' + resp.status + '）'));
                });
            }
            var name = fileNameFrom(resp.headers.get('Content-Disposition')) || 'AI回答.xlsx';
            return resp.blob().then(function (blob) {
                var url = URL.createObjectURL(blob);
                var link = document.createElement('a');
                link.href = url;
                link.download = name;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                URL.revokeObjectURL(url);
            });
        }).then(function () {
            button.textContent = '已导出';
            window.setTimeout(function () {
                button.textContent = label; button.disabled = false;
            }, 1600);
        }).catch(function (err) {
            hint.textContent = '导出失败：' + err.message;
            button.textContent = label;
            button.disabled = false;
        });
    }

    function saveDraft(bubble, button) {
        request('/copilot/message/draft/', {
            method: 'POST',
            body: { message_id: Number(bubble.dataset.messageId) }
        }).then(function (data) {
            button.textContent = '已存草稿';
            button.disabled = true;
            hint.textContent = '已生成知识库草稿《' + data.title + '》，'
                + '默认是草稿状态，需人工编辑后再发布。';
        }).catch(function (err) {
            hint.textContent = '存草稿失败：' + err.message;
        });
    }

    function renderActions(bubble, messageId, vote) {
        if (!messageId) return;
        bubble.dataset.messageId = messageId;
        bubble.querySelectorAll('.copilot-actions, .copilot-reasons').forEach(function (n) {
            n.remove();
        });

        var box = document.createElement('div');
        box.className = 'copilot-actions';

        var copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'copilot-act';
        copy.textContent = '复制';
        copy.title = '复制 Markdown 原文';
        copy.addEventListener('click', function () {
            var text = bubble.dataset.plain || '';
            if (!text) return;
            copyText(text).then(function () {
                copy.textContent = '已复制';
                window.setTimeout(function () { copy.textContent = '复制'; }, 1600);
            }, function () { copy.textContent = '复制失败'; });
        });
        box.appendChild(copy);

        /* 导出只在"这条回答确实引用了记录"时出现：没有引用就肯定导出不出东西，
           按钮放出来只会让人困惑。存草稿还要看知识库新建权限（bootstrap 回传）。 */
        if (bubble.dataset.hasRefs === '1' && !bubble.classList.contains('is-restricted')) {
            var exp = document.createElement('button');
            exp.type = 'button';
            exp.className = 'copilot-act';
            exp.textContent = '导出 Excel';
            exp.title = '把这条回答引用的记录导成表格';
            exp.addEventListener('click', function () { downloadAnswer(bubble, exp); });
            box.appendChild(exp);

            if (draftAllowed) {
                var draft = document.createElement('button');
                draft.type = 'button';
                draft.className = 'copilot-act';
                draft.textContent = '存草稿';
                draft.title = '生成知识库草稿（需人工编辑后发布）';
                draft.addEventListener('click', function () { saveDraft(bubble, draft); });
                box.appendChild(draft);
            }
        }

        var up = document.createElement('button');
        up.type = 'button';
        up.className = 'copilot-act';
        up.dataset.vote = '1';
        up.textContent = '有用';
        up.classList.toggle('is-on', vote === 1);
        box.appendChild(up);

        var down = document.createElement('button');
        down.type = 'button';
        down.className = 'copilot-act';
        down.dataset.vote = '-1';
        down.textContent = '没用';
        down.classList.toggle('is-on', vote === -1);
        box.appendChild(down);

        bubble.appendChild(box);

        var reasons = document.createElement('div');
        reasons.className = 'copilot-reasons';
        reasons.hidden = true;
        FEEDBACK_REASONS.forEach(function (item) {
            var chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'copilot-chip';
            chip.textContent = item.label;
            chip.addEventListener('click', function () {
                submitFeedback(bubble, messageId, -1, item.code);
            });
            reasons.appendChild(chip);
        });
        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'copilot-chip is-ghost';
        close.textContent = '取消';
        close.addEventListener('click', function () { reasons.hidden = true; });
        reasons.appendChild(close);
        bubble.appendChild(reasons);

        up.addEventListener('click', function () {
            submitFeedback(bubble, messageId, 1, '');
        });
        down.addEventListener('click', function () {
            reasons.hidden = !reasons.hidden;
        });
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
        renderActions(bubble, data.message_id, data.vote || 0);
        setBusy(false);
        log.scrollTop = log.scrollHeight;
    }

    /* ---------- 流式（SSE）与轮询降级 ----------
       模型链路中位 4 秒、P95 5.8 秒，原先这段时间界面只有"正在处理…"。
       现在先建 SSE 收增量；拿不到（旧浏览器 / 多进程部署 / 代理不支持）就
       退回原来的 800ms 轮询，功能不降级，只是没那么"跟手"。 */
    function streamingBubble(bubble, text) {
        var body = bubble.querySelector('.copilot-md.is-streaming');
        if (!body) {
            bubble.textContent = '';
            body = document.createElement('div');
            body.className = 'copilot-md is-streaming';
            bubble.classList.add('has-md');
            bubble.appendChild(body);
        }
        body.textContent = text;
        log.scrollTop = log.scrollHeight;
    }

    function appendStep(bubble, step) {
        var steps = [];
        try { steps = JSON.parse(bubble.dataset.steps || '[]'); } catch (err) { steps = []; }
        steps.push(step);
        bubble.dataset.steps = JSON.stringify(steps);
        renderSteps(bubble, steps);
        log.scrollTop = log.scrollHeight;
    }

    function stream(runId, bubble) {
        var finished = false;
        var buffer = '';
        var timeout = window.setTimeout(function () {
            if (!finished) { finished = true; source.close(); poll(runId, bubble); }
        }, 180000);
        var source = new EventSource('/copilot/run/' + runId + '/stream/');

        function finish() {
            if (finished) return;
            finished = true;
            window.clearTimeout(timeout);
            source.close();
            request('/copilot/run/' + runId + '/').then(function (data) {
                finalize(bubble, data);
            }).catch(function (err) {
                bubble.classList.add('is-error');
                bubble.textContent = '出错了：' + err.message;
                setBusy(false);
            });
        }

        source.addEventListener('token', function (event) {
            var data = {};
            try { data = JSON.parse(event.data || '{}'); } catch (err) { return; }
            buffer += data.text || '';
            streamingBubble(bubble, buffer);
        });
        source.addEventListener('step', function (event) {
            var step = {};
            try { step = JSON.parse(event.data || '{}'); } catch (err) { return; }
            appendStep(bubble, step);
        });
        source.addEventListener('done', finish);
        source.onerror = function () {
            // EventSource 正常关闭也会触发 onerror：只有"还没收到 done"才降级，
            // 否则每次成功都会多打一轮轮询。
            if (finished) return;
            finished = true;
            window.clearTimeout(timeout);
            source.close();
            poll(runId, bubble);
        };
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
            if (window.EventSource) {
                stream(data.run_id, bubble);
            } else {
                poll(data.run_id, bubble);
            }
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
            if (m.restricted) bubble.classList.add('is-restricted');
            renderSteps(bubble, m.steps);
            renderRefs(bubble, m.refs);
            renderActions(bubble, m.id, m.vote || 0);
        });
        log.scrollTop = log.scrollHeight;
    }

    /* ---------- 历史会话 ----------
       以前只能看"最近一次"，昨天的分析翻不回来。抽屉里的每一项都能切过去，
       也能改名/删除；删除是软删（后端把 status 置 archived），30 天后才物理清。 */
    function openHistory() {
        if (!historyBox) return;
        historyBox.hidden = false;
        loadConversations();
    }

    function closeHistory() {
        if (historyBox) historyBox.hidden = true;
    }

    function loadConversations() {
        historyList.textContent = '正在加载…';
        request('/copilot/conversations/').then(function (data) {
            renderConversationList(data.items || []);
        }).catch(function (err) {
            historyList.textContent = '加载失败：' + err.message;
        });
    }

    function renderConversationList(items) {
        historyList.textContent = '';
        if (!items.length) {
            historyList.textContent = '还没有历史会话。';
            return;
        }
        items.forEach(function (item) {
            var row = document.createElement('div');
            row.className = 'copilot-history-item';
            if (item.id === state.conversationId) row.classList.add('is-current');

            var main = document.createElement('button');
            main.type = 'button';
            main.className = 'copilot-history-main';
            var title = document.createElement('span');
            title.className = 'copilot-history-title';
            title.textContent = item.title;
            var meta = document.createElement('span');
            meta.className = 'copilot-history-meta';
            meta.textContent = item.last_active_at + ' · ' + item.message_count + ' 条';
            main.appendChild(title);
            main.appendChild(meta);
            main.addEventListener('click', function () {
                loadConversation(item.id);
            });
            row.appendChild(main);

            var rename = document.createElement('button');
            rename.type = 'button';
            rename.className = 'copilot-history-op';
            rename.textContent = '改名';
            rename.addEventListener('click', function () {
                renameConversation(item);
            });
            row.appendChild(rename);

            var del = document.createElement('button');
            del.type = 'button';
            del.className = 'copilot-history-op is-danger';
            del.textContent = '删除';
            del.addEventListener('click', function () {
                deleteConversation(item);
            });
            row.appendChild(del);

            historyList.appendChild(row);
        });
    }

    function loadConversation(id) {
        request('/copilot/conversation/' + id + '/').then(function (data) {
            state.conversationId = data.conversation_id;
            renderHistory((data.messages || []));
            examplesBox.textContent = '';
            hint.textContent = '已切换到「' + (data.title || '历史会话') + '」，可继续追问。';
            closeHistory();
        }).catch(function (err) {
            hint.textContent = '切换失败：' + err.message;
        });
    }

    function renameConversation(item) {
        var title = window.prompt('会话名称', item.title);
        if (title === null) return;
        title = title.trim();
        if (!title) return;
        request('/copilot/conversation/' + item.id + '/rename/', {
            method: 'POST', body: { title: title }
        }).then(function () {
            loadConversations();
        }).catch(function (err) {
            hint.textContent = '改名失败：' + err.message;
        });
    }

    function deleteConversation(item) {
        if (!window.confirm('删除会话「' + item.title + '」？删除后当前对话内容也会清空。')) return;
        request('/copilot/conversation/' + item.id + '/delete/', { method: 'POST' })
            .then(function () {
                if (item.id === state.conversationId) startNewChat();
                loadConversations();
            }).catch(function (err) {
                hint.textContent = '删除失败：' + err.message;
            });
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
    if (historyBtn) historyBtn.addEventListener('click', openHistory);
    if (historyClose) historyClose.addEventListener('click', closeHistory);

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
        if (event.key === 'Escape' && !panel.hidden) {
            if (historyBox && !historyBox.hidden) closeHistory();
            else closePanel();
        }
    });

    /* ---------- 场景入口（列表页 / 详情页的「问 AI」） ----------
       真实用法是"我看到这一行，想立刻追问"，而不是打开面板重新描述。
       页面上任何带 data-copilot-ask 的元素点击 → 打开面板并预填；
       同时把 window.copilotAsk 暴露出去给既有脚本调用。

       刻意只**预填不自动发送**：AI 替用户决定"查什么"比少敲几个字危险得多，
       用户至少要能看一眼问题再决定要不要发。 */
    function askWith(text) {
        text = (text || '').trim();
        if (!text) return;
        if (panel.hidden) openPanel();
        input.value = text;
        input.focus();
        if (input.setSelectionRange) {
            try { input.setSelectionRange(text.length, text.length); } catch (err) { /* 不支持就算了 */ }
        }
        // 别让上一次的提示语还挂着，用户会以为是这次操作的报错
        hint.textContent = '已带入问题，可修改后回车发送。';
    }

    window.copilotAsk = askWith;

    document.addEventListener('click', function (event) {
        var trigger = event.target && event.target.closest
            && event.target.closest('[data-copilot-ask]');
        if (!trigger) return;
        // 行内按钮通常也在整行跳转的点击范围内，必须拦住冒泡
        event.preventDefault();
        event.stopPropagation();
        askWith(trigger.getAttribute('data-copilot-ask'));
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
            draftAllowed = !!data.can_save_draft;
            moduleLabels = {};
            (data.modules || []).forEach(function (m) { moduleLabels[m.key] = m.label; });
            renderExamples(examplesCache);
            // 会话直达页（通知链接落地到这里）：直接切到指定会话，
            // 而不是按默认逻辑恢复"最近一次"——否则点通知会飞到别的话题。
            var target = window.COPILOT_CONVERSATION_ID;
            if (target) {
                openPanel();
                loadConversation(target);
            } else {
                restoreHistory();
            }
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
