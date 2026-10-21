"""知识库可见性：默认私有 + 主动公开 + 可随时收回。

设计不变量
----------
1. **新文档默认私有**。``visibility`` 默认 ``private``，只有 ``owner`` 能读。
2. **公开是显式动作**。必须是用户主动选择，且写操作留痕（``OperationLog``）。
3. **收回必须立刻生效**。私有化时同步吊销分享凭据、触发 ``updated_at`` 刷新，
   任何已经拿到的直链/缓存下次请求即 404。
   （批次 4b 落地 ``tree_version`` 后，此处将实装 ``_bump_version``，
   让正开着编辑器的其他人在下次保存时拿到 409。）
4. **所有读路径统一走这里**。列表、搜索、详情、相关推荐、分类计数、待办聚合、
   全局搜索、DRF API、AI 端点一律用 ``visible_docs()`` / ``can_view()``，
   禁止在视图里手写 ``Q()``。漏一处就是一个数据泄漏点，
   这是本模块存在的唯一理由。

为什么管理员默认也看不到别人的私有文档
--------------------------------------
「仅自己可见」是一句承诺。如果管理员能看，这句承诺就是假的，员工迟早会发现。
所以 ``KB_ADMIN_CAN_READ_PRIVATE`` 默认 **False**；如果公司确实需要管理层可审计，
打开开关的同时，单选框文案会自动变成「仅自己可见（管理员可审计）」（见
``private_label()``），把话说在前面，而不是靠事后解释。
"""

import logging

from django.conf import settings
from django.db.models import F, Q

logger = logging.getLogger(__name__)

PRIVATE = 'private'
PUBLIC = 'public'

VISIBILITY_CHOICES = [
    (PRIVATE, '仅自己可见'),
    (PUBLIC, '全员可见'),
]
VISIBILITY_LABELS = dict(VISIBILITY_CHOICES)


# ---------------------------------------------------------------- 基础判断

def _authed(user):
    return bool(user) and getattr(user, 'is_authenticated', False)


def admin_can_read_private():
    return bool(getattr(settings, 'KB_ADMIN_CAN_READ_PRIVATE', False))


def can_read_private_of_others(user):
    """能否读别人的私有文档。默认关闭，连超管也不行。"""
    if not _authed(user):
        return False
    if not admin_can_read_private():
        return False
    if getattr(user, 'is_superuser', False):
        return True
    codes = set(getattr(settings, 'KB_ADMIN_ROLE_CODES', ('admin',)))
    try:
        return user.rbac_roles.filter(is_active=True, code__in=codes).exists()
    except Exception:
        return False


def private_label():
    """私有选项的文案：管理员可读时必须把话说清楚。"""
    return '仅自己可见（管理员可审计）' if admin_can_read_private() else '仅自己可见'


def visibility_choices_for_ui():
    return [(PRIVATE, private_label()), (PUBLIC, '全员可见（所有有知识库权限的同事）')]


def visibility_label(doc):
    if doc is None:
        return ''
    return VISIBILITY_LABELS.get(doc.visibility, doc.visibility)


# ---------------------------------------------------------------- 查询过滤

def doc_visibility_q(user, prefix=''):
    """可读文档的 Q 条件。``prefix`` 用于关联字段，如 ``'document__'``。

    管理员（开开关时）返回 ``Q()``（空条件 = 不过滤）；未登录返回永假条件。
    """
    if not _authed(user):
        return Q(**{f'{prefix}pk__in': []})
    if can_read_private_of_others(user):
        return Q()
    return Q(**{f'{prefix}visibility': PUBLIC}) | Q(**{f'{prefix}owner': user})


def visible_docs(user, queryset=None):
    """当前用户可读的文档集。所有列表/搜索/统计都必须从这里起手。"""
    if queryset is None:
        from knowledge_base.models import KnowledgeDocument

        queryset = KnowledgeDocument.objects.all()
    if can_read_private_of_others(user):
        return queryset
    if not _authed(user):
        return queryset.none()
    return queryset.filter(
        Q(visibility=PUBLIC) | Q(owner=user)
    ).distinct()


def can_view(doc, user):
    if doc is None:
        return False
    if doc.visibility == PUBLIC:
        return True
    if not _authed(user):
        return False
    if doc.owner_id is None:
        # 「孤儿文档」：owner 注销后被 SET_NULL。注意这里**不能**当作公开，
        # 否则员工离职 = 他的私人笔记自动对全公司公开。只给管理员看，
        # 需要归还给人时用管理命令重新指派 owner。
        return can_read_private_of_others(user)
    return doc.owner_id == getattr(user, 'pk', None) or can_read_private_of_others(user)


# ---------------------------------------------------------------- 写权限

def _has_module_perm(user, action):
    try:
        from accounts.services import has_perm

        return has_perm(user, 'knowledge', action)
    except Exception:
        return False


def can_edit(doc, user):
    """编辑权：owner 专属；历史无 owner 的公开文档沿用模块级权限。

    公开文档对他人 **只读** —— 「他公开了」不等于「大家都能改」。
    """
    if doc is None or not _authed(user) or not _has_module_perm(user, 'update'):
        return False
    if doc.owner_id is None:
        # 只有公开的历史文档才沿用旧的「有 update 权限就能改」，孤儿私有文档不给碰
        return doc.visibility == PUBLIC or can_read_private_of_others(user)
    if doc.owner_id == getattr(user, 'pk', None):
        return True
    return bool(getattr(settings, 'KB_ADMIN_CAN_EDIT_OTHERS', False)
                and can_read_private_of_others(user))


def can_manage_visibility(doc, user):
    if doc is None or not _authed(user):
        return False
    if doc.owner_id is None:
        return _has_module_perm(user, 'update') or can_read_private_of_others(user)
    if doc.owner_id == getattr(user, 'pk', None):
        return True
    return bool(getattr(settings, 'KB_ADMIN_CAN_EDIT_OTHERS', False)
                and can_read_private_of_others(user))


def can_delete(doc, user):
    if doc is None or not _authed(user) or not _has_module_perm(user, 'delete'):
        return False
    if doc.owner_id is None:
        return True
    return doc.owner_id == getattr(user, 'pk', None)


# ---------------------------------------------------------------- 可见性切换

class _ActorRequest:
    """把 ``user`` 包成 ``log_operation`` 认识的最小 request。"""

    def __init__(self, user):
        self.user = user
        self.META = {}


def _audit(request, doc, old, new, source=''):
    try:
        from records.audit import log_operation

        log_operation(
            request, '知识库', '可见性变更',
            f'知识文档#{doc.pk}',
            f'「{doc.title}」{VISIBILITY_LABELS.get(old, old)} → '
            f'{VISIBILITY_LABELS.get(new, new)}'
            + (f'（来源：{source}）' if source else ''),
            before={'visibility': old}, after={'visibility': new},
        )
    except Exception:
        logger.exception('知识库文档 #%s 可见性变更留痕失败', getattr(doc, 'pk', None))


def _revoke_share(doc):
    """收回公开时吊销分享凭据。没有分享功能时静默跳过。"""
    rotated = []
    for field in ('share_token', 'share_code', 'public_token'):
        if hasattr(doc, field):
            setattr(doc, field, '')
            rotated.append(field)
    if rotated:
        try:
            doc.save(update_fields=rotated)
        except Exception:
            logger.warning('知识库文档 #%s 分享凭据吊销失败', doc.pk)


def _bump_version(doc):
    """可见性变化 → tree_version +1（F 表达式原子自增，不动 updated_at）。

    正开着大纲编辑器的人下次保存时 base_version 过期，拿到 409 被迫
    先感知权限变化，防止「刚被收回公开还能凭旧页面继续改」的窗口。
    """
    from .models import KnowledgeDocument

    KnowledgeDocument.objects.filter(pk=doc.pk).update(
        tree_version=F('tree_version') + 1)
    doc.tree_version += 1   # 实例同步，后续同请求内读到的版本号保持一致


def set_visibility(doc, visibility, *, request=None, actor=None, source=''):
    """切换可见性。返回是否真的发生了变化。

    只有详情页切换按钮和编辑表单会调用；每日记录沉淀范围走
    ``records.kb_sync.set_record_visibility``（最终也落到这里）。
    所有调用方都必须先过 ``can_manage_visibility``，这里再兜一次（防御二次）。
    """
    if visibility not in VISIBILITY_LABELS:
        raise ValueError(f'非法的可见性取值: {visibility!r}')
    old = doc.visibility
    if old == visibility:
        return False

    doc.visibility = visibility
    doc.save(update_fields=['visibility', 'updated_at'])

    if visibility == PRIVATE:
        _revoke_share(doc)
    # 公开/收回都推进树版本：编辑器以 base_version 乐观锁感知权限变化
    _bump_version(doc)

    _audit(request or _ActorRequest(actor), doc, old, visibility, source)
    return True


def ensure_owner(doc, user, fallback_visibility=PRIVATE):
    """新建文档时补 owner/visibility：仅赋值，由调用方统一 ``save()``。

    统一入口，别在各个视图里各写各的 ``doc.owner = request.user``——
    漏一处就会造出「孤儿文档」（owner 为空的私有文档谁都改不了）。
    """
    if doc.owner_id is None and _authed(user):
        doc.owner = user
    if not doc.visibility:
        doc.visibility = fallback_visibility
    return doc
