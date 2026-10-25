"""通知服务层：全站唯一的通知入口与状态机操作。

原则（与「每日记录」隔离模式一致）：
- 触发层只调 emit()/announce()，不感知渠道与状态机；
- 状态变更只发生在本模块（mark_* / revoke / expire），视图禁止直接 UPDATE status；
- 查询隔离：_my_notifications 唯一查询起点，_get_my_notification 行级校验（404）；
- 幂等：dedup_key 唯一约束 + get_or_create，并发下只建一条；
- 去重降噪：agg_key 窗口内合并计数，不重复打扰。
"""
import hashlib
import logging
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .models import (
    CHANNELS,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)
from .rendering import render_inapp

logger = logging.getLogger(__name__)

MAX_RETRIES = 5          # 最大重试次数，超过进入死信（failed）
BACKOFF_BASE_SECONDS = 60  # 指数退避基数：60s × 2^n → 1/2/4/8/16 分钟


def _hash(*parts):
    """生成 64 位 sha256 幂等/聚合键。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_prefs(user):
    """获取（或初始化）用户通知偏好。"""
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def _my_notifications(user):
    """通知唯一查询起点：仅本人。"""
    return Notification.objects.filter(recipient=user)


def _get_my_notification(request, pk):
    """行级校验：非本人通知一律 404（不暴露存在性）。"""
    from django.shortcuts import get_object_or_404

    return get_object_or_404(Notification, pk=pk, recipient=request.user)


def _create_notification(template, recipient, *, context, link, priority,
                         expires_at, scheduled_at, created_by, dedup_key=None,
                         agg_key=""):
    rendered = render_inapp(template, context)
    return Notification.objects.create(
        recipient=recipient,
        template=template,
        created_by=created_by,
        title=rendered["title"],
        content=rendered["content"],
        category=template.category,
        dedup_key=dedup_key,
        agg_key=agg_key,
        link=link or "",
        priority=priority,
        context_json=context or {},
        scheduled_at=scheduled_at or timezone.now(),
        expires_at=expires_at,
    )


def emit(template_code, *, recipient, context=None, dedup_context=None,
         agg_context=None, link="", priority="normal", expires_at=None,
         scheduled_at=None, created_by=None):
    """业务事件触发通知的唯一入口。

    - template_code: NotificationTemplate.code，模板停用/不存在则拒发并留痕；
    - dedup_context: 幂等业务键（如 f"record:{pk}"），同键重复触发不重复落库；
    - agg_context:   聚合业务键，用户偏好窗口内的同键触发合并计数（agg_count+1）；
    - 返回 Notification 或 None（拒发/去重命中）。
    """
    template = NotificationTemplate.objects.filter(code=template_code, is_active=True).first()
    if template is None:
        logger.warning("通知拒发：模板 %s 不存在或已停用", template_code)
        return None

    # 1) 幂等：唯一约束兜底，并发重复触发只建一条
    dedup_key = None
    if dedup_context is not None:
        dedup_key = _hash(template_code, recipient.pk, dedup_context)
        try:
            with transaction.atomic():
                return _create_notification(
                    template, recipient, context=context, link=link,
                    priority=priority, expires_at=expires_at,
                    scheduled_at=scheduled_at, created_by=created_by,
                    dedup_key=dedup_key,
                )
        except IntegrityError:
            return Notification.objects.filter(dedup_key=dedup_key).first()

    # 2) 聚合降噪：偏好窗口内同键待读通知合并计数，不刷屏
    agg_key = ""
    if agg_context is not None:
        prefs = get_prefs(recipient)
        agg_key = _hash(template_code, recipient.pk, agg_context)
        if prefs.agg_window_minutes > 0:
            window_start = timezone.now() - timezone.timedelta(minutes=prefs.agg_window_minutes)
            dup = Notification.objects.filter(
                recipient=recipient,
                agg_key=agg_key,
                status=Notification.Status.PENDING,
                is_read=False,
                created_at__gte=window_start,
            ).first()
            if dup is not None:
                Notification.objects.filter(pk=dup.pk).update(
                    agg_count=models.F("agg_count") + 1, updated_at=timezone.now(),
                )
                return Notification.objects.get(pk=dup.pk)

    # 3) 落库为 pending，交 notify_worker 发送
    return _create_notification(
        template, recipient, context=context, link=link, priority=priority,
        expires_at=expires_at, scheduled_at=scheduled_at, created_by=created_by,
        agg_key=agg_key,
    )


def has_emitted(template_code, recipient, dedup_context):
    """按业务幂等键判断是否已触发过（供定时任务统计真实新建数）。"""
    key = _hash(template_code, recipient.pk, dedup_context)
    return Notification.objects.filter(dedup_key=key).exists()


def announce(title, content, *, created_by, link="", priority="normal",
             expires_days=None, recipients=None):
    """管理员系统公告批量推送（模板 system_announcement，无幂等/聚合）。

    recipients 缺省 = 全部启用用户；同一批副本共享 batch_id（撤回整批生效）。
    站内渠道即时送达：无需邮件的副本直接置 sent 并写渠道日志；
    需要邮件的副本保持 pending，由 notify_worker 发送邮件。
    返回创建条数。
    """
    template = NotificationTemplate.objects.filter(code="system_announcement").first()
    if template is None:
        logger.error("系统公告模板缺失，公告未推送")
        return 0
    User = get_user_model()
    if recipients is None:
        recipients = User.objects.filter(is_active=True)
    expires_at = (
        timezone.now() + timezone.timedelta(days=expires_days) if expires_days else None
    )
    batch_id = uuid.uuid4().hex
    now = timezone.now()
    # 偏好一次性预取（审查 H3）：原实现循环内逐用户 get_or_create，
    # 50 人公告 = 100+ 次 SQLite 查询/写入，既拖慢请求又与调度器争抢单写者锁。
    # 缺省（无偏好记录）视为开启，与 NotificationPreference.email_enabled 语义一致。
    recipient_list = list(recipients)
    pref_map = {
        p.user_id: p
        for p in NotificationPreference.objects.filter(user_id__in=[u.pk for u in recipient_list])
    }
    email_users, inapp_users = [], []
    for user in recipient_list:
        pref = pref_map.get(user.pk)
        email_ok = pref.email_enabled(template.category) if pref is not None else True
        if template.supports_email and getattr(user, "email", "") and email_ok:
            email_users.append(user)
        else:
            inapp_users.append(user)

    def _row(user, status):
        return Notification(
            recipient=user,
            template=template,
            created_by=created_by,
            title=title[:200],
            content=content,
            category=template.category,
            priority=priority,
            link=link or "",
            context_json={"title": title, "content": content},
            expires_at=expires_at,
            batch_id=batch_id,
            status=status,
            sent_at=now if status == Notification.Status.SENT else None,
        )

    rows = [_row(u, Notification.Status.SENT) for u in inapp_users]
    rows += [_row(u, Notification.Status.PENDING) for u in email_users]
    created = Notification.objects.bulk_create(rows)
    NotificationDelivery.objects.bulk_create([
        NotificationDelivery(
            notification=n,
            channel="inapp",
            status=NotificationDelivery.ChannelStatus.SUCCESS,
            attempts=1,
        )
        for n in created if n.status == Notification.Status.SENT
    ])
    return len(rows)


# ============ 状态机（视图/命令只允许经此操作） ============

def claim_for_send(pk):
    """worker 原子领取：pending → sending；抢不到（已被其他 worker 领取）返回 False。"""
    updated = Notification.objects.filter(
        pk=pk, status=Notification.Status.PENDING,
    ).update(status=Notification.Status.SENDING, updated_at=timezone.now())
    return bool(updated)


def mark_sent(notification):
    """sending → sent（全部启用渠道成功）。"""
    Notification.objects.filter(
        pk=notification.pk, status=Notification.Status.SENDING,
    ).update(
        status=Notification.Status.SENT,
        sent_at=timezone.now(),
        next_retry_at=None,
        last_error="",
        updated_at=timezone.now(),
    )


def mark_retry(notification, error_detail):
    """可重试失败：指数退避回 pending；达到上限转死信 failed。"""
    next_count = notification.retry_count + 1
    if next_count >= MAX_RETRIES:
        mark_failed(notification, f"{error_detail}（已达最大重试 {MAX_RETRIES} 次）")
        return False
    delay = BACKOFF_BASE_SECONDS * (2 ** notification.retry_count)
    Notification.objects.filter(pk=notification.pk).update(
        status=Notification.Status.PENDING,
        retry_count=models.F("retry_count") + 1,
        next_retry_at=timezone.now() + timezone.timedelta(seconds=delay),
        last_error=error_detail[:2000],
        updated_at=timezone.now(),
    )
    return True


def mark_failed(notification, error_detail):
    """死信：重试耗尽或不可重试错误。notify_requeue 可手工重投。"""
    Notification.objects.filter(pk=notification.pk).update(
        status=Notification.Status.FAILED,
        last_error=error_detail[:2000],
        next_retry_at=None,
        updated_at=timezone.now(),
    )


def revoke(notification, by_user):
    """撤回：system.update 权限、或触发人本人可操作；
    仅 pending、或已发送未读可撤。公告按批次撤回全部未读副本。
    返回撤回条数（0 = 无权限或状态不可撤）。"""
    from accounts.services import has_perm

    if not (
        by_user.is_superuser
        or notification.created_by_id == by_user.pk
        or has_perm(by_user, "system", "update")
    ):
        return 0
    if not notification.can_revoke:
        return 0
    now = timezone.now()
    if notification.batch_id:
        # 公告：同批次全部副本一并撤回（含已读——撤回即作废，接收者不再可见）
        return Notification.objects.filter(
            batch_id=notification.batch_id,
            status__in=[Notification.Status.PENDING, Notification.Status.SENT],
        ).update(status=Notification.Status.REVOKED, updated_at=now)
    notification.status = Notification.Status.REVOKED
    notification.save(update_fields=["status", "updated_at"])
    return 1


def expire_overdue(now=None):
    """兜底过期扫描：pending 且已过 expires_at → expired。返回处理条数。"""
    now = now or timezone.now()
    return Notification.objects.filter(
        status=Notification.Status.PENDING, expires_at__lt=now,
    ).update(status=Notification.Status.EXPIRED, updated_at=now)


def expire_if_overdue(pk, now=None):
    """worker 领取前单条过期拦截：已过期则置 expired。返回是否命中过期。"""
    now = now or timezone.now()
    return bool(Notification.objects.filter(
        pk=pk,
        status=Notification.Status.PENDING,
        expires_at__lt=now,
    ).update(status=Notification.Status.EXPIRED, updated_at=now))


def mark_read(notification, now=None):
    """接收侧：标记已读（幂等）。"""
    if notification.is_read:
        return
    notification.is_read = True
    notification.read_at = now or timezone.now()
    notification.save(update_fields=["is_read", "read_at"])


def mark_all_read(user):
    """全部已读：仅本人未读通知。"""
    return _my_notifications(user).filter(is_read=False).update(
        is_read=True, read_at=timezone.now(),
    )


def unread_count(user):
    """未读数（不含撤回/过期）。"""
    return _my_notifications(user).filter(
        is_read=False,
        status__in=Notification.RECIPIENT_VISIBLE_STATUS,
    ).count()


def requeue(notification):
    """死信重投：重置重试状态回 pending。"""
    Notification.objects.filter(pk=notification.pk).update(
        status=Notification.Status.PENDING,
        retry_count=0,
        next_retry_at=None,
        updated_at=timezone.now(),
    )


def users_with_perm(module, action=None):
    """拥有某权限码的启用用户（超管恒含）；用于无归属字段模块的通知定位。

    与 accounts.services.has_perm 同语义：超管全集、角色并集。
    """
    from accounts.registry import permission_code

    User = get_user_model()
    qs = User.objects.filter(is_active=True)
    if action is not None:
        code = permission_code(module, action)
        qs = qs.filter(
            models.Q(is_superuser=True)
            | models.Q(
                rbac_roles__is_active=True,
                rbac_roles__permissions__code=code,
            )
        ).distinct()
    return qs


def template_or_none(code):
    try:
        return NotificationTemplate.objects.get(code=code)
    except ObjectDoesNotExist:
        return None


CHANNEL_LABELS = dict(CHANNELS)
