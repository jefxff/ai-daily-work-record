"""导航状态与面包屑 —— 菜单高亮与层级定位的唯一事实源。

为什么需要这个模块：

1) 侧栏高亮
   原先 `_sidebar.html` 用 `{% if request.path == '/workspace/' %}` 做全等比较，
   39 个菜单项里只有 22 个带 active 条件，且详情页（`/daily_record/detail/12/`）
   永远匹配不上——用户点进任何详情页，侧栏都不再高亮。

2) 面包屑
   `records/urls.py` 没有 `app_name`，根路由 `path('', include('records.urls'))`
   也不构建命名空间，因此 `resolver_match.namespace` 恒为空。
   面包屑只能按 URL 路径前缀匹配，不能依赖命名空间。

匹配规则：
- 默认前缀比较（`path.startswith(prefix)`）；
- 前缀以 `=` 开头表示精确相等（如 `=/samples/` 只匹配试样看板首页）；
- 表内顺序即优先级，长前缀必须排在短前缀之前。

新增模块时只需在本文件登记一行，侧栏、面包屑、折叠组的自动展开同时生效。
"""

from django.urls import NoReverseMatch, reverse

# (路径前缀, 菜单键, 中文名, 上级列表页路由名或 None)
NAV_MAP = [
    ('/daily_record/', 'daily_record', '每日记录', 'daily_record_list'),
    ('/decoration/', 'decoration', '装修', 'decoration_list'),
    # 穿行测试是客户评审下的子页面，长前缀必须排在 /customer_review/ 之前
    ('/customer_review/walkthrough/', 'customer_review', '付款穿行测试', 'walkthrough_list'),
    ('/customer_review/', 'customer_review', '客户评审', 'customer_review_list'),
    ('/customer_credit/', 'customer_credit', '客户账期', 'customer_credit_list'),
    ('/customer_complaint/', 'customer_complaint', '客户客诉', 'customer_complaint_list'),
    ('/customer_ledger/', 'customer_ledger', '客户台账', 'customer_ledger_list'),
    ('/complaint/', 'complaint', '诉讼', 'complaint_list'),
    ('/regulation/', 'regulation', '制度及处罚', 'regulation_list'),
    ('/price_evaluation/', 'price_evaluation', '核价', 'price_evaluation_list'),
    ('/insurance/subjects/', 'insurance', '保险标的', 'insurance_subject_list'),
    ('/insurance/', 'insurance', '保险', 'insurance_list'),
    ('/metal_price/', 'metal_price', '金属价格', None),
    ('/knowledge/', 'knowledge', '知识库', 'knowledge_base:document_list'),
    ('/samples/', 'samples', '试样管理', 'samples:list'),
    ('/archive/', 'archive', '档案管理', 'archive:overview'),
    ('/workspace/', 'workspace', '待办工作台', None),
    ('/expiry/', 'expiry', '到期预警', 'expiry_calendar'),
    ('/dashboard/', 'data_view', '数据展示', None),
    ('/data_download/', 'data_view', '数据下载', None),
    ('/operation_log/anomalies/', 'system', '异常检测', 'audit_anomalies'),
    ('/operation_log/', 'system', '操作留痕', 'operation_log_list'),
    ('/accounts/', 'system', '系统管理', None),
    ('/system/', 'system', '系统管理', None),
    # 长前缀优先：/notifications/tasks/ 是拆页后的定时任务视图，
    # 否则会落到 /notifications/ 上、再由 SEGMENT_LABELS['tasks'] 误标成「任务历史」。
    ('/notifications/tasks/', 'notifications', '定时任务', 'notifications:task_list'),
    ('/notifications/', 'notifications', '消息通知', 'notifications:list'),
    ('/copilot/', 'copilot', 'AI 助手', None),
    ('/search/', 'search', '搜索', None),
]

# 路径末段 → 面包屑末级文案。命中不了就留空（只显示到模块级）。
SEGMENT_LABELS = {
    'add': '新增',
    'new': '新增',
    'edit': '编辑',
    'detail': '详情',
    'print': '打印',
    'walkthrough': '付款穿行测试',
    'export': '导出',
    'import': '导入',
    'template': '导入模板',
    'launch_review': '发起评审',
    'delete': '删除',
    'incomplete': '待补录',
    'subjects': '保险标的',
    'renewal_report': '续保报告',
    'anomalies': '异常检测',
    'quality': 'AI 用量质量',
    'egress': 'AI 外发审计',
    'health': '系统健康',
    'perf': '性能监控',
    'logs': '日志中心',
    'tasks': '任务历史',
    'backup': '数据备份',
}


def match_nav(path):
    """返回命中的 (prefix, key, label, route)，未命中返回 None。"""
    for prefix, key, label, route in NAV_MAP:
        if path.startswith(prefix):
            return prefix, key, label, route
    return None


def breadcrumb_tail(path, prefix):
    """详情/新增/编辑等子页面的末级文案。"""
    rest = path[len(prefix):].strip('/')
    if not rest:
        return ''
    head = rest.split('/')[0]
    if head.isdigit():
        return '详情'
    return SEGMENT_LABELS.get(head, '')


def nav(request):
    """上下文处理器：供侧栏高亮与面包屑共用一次查表。

    面包屑分两段给模板：
    - nav_crumb_mid：中间层（模块名 + 列表页链接），列表页时可省略；
    - nav_crumb_tail：末级（当前页）。列表页就是模块名，详情页是「详情」。

    列表页两者相同，模板里 `{% if mid.label != tail %}` 会自动省掉中间层，
    所以 `_breadcrumbs.html` 不需要为「列表页/详情页」写两套分支。
    """
    path = getattr(request, 'path', '') or '/'
    hit = match_nav(path)
    if not hit:
        return {
            'nav_key': '',
            'nav_prefix': '',
            'nav_crumb_mid': None,
            'nav_crumb_tail': '',
        }
    prefix, key, label, route = hit
    mid = {'label': label, 'url': ''}
    if route:
        try:
            mid['url'] = reverse(route)
        except NoReverseMatch:
            mid['url'] = ''
    tail = breadcrumb_tail(path, prefix) or label
    return {
        'nav_key': key,
        'nav_prefix': prefix,
        'nav_crumb_mid': mid,
        'nav_crumb_tail': tail,
    }
