"""每日记录 → 知识库「工作沉淀」同步。

一句话：**记录是唯一事实源，知识库里的沉淀是它的影子**。
- 每条记录同步成知识库大纲节点（KnowledgeNode，node_type='todo'）；
- 节点挂在按 (owner, 年月, 可见性) 拆分的容器文档里；
- 记录更新 → 节点原位覆盖；记录删除 → 节点移除；
- 容器空了 → 容器删除（不留空壳）；
- 切换可见性 → 节点**搬**到另一档容器（不复制，避免两处漂移）。

设计决定（与交付包差异见《01-知识库可见性方案》第十二节）：
- 容器定位一律用 KnowledgeDocument.source='daily_record_sync' 标记，
  不再用「标题含『工作沉淀』」字符串匹配——用户改了标题不该破坏同步。
- _bump_version 实装 tree_version+1：批量搬节点时挡住正开着编辑器的
  其他人，保存时拿到 409。
- 本模块所有函数都吞异常只记日志：沉淀失败绝不能影响每日记录主流程。
"""

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

REF_MODULE = 'daily_record'          # 节点业务引用标记（与 accounts.registry 对齐）
SOURCE_MARK = 'daily_record_sync'    # 容器来源指纹（定位/清理一律以此为准）
CONTAINER_MARK = '工作沉淀'          # 仅用于展示标题，不参与定位


# ---------------------------------------------------------------- 工具

def _month_key(dt):
    """容器月份标识：'2026-10'（按本地时区）。"""
    return dt.astimezone(timezone.get_current_timezone()).strftime('%Y-%m')


def _month_title(dt):
    local = dt.astimezone(timezone.get_current_timezone())
    return f'{local.year}年{local.month}月{CONTAINER_MARK}（每日记录同步）'


def _record_text(record):
    return (record.work_content or '').strip()


def _record_done(record):
    return record.status == '完成'


def _node_ref_label(record):
    due = record.due_date.strftime('%Y-%m-%d') if record.due_date else '无'
    return (f'[{record.status}|{record.progress}%|截至{due}] '
            f'{_record_text(record)[:80]}')


def _bump_version(doc):
    """容器树版本 +1（乐观锁）：节点被同步改写时，让正开着编辑器的人保存时拿到 409。"""
    from django.db.models import F

    type(doc).objects.filter(pk=doc.pk).update(tree_version=F('tree_version') + 1)


# ---------------------------------------------------------------- 容器

def _get_or_create_container(owner, record, visibility):
    """取 (owner, 月份, 可见性) 对应的容器；没有就建。定位以 source 标记为准。"""
    from knowledge_base.models import KnowledgeDocument

    month = _month_key(record.record_date)
    container = (KnowledgeDocument.objects
                 .filter(owner=owner, source=SOURCE_MARK, visibility=visibility,
                         tags__icontains=month)
                 .first())
    if container is not None:
        return container, False

    # 内容仍走 markdown 渲染链路（content 为摘要）；正文主体是节点树
    summary = f'{_month_title(record.record_date)} · 每日记录自动沉淀（可见性：{"全员可见" if visibility == "public" else "仅自己"}）'
    container = KnowledgeDocument.objects.create(
        title=_month_title(record.record_date),
        category=_default_category(),
        summary=summary,
        content=f'# {_month_title(record.record_date)}\n\n'
                f'由每日记录自动沉淀生成，按月归档。记录为本体，此处为同步视图。\n',
        tags=f'{CONTAINER_MARK},{month}',
        status='published',
        owner=owner,
        visibility=visibility,
        source=SOURCE_MARK,
    )
    return container, True


def _default_category():
    """「工作沉淀」分类（不存在则创建）。分类不设可见性（共享基础设施）。"""
    from knowledge_base.models import KnowledgeCategory

    cat, _ = KnowledgeCategory.objects.get_or_create(
        name=CONTAINER_MARK, defaults={'description': '每日记录自动沉淀'})
    return cat


def _cleanup_container(container):
    """容器没有任何业务节点了就删掉，不留空壳。

    只删 source 指纹命中的沉淀容器——用户手动建的「同标题」文档绝不能被误删，
    这是当初放弃标题匹配、改用 source 字段的原因。
    """
    from knowledge_base.models import KnowledgeDocument, KnowledgeNode

    has_nodes = KnowledgeNode.objects.filter(
        document=container).exclude(node_type='ref').exists()
    if has_nodes:
        return
    if container.source != SOURCE_MARK:
        logger.warning('拒绝清理非沉淀容器 #%s（source=%r）', container.pk, container.source)
        return
    container.delete()


# ---------------------------------------------------------------- 节点

def _node_values(record, ref_label):
    """节点字段快照（不含 parent/sort——由容器归属决定）。"""
    return dict(
        text=_record_text(record),
        node_type='todo',
        is_done=_record_done(record),
        ref_module=REF_MODULE,
        ref_object_id=record.pk,
        ref_label=ref_label[:200],
    )


def sync_record_to_kb(record, *, actor=None, request=None):
    """把单条记录同步进知识库。返回 (node, container) 或 (None, None)。

    节点全局按 (ref_module, ref_object_id) 查找——不限定容器：
    沉淀范围切换后（private↔public），旧容器里的节点会被**搬**到新容器，
    绝不复制（复制会导致两处漂移），旧容器空了自动清理。
    """
    if record is None or record.user_id is None:
        return None, None
    try:
        with transaction.atomic():
            from knowledge_base.models import KnowledgeNode

            visibility = record.kb_visibility if record.kb_visibility in ('private', 'public') else 'private'
            container, created = _get_or_create_container(record.user, record, visibility)

            ref_label = _node_ref_label(record)
            node = KnowledgeNode.objects.filter(
                ref_module=REF_MODULE, ref_object_id=record.pk).select_related('document').first()
            old_container = node.document if node is not None else None

            if node is None:
                node = KnowledgeNode(
                    document=container,
                    uid=f'r{record.pk:010d}',
                    parent=None,
                    sort=0,
                    **_node_values(record, ref_label),
                )
                # 同级追加到末尾
                last = (KnowledgeNode.objects.filter(document=container, parent=None)
                        .order_by('-sort').first())
                node.sort = (last.sort + 1) if last else 0
                node.save()
            else:
                for k, v in _node_values(record, ref_label).items():
                    setattr(node, k, v)
                if old_container.pk != container.pk:
                    # 范围切换：搬到新容器同级末尾
                    last = (KnowledgeNode.objects.filter(document=container, parent=None)
                            .order_by('-sort').first())
                    node.sort = (last.sort + 1) if last else 0
                    node.document = container
                    node.save()
                else:
                    node.save(update_fields=['text', 'node_type', 'is_done',
                                             'ref_module', 'ref_object_id', 'ref_label',
                                             'updated_at'])
                _bump_version(container)

            # 节点已生成，回填记录上的沉淀指针
            if record.kb_doc_id != container.pk or record.kb_synced_at is None:
                type(record).objects.filter(pk=record.pk).update(
                    kb_doc=container, kb_synced_at=timezone.now())
            record.kb_doc = container
            record.kb_synced_at = timezone.now()

        # 事务外清理旧容器（其内已无该记录节点；若空则删除）
        if old_container is not None and old_container.pk != container.pk:
            old_container.refresh_from_db()
            _cleanup_container(old_container)
        return node, container
    except Exception:
        logger.exception('每日记录 #%s 沉淀到知识库失败', getattr(record, 'pk', None))
        return None, None


def unsync_record(record):
    """记录不再沉淀（或记录被删除）：移除节点，容器空了就连容器一起删。"""
    try:
        from knowledge_base.models import KnowledgeNode

        node = KnowledgeNode.objects.filter(
            ref_module=REF_MODULE, ref_object_id=record.pk).select_related('document').first()
        if node is None:
            return False
        container = node.document
        node.delete()
        _bump_version(container)
        _cleanup_container(container)
        return True
    except Exception:
        logger.exception('每日记录 #%s 撤销沉淀失败', getattr(record, 'pk', None))
        return False


def set_record_visibility(record, visibility, *, actor=None, request=None):
    """记录的沉淀范围变化 → 更新范围并同步（节点会被搬到对应档位的容器）。"""
    if visibility not in ('private', 'public'):
        return False
    try:
        record.kb_visibility = visibility
        type(record).objects.filter(pk=record.pk).update(kb_visibility=visibility)
        node, _ = sync_record_to_kb(record, actor=actor, request=request)
        return node is not None
    except Exception:
        logger.exception('每日记录 #%s 切换沉淀范围失败', getattr(record, 'pk', None))
        return False


def cleanup_kb_on_record_delete(sender, instance, **kwargs):
    """DailyRecord post_delete 信号：记录删了，影子也得删。"""
    try:
        unsync_record(instance)
    except Exception:
        logger.exception('记录 #%s 删除后清理知识库节点失败', getattr(instance, 'pk', None))


def apply_flag(record, *, enabled, visibility, actor=None, request=None):
    """表单两问（要不要沉淀/给谁看）的落地入口；绝不抛异常影响主流程。"""
    try:
        if enabled:
            if visibility in ('private', 'public') and visibility != record.kb_visibility:
                record.kb_visibility = visibility
                type(record).objects.filter(pk=record.pk).update(kb_visibility=visibility)
            node, _ = sync_record_to_kb(record, actor=actor, request=request)
            if node is not None and not record.kb_enabled:
                type(record).objects.filter(pk=record.pk).update(kb_enabled=True)
                record.kb_enabled = True
        else:
            unsync_record(record)
            if record.kb_doc_id or record.kb_synced_at or record.kb_enabled:
                type(record).objects.filter(pk=record.pk).update(
                    kb_doc=None, kb_synced_at=None, kb_enabled=False)
                record.kb_doc = None
                record.kb_synced_at = None
                record.kb_enabled = False
    except Exception:
        logger.exception('每日记录 #%s 沉淀开关处理失败', getattr(record, 'pk', None))


def sync_batch(user, *, request=None, actor=None):
    """批量沉淀：把当前用户所有未沉淀记录全部沉淀（一律私有，D5 拍板）。

    返回 (成功数, 失败数)。
    """
    records = (user.daily_records.model.objects
               .filter(user=user, kb_enabled=False)
               .order_by('record_date'))
    ok = fail = 0
    for record in records:
        try:
            record.kb_visibility = 'private'   # D5：批量一律私有
            type(record).objects.filter(pk=record.pk).update(kb_visibility='private')
            node, _ = sync_record_to_kb(record, actor=actor or user, request=request)
            if node is not None:
                type(record).objects.filter(pk=record.pk).update(kb_enabled=True)
                ok += 1
            else:
                fail += 1
        except Exception:
            logger.exception('批量沉淀记录 #%s 失败', record.pk)
            fail += 1
    return ok, fail
