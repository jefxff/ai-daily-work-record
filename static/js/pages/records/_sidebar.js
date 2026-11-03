(function () {
    // 侧边栏折叠组统一实现（工作台 / 客户管理 / 试样管理 / 档案管理 / 系统）：
    // 交互行为、动画效果、展开-收起状态的存储逻辑完全一致。
    // 初始展开判定：优先读取 localStorage 上次状态；无记录时按当前所属路径自动展开；
    // 当前路径不属于任何分组（如首页 /、全局搜索 /search/）时按「最近访问模块」兜底，
    // 工作台分组额外兜底为展开——否则首页首屏没有任何可点的导航项。
    var LAST_KEY = 'adw-sidebar-last-path';

    function readStored(key) {
        try { return localStorage.getItem(key); } catch (e) { return null; }
    }

    function writeStored(key, value) {
        try { localStorage.setItem(key, value); } catch (e) {}
    }

    var SECTIONS = [
        {
            btn: 'workbenchDropdown',
            menu: 'workbenchDropdownMenu',
            storage: 'adw-sidebar-workbench',
            prefixes: ['/workspace', '/daily_record', '/expiry', '/knowledge', '/decoration',
                '/regulation', '/price_evaluation', '/insurance', '/metal_price', '/dashboard',
                '/data_download'],
            openOnNoMatch: true
        },
        {
            btn: 'customersDropdown',
            menu: 'customersDropdownMenu',
            storage: 'adw-sidebar-customers',
            prefixes: ['/customer_credit', '/customer_complaint', '/customer_ledger', '/complaint'],
            openOnNoMatch: false
        },
        {
            btn: 'samplesDropdown',
            menu: 'samplesDropdownMenu',
            storage: 'adw-sidebar-samples',
            prefixes: ['/samples'],
            openOnNoMatch: false
        },
        {
            btn: 'archiveDropdown',
            menu: 'archiveDropdownMenu',
            storage: 'adw-sidebar-archive',
            prefixes: ['/archive'],
            openOnNoMatch: false
        },
        {
            btn: 'systemsDropdown',
            menu: 'systemsDropdownMenu',
            storage: 'adw-sidebar-system',
            prefixes: ['/system', '/accounts', '/operation_log', '/notifications'],
            openOnNoMatch: false
        }
    ];

    function matches(path, prefixes) {
        return prefixes.some(function (p) { return path.indexOf(p) === 0; });
    }

    var path = window.location.pathname;
    var anyMatch = SECTIONS.some(function (s) { return matches(path, s.prefixes); });
    // 只有当前页不属于任何分组时才把本页记成「最近访问」，
    // 否则记录会被 /accounts/login/ 这类无分组页反复覆盖。
    if (anyMatch) { writeStored(LAST_KEY, path); }
    var fallbackPath = anyMatch ? null : readStored(LAST_KEY);

    function initCollapse(cfg) {
        var btn = document.getElementById(cfg.btn);
        var menu = document.getElementById(cfg.menu);
        if (!btn || !menu) { return; }
        var stored = readStored(cfg.storage);
        var inSection = matches(path, cfg.prefixes);
        var open;
        if (stored !== null) {
            open = stored === 'open';
        } else if (inSection) {
            open = true;
        } else if (fallbackPath) {
            open = matches(fallbackPath, cfg.prefixes);
        } else {
            open = !!cfg.openOnNoMatch;
        }
        menu.classList.toggle('show', open);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.addEventListener('click', function () {
            var willOpen = !menu.classList.contains('show');
            menu.classList.toggle('show', willOpen);
            btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
            writeStored(cfg.storage, willOpen ? 'open' : 'closed');
        });
    }

    SECTIONS.forEach(initCollapse);
})();
