"""验证小屏卡片模式的两处问题，并输出改进后的对比图。"""
import json
import os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
AB = os.path.join(ROOT, "ab")
os.makedirs(AB, exist_ok=True)

# 改进后的小屏卡片样式
FIX_MOBILE = """
@media (max-width: 640px) {
  /* 1) 卡片序号：退化为右上角角标，不再占一整行字段
        （注意：行首是隐藏的「选择」勾选格，序号需按 data-label 精确命中） */
  .table--cards tr { position: relative; padding-top: 16px; }
  .table--cards td[data-label="序号"] {
    position: absolute !important; top: 10px; right: 12px;
    width: auto !important; background: none !important;
    padding: 0 !important; text-align: right !important;
    font-size: 12px; font-weight: 600; color: var(--c-text-secondary);
    border-radius: 0;
  }
  .table--cards td[data-label="序号"]::before { display: none !important; }

  /* 2) 长文本字段：标签独占一行，值整行展开，徽标不再压字 */
  .table--cards td[data-label="工作内容"],
  .table--cards td[data-label="详细进度"],
  .table--cards td[data-label="受理法院 / 律师"] { display: block !important; }
  .table--cards td[data-label="工作内容"]::before,
  .table--cards td[data-label="详细进度"]::before,
  .table--cards td[data-label="受理法院 / 律师"]::before {
    display: block !important; width: 100%; text-align: left;
    margin-bottom: 3px; font-size: 12px; font-weight: 600;
  }
  .table--cards td[data-label="工作内容"] > *,
  .table--cards td[data-label="详细进度"] > *,
  .table--cards td[data-label="受理法院 / 律师"] > * { display: block; width: 100%; }

  /* 3) 徽标 / 标签字号下限 12px */
  .table--cards :is(.badge, .status-badge, .kb-chip) { font-size: 12px !important; }

  /* 4) 操作行：带文字标签的 44px 触控按钮 */
  .table--cards td[data-label="操作"] {
    display: flex !important; flex-wrap: wrap; gap: 8px;
    justify-content: flex-start; padding-top: 14px !important;
  }
  .table--cards td[data-label="操作"]::before { display: none !important; }
  .table--cards td[data-label="操作"] :is(a, button) {
    min-height: 44px; padding: 0 14px; gap: 6px;
    display: inline-flex; align-items: center; white-space: nowrap;
    font-size: 13px; border-radius: var(--r-sm);
    border: 1px solid var(--c-border); background: var(--c-surface);
    color: var(--c-text);
  }
  .table--cards td[data-label="操作"] :is(a, button) svg { flex: 0 0 auto; }
  .table--cards td[data-label="操作"] :is(a, button) .act-lbl {
    display: inline !important; white-space: nowrap; font-size: 13px;
  }
}
"""

JS_ACT = """() => {
  const map = {detail: '查看', edit: '编辑', delete: '删除', 'daily_record_detail': '查看'};
  document.querySelectorAll('.table--cards td[data-label="操作"] a, .table--cards td[data-label="操作"] button')
    .forEach(el => {
      const href = (el.getAttribute('href') || '') + (el.getAttribute('onclick') || '');
      const key = Object.keys(map).find(k => href.includes(k));
      if (!key) return;
      if (el.querySelector('.act-lbl')) return;
      const s = document.createElement('span');
      s.className = 'act-lbl';
      s.textContent = map[key];
      el.appendChild(s);
    });
}"""

CHECK = """() => {
  const cells = [...document.querySelectorAll('.table--cards tbody td')].slice(0, 3);
  const optional = document.querySelector('.table--cards tbody td.col-optional');
  const th = document.querySelector('.table--cards tbody td[data-label="序号"]');
  const hint = document.querySelector('.hidden-cols-hint');
  const acts = [...document.querySelectorAll('.table--cards td[data-label="操作"] a')].map(a => {
    const r = a.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  });
  return {
    序号_display: th ? getComputedStyle(th).display : null,
    序号_class: th ? th.className : null,
    colOptional_display: optional ? getComputedStyle(optional).display : null,
    hint_display: hint ? getComputedStyle(hint).display : null,
    actionButtons: acts,
  };
}"""

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:9000/accounts/login/", wait_until="domcontentloaded")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", os.environ["UI_AUDIT_PASSWORD"])
    pg.click("button[type=submit], input[type=submit]")
    pg.wait_for_load_state("networkidle")
    pg.goto("http://127.0.0.1:9000/daily_record/", wait_until="networkidle")
    pg.wait_for_timeout(500)

    print("=== 现状（390px 卡片模式）===")
    print(json.dumps(pg.evaluate(CHECK), ensure_ascii=False, indent=1))
    pg.screenshot(path=os.path.join(AB, "P3_390_before.png"), full_page=False)

    pg.evaluate(JS_ACT)
    pg.add_style_tag(content=FIX_MOBILE)
    pg.wait_for_timeout(500)
    print("=== 优化后 ===")
    print(json.dumps(pg.evaluate(CHECK), ensure_ascii=False, indent=1))
    pg.screenshot(path=os.path.join(AB, "P3_390_after.png"), full_page=False)

    # 抽屉遮罩对比
    pg.goto("http://127.0.0.1:9000/daily_record/", wait_until="networkidle")
    pg.wait_for_timeout(400)
    pg.click("#mobileMenuToggle")
    pg.wait_for_timeout(700)
    pg.screenshot(path=os.path.join(AB, "P4_390_before_drawer.png"), full_page=False)
    pg.add_style_tag(content="body.sidebar-open .sidebar-scrim { display:block !important; }")
    pg.wait_for_timeout(400)
    pg.screenshot(path=os.path.join(AB, "P4_390_after_drawer.png"), full_page=False)
    print("drawer shots saved")
    b.close()
