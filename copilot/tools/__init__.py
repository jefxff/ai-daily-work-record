"""工具包：import 即完成注册（由 CopilotConfig.ready 触发）。"""

from . import attachments, kline, meta, records, search  # noqa: F401

__all__ = ['attachments', 'kline', 'meta', 'records', 'search']
