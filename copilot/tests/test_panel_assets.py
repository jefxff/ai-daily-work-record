"""面板静态资源的一致性测试。

踩过的坑：``copilot/panel.js`` 改成"用服务端渲染好的 ``answer_html`` 显示正文"
之后，只改了 ``static/`` 下的源文件、没跑 ``collectstatic``，``STATIC_ROOT`` 里
留着的还是旧版——页面继续加载旧 JS，AI 回答就把 Markdown 源码（``**加粗**``、
``| 表格 |``）当纯文本显示，看起来像后端没生效，其实后端一直是对的。

所以这里锁两件事：

1. ``/static/`` 下取到的 ``panel.js`` / ``panel.css`` 必须是带 Markdown 渲染的版本；
2. ``STATIC_ROOT`` 落后于源文件时，取源文件（而不是继续返回旧产物）。
"""

import os
import tempfile
import time
from pathlib import Path
from unittest import mock, skipUnless

from django.test import SimpleTestCase

from work_record import urls as urlconf

# serve_static 只在 DEBUG=0 时定义（DEBUG=1 由 runserver 自己服务 /static/）
HAS_STATIC_ROUTE = hasattr(urlconf, 'serve_static')


def _body(resp):
    if getattr(resp, 'streaming', False):
        return b''.join(resp.streaming_content).decode('utf-8')
    return resp.content.decode('utf-8')


@skipUnless(HAS_STATIC_ROUTE, 'DEBUG=1 时 /static/ 由 runserver 提供，不测本项目路由')
class PanelAssetTests(SimpleTestCase):

    def test_panel_js_renders_answer_html(self):
        """前端必须走 answer_html（服务端 markdown），不能退回纯文本。"""
        resp = self.client.get('/static/copilot/panel.js')
        self.assertEqual(resp.status_code, 200)
        body = _body(resp)
        self.assertIn('answer_html', body)
        self.assertIn('content_html', body)

    def test_panel_js_remembers_position(self):
        """面板位置拖过就记住：大屏用户会把面板拖到视线附近，刷新不能弹回右下角。"""
        body = _body(self.client.get('/static/copilot/panel.js'))
        self.assertIn('copilot:panel-pos', body)
        self.assertIn('is-floating', body)

    def test_panel_js_remembers_size(self):
        """拖过右下角就该记住：400×560 装不下一张三列表，用户会一直拖着调。"""
        body = _body(self.client.get('/static/copilot/panel.js'))
        self.assertIn('--copilot-w', body)
        self.assertIn('--copilot-h', body)
        self.assertIn('setupResize', body)

    def test_panel_css_has_markdown_styles(self):
        """回答区靠 .copilot-md 撑排版；样式缺了会变成一坨没间距的文本。"""
        resp = self.client.get('/static/copilot/panel.css')
        self.assertEqual(resp.status_code, 200)
        body = _body(resp)
        self.assertIn('.copilot-md', body)
        # 拖动后改走 left/top 定位，right/bottom 必须清掉，否则两套定位打架
        self.assertIn('.copilot-panel.is-floating', body)

    def test_panel_template_has_drag_and_pin(self):
        """JS 按 id 取钉住按钮、按 class 取标题栏把手；模板删了会静默失效。"""
        tpl = (Path(__file__).resolve().parents[2]
               / 'copilot' / 'templates' / 'copilot' / '_panel.html')
        text = tpl.read_text(encoding='utf-8')
        self.assertIn('id="copilotPin"', text)
        self.assertIn('copilot-header', text)
        # 缩放把手：JS 按 id 取，模板删了就静默不能缩放
        self.assertIn('id="copilotResize"', text)

    def test_newer_source_wins_over_stale_static_root(self):
        """STATIC_ROOT 里是旧产物时，按 mtime 取新的源文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            stale_dir = Path(tmp, 'copilot')
            stale_dir.mkdir()
            stale = stale_dir / 'panel.js'
            stale.write_text('/* stale build, no answer_html */', encoding='utf-8')
            yesterday = time.time() - 86400
            os.utime(stale, (yesterday, yesterday))

            with mock.patch.object(urlconf, '_static_root', Path(tmp)):
                resp = self.client.get('/static/copilot/panel.js')
                body = _body(resp)

        self.assertEqual(resp.status_code, 200)
        self.assertIn('answer_html', body)
        self.assertNotIn('stale build', body)
