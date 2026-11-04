/* ==========================================================================
   操作列图标微按钮 + 悬停提示气泡（从 records/base.html 内联脚本拆出）
   --------------------------------------------------------------------------
   必须在 <body> 中同步执行（本文件不做 defer 加载）：脚本一进来就给
   <html> 打 act-icons 类，让图标模式在首屏绘制前生效，否则会先看到
   文字按钮再"收窄"成图标，出现一次可见的布局跳动。
   ========================================================================== */
/* 操作列图标微按钮（渐进增强，只读 DOM 上已有的文字，不改模板与业务逻辑）
   - 从按钮文字推导动作类型写入 data-act，供 components.css §6 选图标，
     并把「详情/详细/查看/编辑/删除」在桌面端收成纯图标（§6b）。
   - 有 hover 能力的设备上，为这些图标按钮挂悬停/聚焦提示气泡；
     触屏端不挂，文字保持可见。
   - 系统管理类页面（/accounts/users、/accounts/roles、/system/backup
     等路径，见下方 OPT_OUT 清单）的按钮构成与本层假设不同（混有
     「重置密码/停用/恢复/下载」等），刻意不启用图标模式，保持原有
     「文字按钮」形态。
   刻意不用 href*="edit" 判断编辑：/customer_credit/... 里的 "credit"
   含 "edit" 子串，会把「详细」误判成编辑。 */
(function () {
    /* 不启用图标模式的页面：按 URL 前缀匹配 */
    var OPT_OUT = ['/accounts/users/', '/accounts/roles/', '/system/backup/'];
    var path = window.location.pathname;
    for (var p = 0; p < OPT_OUT.length; p++) {
        if (path.indexOf(OPT_OUT[p]) === 0) return;
    }
    /* 同步打标（本脚本在 body 内联执行），避免样式等到 DOMContentLoaded
       才生效造成图标闪一下再收窄 */
    document.documentElement.className += ' act-icons';

    var HOVERABLE = !!(window.matchMedia &&
        window.matchMedia('(hover: hover) and (pointer: fine)').matches);
    var tipEl, showTimer;

    function tipNode() {
        if (!tipEl) {
            tipEl = document.createElement('div');
            tipEl.className = 'u-tip';
            tipEl.setAttribute('role', 'tooltip');
            tipEl.hidden = true;
            document.body.appendChild(tipEl);
        }
        return tipEl;
    }

    function show(el) {
        var t = tipNode();
        t.textContent = el.getAttribute('aria-label') || '';
        t.hidden = false;
        var a = el.getBoundingClientRect(), b = t.getBoundingClientRect();
        var top = a.top - b.height - 9;
        if (top < 4) top = a.bottom + 9;          // 贴近视口顶部时翻到下方
        var max = document.documentElement.clientWidth - b.width - 6;
        tipEl.style.top = top + 'px';
        tipEl.style.left = Math.max(6, Math.min(a.left + a.width / 2 - b.width / 2, max)) + 'px';
    }

    function hide() {
        window.clearTimeout(showTimer);
        if (tipEl) tipEl.hidden = true;
    }

    /* 先看文字再看类名：文字最可靠，类名只作兜底。
       「恢复 / 下载 / 归还 / 撤销」等非图标动作一律不命中。
       保险续保视图里「时间线 / 当前保单 / 续保」都跳到不同页面，
       若统一归到 detail 会出现三个一模一样的眼睛图标；
       因此这里拆成 timeline / policy / renew 三个独立动作，
       由 components.css §6 各配一枚语义图标。 */
    function actOf(el, text) {
        /* 待办工作台的「处理」是统一入口（不同来源分别跳详情/编辑页），整列
           刻意保持纯文字（见 components.css §6 工作台专条）。若被下方
           a[href*="detail"] 兜底打上 data-act，§6b 会把文字收成 0 号字，
           而图标又被工作台专条收起——单元格彻底空白。 */
        if (el.closest('.workspace-table')) return '';
        if (text.indexOf('删除') > -1) return 'delete';
        if (text.indexOf('编辑') > -1) return 'edit';
        if (text.indexOf('续保') > -1) return 'renew';
        if (text.indexOf('时间线') > -1) return 'timeline';
        if (text.indexOf('当前保单') > -1) return 'policy';
        if (/详情|详细|查看|档案|明细/.test(text)) return 'detail';
        /* href 兜底放在语义文字之后：/insurance/subjects/detail/ 的「时间线」
           已在上面命中 timeline，不会被这条统归成 detail。 */
        if (el.matches('a[href*="detail"]')) return 'detail';
        if (el.matches('.btn-delete, .btn-delete-bk, a[href*="delete"]')) return 'delete';
        return '';
    }

    function decorate(el, act, text) {
        el.setAttribute('data-act', act);
        if (!el.getAttribute('aria-label')) el.setAttribute('aria-label', text);
        if (!HOVERABLE) return;
        el.addEventListener('mouseenter', function () {
            window.clearTimeout(showTimer);
            showTimer = window.setTimeout(function () { show(el); }, 120);
        });
        el.addEventListener('mouseleave', hide);
        el.addEventListener('focus', function () { show(el); });
        el.addEventListener('blur', hide);
    }

    function run() {
        var list = document.querySelectorAll('td[data-label="操作"] a, td[data-label="操作"] button');
        for (var i = 0; i < list.length; i++) {
            var el = list[i];
            if (el.hasAttribute('data-act')) continue;
            var text = (el.textContent || '').trim();
            if (!text) continue;
            var act = actOf(el, text);
            if (act) decorate(el, act, text);
        }
        if (HOVERABLE) window.addEventListener('scroll', hide, true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', run);
    } else {
        run();
    }
})();
