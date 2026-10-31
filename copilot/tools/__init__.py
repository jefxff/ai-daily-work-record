"""工具包：import 即完成注册（由 CopilotConfig.ready 触发）。"""

from . import attachments, meta, records, search  # noqa: F401

__all__ = ['attachments', 'meta', 'records', 'search']
