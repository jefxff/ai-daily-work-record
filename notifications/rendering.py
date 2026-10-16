"""通知模板渲染：统一走 Django Template 引擎。

- 变量占位符：{{变量}}，支持过滤链（如 {{date|date:"Y-m-d"}}）；
- autoescape 默认开启，防模板变量注入 XSS；
- 渲染失败不抛异常（模板语法错误时变量占位符原样保留），由 logger 留痕。
"""
import logging

from django.template import Context, Template

logger = logging.getLogger(__name__)


def render_text(template_str, context):
    """渲染单段模板文本；模板为空/出错时安全降级。"""
    if not template_str:
        return ""
    try:
        return Template(template_str).render(Context(context or {}))
    except Exception as exc:  # 模板语法错误：原样返回，记录日志，不阻断发送流程
        logger.warning("通知模板渲染失败: %s (%s)", template_str[:100], exc)
        return template_str


def render_inapp(template_obj, context):
    """渲染站内渠道标题与正文。"""
    return {
        "title": render_text(template_obj.title_template, context)[:200],
        "content": render_text(template_obj.content_template, context),
    }


def render_email(template_obj, context):
    """渲染邮件渠道主题与正文（模板未配置邮件内容时返回 None）。"""
    if not template_obj.supports_email:
        return None
    return {
        "subject": render_text(template_obj.email_subject_template, context)[:200],
        "body": render_text(template_obj.email_body_template, context),
    }
