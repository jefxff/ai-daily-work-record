(function () {
    // 侧边栏折叠组统一实现（试样管理 / 档案管理 / 系统）：
    // 交互行为、动画效果、展开-收起状态的存储逻辑三者完全一致。
    // 初始展开判定：优先读取 localStorage 上次状态；无记录时按当前所属路径自动展开。
    function initCollapse(btnId, menuId, storageKey, pathPrefixes) {
        var btn = document.getElementById(btnId);
        var menu = document.getElementById(menuId);
        if (!btn || !menu) { return; }
        var stored = null;
        try { stored = localStorage.getItem(storageKey); } catch (e) { stored = null; }
        var inSection = pathPrefixes.some(function (p) {
            return window.location.pathname.indexOf(p) === 0;
        });
        var open = stored ? (stored === 'open') : inSection;
        menu.classList.toggle('show', open);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.addEventListener('click', function () {
            var willOpen = !menu.classList.contains('show');
            menu.classList.toggle('show', willOpen);
            btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
            try { localStorage.setItem(storageKey, willOpen ? 'open' : 'closed'); } catch (e) {}
        });
    }
    initCollapse('workbenchDropdown', 'workbenchDropdownMenu', 'adw-sidebar-workbench', ['/workspace', '/daily_record', '/expiry', '/knowledge', '/decoration', '/regulation', '/price_evaluation', '/insurance', '/metal_price', '/dashboard', '/data_download']);
    initCollapse('customersDropdown', 'customersDropdownMenu', 'adw-sidebar-customers', ['/customer_credit', '/customer_complaint', '/customer_ledger', '/complaint']);
    initCollapse('samplesDropdown', 'samplesDropdownMenu', 'adw-sidebar-samples', ['/samples']);
    initCollapse('archiveDropdown', 'archiveDropdownMenu', 'adw-sidebar-archive', ['/archive']);
    initCollapse('systemsDropdown', 'systemsDropdownMenu', 'adw-sidebar-system', ['/system', '/accounts', '/operation_log']);
})();
