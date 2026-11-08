"""测量修复前后 诉讼列表 各列的实际渲染宽度与截断情况。"""
import json
import os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(ROOT, "ab.py"), encoding="utf-8").read()


def grab(name):
    a = src.index(f'{name} = """') + len(f'{name} = """')
    z = src.index('"""', a)
    return src[a:z]


JS = """() => {
  const ths = [...document.querySelectorAll('thead th')].map(t => ({
    label: t.textContent.trim().slice(0, 6),
    w: Math.round(t.getBoundingClientRect().width),
    disp: getComputedStyle(t).display }));
  const td = document.querySelector('tbody td[data-label="纠纷类型"]');
  const cs = getComputedStyle(td);
  return {
    docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ths, disputeW: Math.round(td.getBoundingClientRect().width),
    disputeText: td.textContent.trim(), pad: cs.paddingLeft + '/' + cs.paddingRight,
    fontSize: cs.fontSize, overflow: cs.overflow, textOverflow: cs.textOverflow };
}"""

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1180, "height": 900})
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:9000/accounts/login/", wait_until="domcontentloaded")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", os.environ["UI_AUDIT_PASSWORD"])
    pg.click("button[type=submit], input[type=submit]")
    pg.wait_for_load_state("networkidle")
    pg.goto("http://127.0.0.1:9000/complaint/", wait_until="networkidle")
    pg.wait_for_timeout(400)
    print("=== BEFORE ===")
    print(json.dumps(pg.evaluate(JS), ensure_ascii=False, indent=1))
    pg.add_style_tag(content=grab("FIX_TABLE_FIT") + grab("FIX_COLUMN_BUDGET"))
    pg.wait_for_timeout(500)
    print("=== AFTER ===")
    print(json.dumps(pg.evaluate(JS), ensure_ascii=False, indent=1))
    b.close()
