"""发送层：渠道适配注册表 + 用户偏好过滤 + 逐渠道派发。

- BaseChannel.send(notification) -> SendResult，新增渠道只需子类化并注册 CHANNEL_REGISTRY；
- allowed_channels()：发送前的渠道过滤（用户偏好 / 免打扰 / 模板能力）；
- dispatch()：逐渠道发送并落 NotificationDelivery 日志（一通知 × 一渠道一行）。
"""
import logging
import smtplib
import socket
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail

from .models import NotificationDelivery
from .rendering import render_email
from .services import get_prefs

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    channel: str
    status: str            # NotificationDelivery.ChannelStatus
    detail: str = ""
    permanent: bool = False  # True = 不可重试错误（如收件人无邮箱）
    ok: bool = False


class BaseChannel:
    code = ""
    name = ""

    def send(self, notification):
        raise NotImplementedError

    def _result(self, status, detail="", permanent=False, ok=False):
        return SendResult(self.code, status, detail=detail, permanent=permanent, ok=ok)


class InAppChannel(BaseChannel):
    """站内通知：通知行本身即消息载体，落库即送达。"""

    code = "inapp"
    name = "站内通知"

    def send(self, notification):
        # 通知主表已持久化，站内渠道恒成功
        return self._result(NotificationDelivery.ChannelStatus.SUCCESS,
                            "站内消息已写入收件箱", ok=True)


class EmailChannel(BaseChannel):
    """邮件渠道：Django email backend（settings 中配置 SMTP）。"""

    code = "email"
    name = "邮件"

    def send(self, notification):
        recipient = notification.recipient
        if not getattr(recipient, "email", ""):
            return self._result(
                NotificationDelivery.ChannelStatus.FAILED,
                "收件人未配置邮箱地址", permanent=True,
            )
        rendered = render_email(notification.template, notification.context_json)
        if rendered is None:
            return self._result(NotificationDelivery.ChannelStatus.SKIPPED,
                                "模板未配置邮件内容")
        try:
            send_mail(
                subject=rendered["subject"] or notification.title,
                message=rendered["body"] or notification.content,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@localhost",
                recipient_list=[recipient.email],
                fail_silently=False,
            )
        except (smtplib.SMTPException, socket.error, ConnectionError, TimeoutError) as exc:
            # 网络/SMTP 故障：可重试
            return self._result(NotificationDelivery.ChannelStatus.FAILED,
                                f"SMTP 发送失败: {exc}")
        except Exception as exc:  # 配置类错误：不可重试
            return self._result(NotificationDelivery.ChannelStatus.FAILED,
                                f"邮件发送异常: {exc}", permanent=True)
        return self._result(NotificationDelivery.ChannelStatus.SUCCESS,
                            f"已发送至 {recipient.email}", ok=True)


# 渠道注册表：新增渠道在此登记即可被 worker 调度
CHANNEL_REGISTRY = {
    InAppChannel.code: InAppChannel(),
    EmailChannel.code: EmailChannel(),
}


def allowed_channels(notification):
    """发送前渠道过滤：偏好 / 免打扰 / 模板能力。

    - 站内通知为系统基础能力，恒发送；
    - 邮件按用户偏好（类别开关）与模板能力过滤；免打扰时段非高优先级邮件跳过。
    """
    prefs = get_prefs(notification.recipient)
    allowed = ["inapp"]
    has_email = bool(getattr(notification.recipient, "email", ""))
    if (
        has_email
        and template_supports_email(notification)
        and prefs.email_enabled(notification.category)
        and (notification.priority == "high" or not prefs.in_quiet_hours())
    ):
        allowed.append("email")
    return allowed


def template_supports_email(notification):
    return notification.template.supports_email


def dispatch(notification):
    """逐渠道发送并回写渠道日志。返回 [SendResult, ...]。"""
    results = []
    for code in allowed_channels(notification):
        channel = CHANNEL_REGISTRY.get(code)
        if channel is None:
            continue
        try:
            result = channel.send(notification)
        except Exception as exc:  # 渠道异常不阻断其他渠道
            logger.exception("渠道 %s 发送异常 notification#%s", code, notification.pk)
            result = SendResult(code, NotificationDelivery.ChannelStatus.FAILED,
                                detail=f"渠道内部异常: {exc}")
        NotificationDelivery.objects.update_or_create(
            notification=notification, channel=code,
            defaults={
                "status": result.status,
                "detail": result.detail[:2000],
                "attempts": notification.retry_count + 1,
            },
        )
        results.append(result)
    return results
