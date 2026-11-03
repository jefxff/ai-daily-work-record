/* ==========================================================================
   应用外壳交互（从 records/base.html 内联脚本拆出）
   --------------------------------------------------------------------------
   内容：移动端侧栏抽屉、顶部用户菜单、通知铃铛轮询、Toast、确认弹窗、
        表单弹窗（AJAX）、筛选自动提交等"每页都要跑"的逻辑。
   为什么拆：base.html 曾内联 600+ 行脚本，每次翻页都重新下载、无法缓存，
   CSP 也必须为这段开 nonce。拆到静态文件后由浏览器缓存，模板只剩一个
   <script src>，CSP 侧只依赖 'self'。
   依赖：window.getCsrfToken（static/js/csrf.js）、body[data-url-notifications]。
   ========================================================================== */
document.addEventListener('DOMContentLoaded', function() {
    // 顶栏的全站搜索与内容区首页内搜索职责不同，二者可以共存：
    // 页面操作区已下沉到 .page-actions（2026-10 阶段一），顶栏不再承载页面按钮，
    // 原先"检测到页面搜索框就隐藏全站搜索"的逻辑随之移除。
    const mobileMenuToggle = document.getElementById('mobileMenuToggle');
    const sidebar = document.getElementById('sidebar');
    const sidebarScrim = document.getElementById('sidebarScrim');
    if (mobileMenuToggle && sidebar) {
        // 抽屉语义：开合状态同步 aria-expanded / body.sidebar-open（锁滚动）/ 遮罩
        const setOpen = function (open) {
            sidebar.classList.toggle('show', open);
            mobileMenuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            document.body.classList.toggle('sidebar-open', open);
        };
        mobileMenuToggle.addEventListener('click', function () {
            setOpen(!sidebar.classList.contains('show'));
        });
        if (sidebarScrim) {
            sidebarScrim.addEventListener('click', function () { setOpen(false); });
        }
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && sidebar.classList.contains('show')) {
                setOpen(false);
                mobileMenuToggle.focus();
            }
        });
        // 视口回到桌面宽度时复位，避免"半开"状态残留
        window.addEventListener('resize', function () {
            if (window.innerWidth > 960) setOpen(false);
        });
    }
    // 顶部用户菜单
    const navUserArea = document.getElementById('navUserArea');
    const navUserMenu = document.getElementById('navUserMenu');
    if (navUserArea && navUserMenu) {
        navUserArea.addEventListener('click', function(e) {
            e.stopPropagation();
            navUserMenu.classList.toggle('show');
        });
        document.addEventListener('click', function(e) {
            if (!navUserArea.contains(e.target)) {
                navUserMenu.classList.remove('show');
            }
        });
    }
    // 外观主题切换：仅作用于 <html data-theme>，不改动任何业务数据
    (function () {
        var btn = document.getElementById('themeToggleBtn');
        var label = document.getElementById('themeToggleLabel');
        function isDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }
        function paint() {
            var dark = isDark();
            if (label) label.textContent = dark ? '外观：深色' : '外观：浅色';
            if (btn) btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
        }
        paint();
        if (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();  // 保持菜单展开，便于看到外观标签即时更新
                var next = isDark() ? 'light' : 'dark';
                document.documentElement.setAttribute('data-theme', next);
                try { localStorage.setItem('adw-theme', next); } catch (e) {}
                paint();
            });
        }
    })();
    document.addEventListener('click', function(event) {
        if (window.innerWidth <= 576) {
            if (sidebar && !sidebar.contains(event.target) && mobileMenuToggle && !mobileMenuToggle.contains(event.target)) {
                sidebar.classList.remove('show');
            }
        }
    });
    document.addEventListener('click', function(event) {
        document.querySelectorAll('.dropdown').forEach(dropdown => {
            const toggle = dropdown.querySelector('.dropdown-toggle');
            const menu = dropdown.querySelector('.dropdown-menu');
            if (toggle && menu) {
                const isClickInside = dropdown.contains(event.target);
                if (!isClickInside) {
                    menu.classList.remove('show');
                    toggle.setAttribute('aria-expanded', 'false');
                }
            }
        });
    });

    // 0) 筛选下拉面板（各列表页共用，样式见 app.css「筛选下拉面板」）
    //    原先 8 个列表页各写一份内联脚本，行为还不一致（有的没有 Esc、
    //    有的不重提交、重置只是把当前筛选值再提交一遍）。这里统一：
    //    开合 + aria-expanded 同步 + Esc 关闭 + 点击外部关闭 + 重置筛选。
    (function () {
        var boxes = Array.prototype.slice.call(
            document.querySelectorAll('.filter-container, .filter-button')
        );
        if (!boxes.length) return;
        var items = [];

        function setOpen(item, open) {
            item.panel.classList.toggle('show', open);
            item.btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        // 「重置」必须把字段清空再提交：form.reset() 只是恢复成初始 HTML 里的
        // value，而那些 value 正是当前已生效的筛选值，等于什么都没重置。
        function resetForm(form) {
            Array.prototype.forEach.call(
                form.querySelectorAll('input:not([type="hidden"]), select, textarea'),
                function (el) {
                    if (el.type === 'radio' || el.type === 'checkbox') el.checked = false;
                    else el.value = '';
                }
            );
            // 单选组恢复「全部」（value 为空的选项）
            Array.prototype.forEach.call(form.querySelectorAll('input[type="radio"]'), function (el) {
                if (el.value === '') el.checked = true;
            });
            // 空字段不参与提交：否则重置后地址栏会留下一串 ?search=&customer_name=&…
            // （disabled 的表单控件不会被序列化，页面随即跳转，无需恢复）
            Array.prototype.forEach.call(form.querySelectorAll('input, select'), function (el) {
                if (el.type === 'hidden' && (el.name === 'csrfmiddlewaretoken' || el.name === 'page')) return;
                if (!el.value) el.disabled = true;
            });
            if (form.requestSubmit) form.requestSubmit(); else form.submit();
        }

        boxes.forEach(function (box) {
            var btn = box.querySelector('button[id="filterBtn"], .filter-toggle');
            var panel = box.querySelector('.filter-dropdown');
            if (!btn || !panel) return;
            btn.setAttribute('type', 'button');
            btn.setAttribute('aria-expanded', 'false');
            if (panel.id) btn.setAttribute('aria-controls', panel.id);
            var item = { btn: btn, panel: panel };
            items.push(item);

            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var open = !panel.classList.contains('show');
                items.forEach(function (it) { setOpen(it, false); });
                if (open) setOpen(item, true);
            });
            // 面板内部的点击（选择、输入、滚动）不算「点击外部」
            panel.addEventListener('click', function (e) { e.stopPropagation(); });

            var form = panel.querySelector('form');
            if (!form) return;
            form.addEventListener('submit', function () { setOpen(item, false); });
            Array.prototype.forEach.call(panel.querySelectorAll('.filter-reset'), function (rb) {
                if (rb.tagName !== 'BUTTON') return;   // 重置为链接的页面交给浏览器跳转
                rb.setAttribute('type', 'button');
                rb.addEventListener('click', function () { resetForm(form); });
            });
        });

        document.addEventListener('click', function () {
            items.forEach(function (it) { setOpen(it, false); });
        });
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            items.forEach(function (it) {
                if (it.panel.classList.contains('show')) {
                    setOpen(it, false);
                    it.btn.focus();
                }
            });
        });
    })();

    // ===== 全局交互组件 =====
    // 1) 搜索框防抖：输入停顿 500ms 后自动提交所在表单
    //    三条保护，否则会出现「还没输入内容，页面就跳走」：
    //    - 输入法组词期间（拼音尚未上屏）不提交，compositionend 后重新计时；
    //    - 值为空不自动提交，清空输入框不等于搜索空串；
    //    - 与上次已提交的值相同不再提交，浏览器恢复表单/重复事件不会误触发。
    document.querySelectorAll('.search-input').forEach(function (inp) {
        let composing = false;
        let lastSubmitted = inp.value.trim();
        let timer = null;

        function schedule() {
            clearTimeout(timer);
            if (composing) return;
            const value = inp.value.trim();
            if (!value || value === lastSubmitted) return;
            timer = setTimeout(function () {
                if (composing || !inp.form) return;
                lastSubmitted = inp.value.trim();
                inp.form.requestSubmit();
            }, 500);
        }

        inp.addEventListener('compositionstart', function () {
            composing = true;
            clearTimeout(timer);
        });
        inp.addEventListener('compositionend', function () {
            composing = false;
            schedule();
        });
        inp.addEventListener('input', function (e) {
            if (e.isComposing || composing) return;
            schedule();
        });
        if (inp.form) {
            inp.form.addEventListener('submit', function () {
                lastSubmitted = inp.value.trim();
            });
        }
    });

    // 2) 「选完即提交」的下拉（每页条数、状态快捷筛选）
    //    以前写成内联 onchange="this.form.submit()"，那是 CSP 里
    //    script-src-attr 'unsafe-inline' 唯一的下不去的原因；改用委托后
    //    模板只留 data-auto-submit，内联事件处理器可以彻底清零。
    document.addEventListener('change', function (e) {
        var el = e.target.closest('[data-auto-submit]');
        if (!el || !el.form) return;
        if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    });

    // 3) 顶栏全站搜索：点输入框内的原生 × 清空后回到「无搜索词」的结果。
    //    列表页的 .search-input 是边打字边查（500ms 防抖），全站搜索开销大，
    //    不走防抖，只补上「清除即重置」这一个缺失动作。
    document.querySelectorAll('.nav-search-input').forEach(function (inp) {
        inp.addEventListener('search', function () {
            if (!inp.value && inp.form) inp.form.requestSubmit();
        });
    });

    // 4) 筛选按钮角标：面板收起时也要能看出「有几个条件在生效」
    document.querySelectorAll('button[id="filterBtn"], .filter-toggle').forEach(function (btn) {
        if (btn.querySelector('.filter-badge')) return;
        var active = 0;
        new URLSearchParams(window.location.search).forEach(function (value, key) {
            if (key !== 'page' && key !== 'per_page' && value) active += 1;
        });
        if (!active) return;
        var badge = document.createElement('span');
        badge.className = 'filter-badge';
        badge.textContent = active;
        badge.setAttribute('aria-label', '已生效 ' + active + ' 个筛选条件');
        btn.appendChild(badge);
    });

    // 焦点陷阱：模态打开期间 Tab 在弹窗内循环，避免焦点逃逸到背景页面
    function trapFocus(container, isOpen) {
        container.addEventListener('keydown', function (e) {
            if (e.key !== 'Tab' || !isOpen()) return;
            var nodes = container.querySelectorAll(
                'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
            );
            var focusables = Array.prototype.filter.call(nodes, function (n) {
                return n.offsetParent !== null;
            });
            if (!focusables.length) return;
            var first = focusables[0];
            var last = focusables[focusables.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        });
    }

    // 2) 全局确认模态：替代原生 confirm（元素加 data-confirm="提示文案"）
    (function () {
        var backdrop = document.getElementById('confirmBackdrop');
        if (!backdrop) return;
        var pendingEl = null;
        var lastFocused = null;
        var confirmOk = document.getElementById('confirmOk');
        var isOpen = function () { return backdrop.classList.contains('show'); };
        trapFocus(backdrop, isOpen);
        var close = function () {
            backdrop.classList.remove('show');
            pendingEl = null;
            // 焦点归还触发元素，避免键盘用户丢失位置
            if (lastFocused && lastFocused.focus) lastFocused.focus();
            lastFocused = null;
        };
        document.addEventListener('click', function (e) {
            var el = e.target.closest('[data-confirm]');
            if (!el || el.tagName === 'FORM') return;   // 表单由下面的 submit 拦截
            e.preventDefault();
            lastFocused = document.activeElement;
            pendingEl = el;
            document.getElementById('confirmMessage').textContent = el.getAttribute('data-confirm');
            backdrop.classList.add('show');
            confirmOk.focus();
        });
        // 表单级确认：<form data-confirm="…"> 取代内联 onsubmit="return confirm(...)"
        // 内联事件处理器会逼着 CSP 一直保留 script-src-attr 'unsafe-inline'
        document.addEventListener('submit', function (e) {
            var form = e.target;
            if (!form.matches || !form.matches('form[data-confirm]')) return;
            if (form.dataset.confirmed === '1') return;
            e.preventDefault();
            lastFocused = document.activeElement;
            pendingEl = form;
            document.getElementById('confirmMessage').textContent = form.getAttribute('data-confirm');
            backdrop.classList.add('show');
            confirmOk.focus();
        }, true);
        document.getElementById('confirmCancel').addEventListener('click', close);
        backdrop.addEventListener('click', function (e) {
            if (e.target === backdrop) close();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && isOpen()) { e.preventDefault(); close(); }
        });
        confirmOk.addEventListener('click', function () {
            var el = pendingEl;
            backdrop.classList.remove('show');
            pendingEl = null;
            lastFocused = null;
            if (!el) return;
            if (el.tagName === 'FORM') {
                // requestSubmit 会走完整校验链；confirmed 标记防止再次弹确认
                el.dataset.confirmed = '1';
                if (el.requestSubmit) el.requestSubmit(); else el.submit();
                delete el.dataset.confirmed;
                return;
            }
            // 表单内的按钮（如列表行撤回按钮）：提交所属表单
            if (el.tagName === 'BUTTON' && el.form) {
                if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
                return;
            }
            if (el.getAttribute('href')) { window.location.href = el.getAttribute('href'); }
        });
    })();

    // 2.5) 通用表单弹窗：data-modal-edit / data-modal-add 触发 AJAX 表单
    (function () {
        var backdrop = document.getElementById('formModalBackdrop');
        var modal = document.getElementById('formModal');
        var titleEl = document.getElementById('formModalTitle');
        var bodyEl = document.getElementById('formModalBody');
        if (!backdrop || !modal || !titleEl || !bodyEl) return;

        var lastFocused = null;
        trapFocus(backdrop, function () { return backdrop.classList.contains('show'); });

        function openModal(title) {
            lastFocused = document.activeElement;
            titleEl.textContent = title || '编辑';
            bodyEl.innerHTML = '<div class="form-modal-loading">加载中…</div>';
            backdrop.classList.add('show');
            var closeBtn = modal.querySelector('.form-modal-close');
            if (closeBtn) closeBtn.focus();
        }
        function closeModal() {
            backdrop.classList.remove('show');
            bodyEl.innerHTML = '<div class="form-modal-loading">加载中…</div>';
            // 焦点归还触发按钮（如行内「编辑」按钮），避免键盘用户丢失位置
            if (lastFocused && lastFocused.focus) lastFocused.focus();
            lastFocused = null;
        }

        backdrop.addEventListener('click', function (e) {
            if (e.target === backdrop) closeModal();
        });
        document.addEventListener('click', function (e) {
            if (e.target.closest('[data-modal-close]')) {
                e.preventDefault();
                closeModal();
            }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && backdrop.classList.contains('show')) closeModal();
        });

        function injectForm(html) {
            bodyEl.innerHTML = html;
            var form = bodyEl.querySelector('form[data-modal-form]');
            if (form) wireFormSubmit(form);
            // 通用批量勾选：data-check-all / data-uncheck-all 的值为 form 内 checkbox 选择器
            bodyEl.querySelectorAll('[data-check-all], [data-uncheck-all]').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var scope = form || bodyEl;
                    var selector = btn.getAttribute('data-check-all') || btn.getAttribute('data-uncheck-all');
                    var checked = btn.hasAttribute('data-check-all');
                    scope.querySelectorAll(selector).forEach(function (cb) {
                        if (!cb.disabled) cb.checked = checked;
                    });
                });
            });
            // 执行注入的 script 标签（详情弹窗可能含图表等脚本）
            bodyEl.querySelectorAll('script').forEach(function (old) {
                var s = document.createElement('script');
                if (old.src) { s.src = old.src; }
                else { s.textContent = old.textContent; }
                old.parentNode.replaceChild(s, old);
            });
            // 焦点移入弹窗内首个可交互字段，键盘用户无需手动 Tab（详情弹窗无字段时保持关闭按钮焦点）
            var firstField = bodyEl.querySelector('input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])');
            if (firstField) firstField.focus();
        }

        function wireFormSubmit(form) {
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                // 弹窗表单走 fetch，文档级 submit 监听排在它之后，
                // 必填/格式校验必须在这里主动跑一次，否则会绕过 form-guard 直接提交
                if (window.validateEntryForm && !window.validateEntryForm(form)) return;
                var btn = form.querySelector('[data-modal-save]');
                if (btn) { btn.disabled = true; btn.dataset.oldText = btn.textContent; btn.textContent = '保存中…'; }
                var fd = new FormData(form);
                fetch(form.action, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
                    body: fd
                }).then(function (resp) {
                    var ct = resp.headers.get('content-type') || '';
                    if (ct.indexOf('application/json') !== -1) return resp.json();
                    return resp.text().then(function (txt) { return { ok: false, html: txt }; });
                }).then(function (data) {
                    if (data && data.ok) {
                        closeModal();
                        if (window.showToast) window.showToast(data.message || '保存成功', 'success');
                        if (data.redirect) { window.location.href = data.redirect; }
                        else { window.location.reload(); }
                    } else {
                        if (data && data.html) injectForm(data.html);
                        else if (data && data.message) { if (window.showToast) window.showToast(data.message, 'error'); }
                        else { if (window.showToast) window.showToast('保存失败', 'error'); }
                    }
                }).catch(function () {
                    if (window.showToast) window.showToast('网络错误，请重试', 'error');
                    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.oldText || '保存'; }
                });
            });
        }

        window.openFormModal = function (url, title) {
            openModal(title);
            // cache:'no-store'：详情/表单内容随模板更新，禁止浏览器复用旧的 AJAX 响应
            fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store' })
                .then(function (resp) {
                    var ct = resp.headers.get('content-type') || '';
                    if (ct.indexOf('application/json') !== -1) {
                        return resp.json().then(function (data) {
                            if (data && data.ok && data.html) return data.html;
                            throw new Error(data && data.message || '加载失败');
                        });
                    }
                    return resp.text();
                })
                .then(function (html) { injectForm(html); })
                .catch(function () {
                    if (window.showToast) window.showToast('加载表单失败', 'error');
                    closeModal();
                });
        };

        document.addEventListener('click', function (e) {
            var el = e.target.closest('[data-modal-edit], [data-modal-add], [data-modal-detail]');
            if (!el) return;
            e.preventDefault();
            var url = el.getAttribute('href');
            if (!url) return;
            var defTitle = el.hasAttribute('data-modal-add') ? '新增' : (el.hasAttribute('data-modal-detail') ? '详情' : '编辑');
            var title = el.getAttribute('data-modal-title') || defTitle;
            openFormModal(url, title);
        });
    })();

    // 3) 全局 Toast：showToast('已保存', 'success'|'error'|'info')
    window.showToast = function (message, type) {
        var c = document.getElementById('toastContainer');
        if (!c) return;
        var t = document.createElement('div');
        t.className = 'app-toast ' + (type || 'success');
        t.textContent = message;
        c.appendChild(t);
        setTimeout(function () { t.remove(); }, 3000);
    };

    // 4) 模板动态样式：模板值通过 data-* 属性传递，避免 style 属性内出现模板语法导致 IDE 报错
    // 4a) KPI 趋势条高度（首页/数据展示）
    document.querySelectorAll('.trend-bar[data-h]').forEach(function (bar) {
        bar.style.height = bar.dataset.h + 'px';
    });
    // 4b) 标签云圆点颜色（知识库文档列表）
    document.querySelectorAll('.tag-dot[data-color]').forEach(function (dot) {
        if (dot.dataset.color) dot.style.background = dot.dataset.color;
    });

    // ===== 5) 通知铃铛：30s 轮询未读数 + 下拉最近通知 =====
    (function () {
        var area = document.getElementById('navNotifyArea');
        if (!area) return; // 无 notifications 模块权限时不渲染
        var bellBtn = document.getElementById('navBellBtn');
        var badge = document.getElementById('navBellBadge');
        var menu = document.getElementById('navNotifyMenu');
        var listBox = document.getElementById('navNotifyList');
        var csrf = (window.getCsrfToken && window.getCsrfToken()) || '';

        function escapeHtml(s) {
            // 显式转义 5 个字符：textContent→innerHTML 不处理引号，
            // 而下列取值会拼进 data-* 属性，未转义引号可导致属性逃逸
            var str = s == null ? '' : String(s);
            return str.replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;')
                      .replace(/'/g, '&#39;');
        }

        function renderBadge(count) {
            if (!badge) return;
            if (count > 0) {
                badge.hidden = false;
                badge.textContent = count > 99 ? '99+' : count;
            } else {
                badge.hidden = true;
            }
            // 徽标数字是可见文本，会自然进入可访问名称（"消息通知 25"），
            // 无需再同步隐藏文本，避免名称与可见文本不一致
        }

        function renderList(items) {
            if (!items.length) {
                listBox.innerHTML = '<div class="nav-notify-empty">暂无通知</div>';
                return;
            }
            listBox.innerHTML = items.map(function (n) {
                var agg = n.agg_count > 1 ? '<span class="notif-agg-badge">合并 ' + n.agg_count + ' 条</span>' : '';
                return '<div class="nav-notify-item ' + (n.is_read ? '' : 'unread') + '" data-id="' + n.id + '" data-link="' + escapeHtml(n.link || '') + '">'
                    + '<div style="flex:1;min-width:0;">'
                    + '<div class="nav-notify-item-title">' + escapeHtml(n.title) + agg + '</div>'
                    + '<div class="nav-notify-item-content">' + escapeHtml(n.content) + '</div>'
                    + '<div class="nav-notify-item-meta">' + escapeHtml(n.category_label) + ' · ' + escapeHtml(n.created_at) + '</div>'
                    + '</div></div>';
            }).join('');
            listBox.querySelectorAll('.nav-notify-item').forEach(function (el) {
                el.addEventListener('click', function () {
                    var id = el.getAttribute('data-id');
                    var link = el.getAttribute('data-link');
                    fetch('/notifications/read/' + id + '/', {
                        method: 'POST',
                        headers: { 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest' },
                        body: new URLSearchParams({})
                    }).then(function (r) { return r.text(); }).catch(function () {}).finally(function () {
                        window.location.href = link || document.body.getAttribute('data-url-notifications') || '/notifications/';
                    });
                });
            });
        }

        function refresh() {
            fetch('/notifications/api/unread/', {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data || !data.ok) return;
                renderBadge(data.count);
                // 下拉未展开时只更新徽标数，避免 30s 轮询无条件重绘整份列表
                if (menu.classList.contains('show')) {
                    renderList(data.items || []);
                }
            }).catch(function () {});
        }

        bellBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            var willShow = !menu.classList.contains('show');
            menu.classList.toggle('show');
            if (willShow) refresh();
        });
        document.addEventListener('click', function (e) {
            if (!area.contains(e.target)) menu.classList.remove('show');
        });
        refresh();
        setInterval(refresh, 30000);
    })();

    // 工具栏「更多 ▾」（details.tb-more）：点击外部或按 Esc 收起。
    // 原生 <details> 只有 summary 能切换，点击别处不会关闭，与其他下拉行为不一致。
    document.addEventListener('click', function (e) {
        document.querySelectorAll('details.tb-more[open]').forEach(function (d) {
            if (!d.contains(e.target)) d.removeAttribute('open');
        });
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        document.querySelectorAll('details.tb-more[open]').forEach(function (d) {
            d.removeAttribute('open');
            var summary = d.querySelector('summary');
            if (summary) summary.focus();
        });
    });

    // 全屏搜索层（≤992px 顶栏搜索框被隐藏时由图标唤起）。
    // 只有一个输入框，所以不做二次提交按钮：回车即提交到 /search/。
    (function () {
        var trigger = document.getElementById('navSearchTrigger');
        var sheet = document.getElementById('searchSheet');
        if (!trigger || !sheet) return;
        var input = document.getElementById('searchSheetInput');
        var cancel = document.getElementById('searchSheetCancel');
        var lastFocus = null;

        function open() {
            lastFocus = document.activeElement;
            sheet.classList.add('show');
            trigger.setAttribute('aria-expanded', 'true');
            if (input) { input.value = ''; input.focus(); }
        }
        function close() {
            sheet.classList.remove('show');
            trigger.setAttribute('aria-expanded', 'false');
            if (lastFocus && lastFocus.focus) lastFocus.focus();
        }

        trigger.addEventListener('click', open);
        if (cancel) cancel.addEventListener('click', close);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && sheet.classList.contains('show')) close();
        });
    })();
});
