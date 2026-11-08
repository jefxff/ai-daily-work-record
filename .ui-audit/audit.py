"""
UI 审计取证脚本（只读，不改动项目代码）

用途：以真实浏览器渲染 http://127.0.0.1:9000，采集
  1) 指定断点下的整页截图
  2) 可量化的 DOM 事实（横向溢出、越界元素、点击热区、字号等）
输出：.ui-audit/shots/*.png 与 .ui-audit/report.json
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9000"
ROOT = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(ROOT, "shots")
os.makedirs(SHOTS, exist_ok=True)

USER = "admin"
PWD = os.environ["UI_AUDIT_PASSWORD"]

WIDTHS = {
    "desktop": 1440,
    "laptop": 1180,
    "tablet": 768,
    "mobile": 390,
    "mobile_sm": 320,
}

PAGES = [
    ("home", "/"),
    ("workspace", "/workspace/"),
    ("dashboard", "/dashboard/"),
    ("daily_record_list", "/daily_record/"),
    ("daily_record_form", "/daily_record/add/"),
    ("daily_record_detail", "/daily_record/detail/127/"),
    ("complaint_list", "/complaint/"),
    ("complaint_form", "/complaint/add/"),
    ("complaint_detail", "/complaint/detail/120/"),
    ("price_eval_list", "/price_evaluation/"),
    ("customer_credit_list", "/customer_credit/"),
    ("decoration_list", "/decoration/"),
    ("knowledge", "/knowledge/"),
    ("data_download", "/data_download/"),
]

MEASURE_JS = r"""
() => {
  const vw = document.documentElement.clientWidth;
  const out = {
    url: location.pathname,
    vw,
    docScrollW: document.documentElement.scrollWidth,
    bodyScrollW: document.body.scrollWidth,
    hasHScroll: document.documentElement.scrollWidth > vw + 1,
    overflowX: document.documentElement.scrollWidth - vw,
    tinyText: [],
    smallTargets: [],
    overflowing: [],
    fixedWide: [],
    tiny: [],
  };
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    const sel = tag + id + cls;

    // 文本过小
    if (el.children.length === 0 && el.textContent.trim().length > 1) {
      const fs = parseFloat(cs.fontSize);
      if (fs && fs < 12) {
        out.tinyText.push({ sel, fs: +fs.toFixed(1), text: el.textContent.trim().slice(0, 24) });
      }
    }
    // 可点击热区过小
    const interactive = el.matches('a,button,input[type=button],input[type=submit],select,[role=button],[onclick]');
    if (interactive && (r.width < 32 || r.height < 32)) {
      const k = sel + '|' + Math.round(r.width) + 'x' + Math.round(r.height);
      if (!seen.has(k)) { seen.add(k);
        out.smallTargets.push({ sel, w: Math.round(r.width), h: Math.round(r.height),
                                text: (el.textContent || el.value || '').trim().slice(0, 24) }); }
    }
    // 横向越界
    if (r.right > vw + 2 && r.width > 40) {
      const k = 'ov|' + sel;
      if (!seen.has(k)) { seen.add(k);
        out.overflowing.push({ sel, right: Math.round(r.right), w: Math.round(r.width) }); }
    }
    // 固定定位且超宽
    if ((cs.position === 'fixed' || cs.position === 'sticky') && r.width > vw + 2) {
      out.fixedWide.push({ sel, pos: cs.position, w: Math.round(r.width) });
    }
  }
  const cap = (a, n) => a.slice(0, n);
  out.tinyText = cap(out.tinyText, 12);
  out.smallTargets = cap(out.smallTargets, 25);
  out.overflowing = cap(out.overflowing, 20);
  out.fixedWide = cap(out.fixedWide, 10);
  return out;
}
"""

# 视口内可见的“首屏”测量：侧栏/顶栏/表格容器
LAYOUT_JS = r"""
() => {
  const pick = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return { sel: s, x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height),
             pos: cs.position, display: cs.display, overflowX: cs.overflowX,
             transform: cs.transform === 'none' ? '' : cs.transform.slice(0, 40) }; };
  const table = document.querySelector('table');
  let t = null;
  if (table) { const r = table.getBoundingClientRect();
    const wrap = table.closest('.table-responsive, .u-table-wrap, div');
    t = { tableW: Math.round(r.width), scrollW: table.scrollWidth,
          clientW: table.clientWidth, minWidth: getComputedStyle(table).minWidth,
          parentOverflow: wrap ? getComputedStyle(wrap).overflowX : null }; }
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    sidebar: pick('aside, .sidebar, .app-sidebar, nav.sidebar, #sidebar'),
    topnav: pick('header, .topnav, .app-topbar, nav.navbar, .navbar'),
    main: pick('main, main.container, .app-main'),
    table: t,
  };
}
"""


def main():
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=1)
        page = ctx.new_page()
        # 登录
        page.goto(f"{BASE}/accounts/login/", wait_until="domcontentloaded")
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PWD)
        page.click("button[type=submit], input[type=submit]")
        page.wait_for_load_state("networkidle")
        print("LOGIN ->", page.url)

        for key, path in PAGES:
            if only and key not in only:
                continue
            report[key] = {}
            for wname, w in WIDTHS.items():
                page.set_viewport_size({"width": w, "height": 900})
                try:
                    page.goto(BASE + path, wait_until="networkidle", timeout=45000)
                except Exception as e:
                    report[key][wname] = {"error": str(e)[:120]}
                    continue
                page.wait_for_timeout(700)
                try:
                    m = page.evaluate(MEASURE_JS)
                except Exception as e:
                    m = {"error": str(e)[:120]}
                try:
                    l = page.evaluate(LAYOUT_JS)
                except Exception:
                    l = {}
                m["layout"] = l
                report[key][wname] = m
                if wname in ("desktop", "tablet", "mobile"):
                    page.screenshot(path=os.path.join(SHOTS, f"{key}__{wname}.png"),
                                    full_page=(wname != "mobile"))
                    # 首屏视口截图（小屏只看首屏更真实）
                    if wname == "mobile":
                        page.screenshot(path=os.path.join(SHOTS, f"{key}__mobile_fold.png"),
                                        full_page=False)
                print(f"  {key:22s} {wname:10s} vw={m.get('vw')} hscroll={m.get('hasHScroll')}"
                      f" ovf={m.get('overflowX')} tinyText={len(m.get('tinyText', []))}"
                      f" smallTap={len(m.get('smallTargets', []))}")
        browser.close()

    with open(os.path.join(ROOT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("REPORT ->", os.path.join(ROOT, "report.json"))


if __name__ == "__main__":
    main()
