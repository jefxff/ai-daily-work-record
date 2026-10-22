"""
WSGI config for work_record project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'work_record.settings')

try:  # 隐藏 wsgiref 的 Server 头版本信息（审计 P3-6）
    from work_record.server_patch import harden_server_banner
    harden_server_banner()
except Exception:
    pass

application = get_wsgi_application()
