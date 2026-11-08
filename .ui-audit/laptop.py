"""聚焦取证：1180px「死区」横向溢出 + 小屏关键页截图。"""
import json
import os

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9000"
ROOT = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(ROOT, "shots")
os.makedirs(SHOTS, exist_ok=True)

TARGETS = [
    ("daily_record_list", "/daily_record/"),
    ("complaint_list", "/complaint/"),
    ("decoration_list", "/decoration/"),
]

JS = r"""
() => {
  const de = document.documentElement;
  const vw = de.clientWidth;
  const res = {
    vw, docScrollW: de.scrollWidth,
    hScrollbar: de.scrollWidth > vw,
    // 页面级横向滚动条是否存在（scrollWidth>clientWidth 且 body 不隐藏溢出）
    bodyOverflowX: getComputedStyle(document.body).overflowX,
    htmlOverflowX: getComputedStyle(de).overflowX,
    clippedByAncestor: null,
    hiddenRight: [],
  };
  // 找出被祖先裁掉的元素：自身右边界超出最近的 overflow 裁剪祖先
  for (const el of document.querySelectorAll('table, table tr, table td, table th')) {
    const r = el.getBoundingClientRect();
    if (r.right <= vw + 2) continue;
    let p = el.parentElement, clip = null;
    while (p) {
      const cs = getComputedStyle(p);
      if (['hidden','clip','auto','scroll'].includes(cs.overflowX)) { clip = p; break; }
      p = p.parentElement;
    }
    res.hiddenRight.push({
      sel: el.tagName.toLowerCase() + (el.className && typeof el.className==='string' ? '.'+el.className.trim().split(/\s+/).slice(0,2).join('.') : ''),
      right: Math.round(r.right), w: Math.round(r.width),
      clippedBy: clip ? (clip.tagName.toLowerCase() + '.' + (typeof clip.className==='string'? clip.className.trim().split(/\s+/)[0]:'')) : null,
    });
    if (res.hiddenRight.length > 6) break;
  }
  return res;
}
"""


def main():
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1180, "height": 900})
        pg = ctx.new_page()
        pg.goto(f"{BASE}/accounts/login/", wait_until="domcontentloaded")
        pg.fill("input[name=username]", "admin")
        pg.fill("input[name=password]", os.environ["UI_AUDIT_PASSWORD"])
        pg.click("button[type=submit], input[type=submit]")
        pg.wait_for_load_state("networkidle")

        for name, path in TARGETS:
            pg.set_viewport_size({"width": 1180, "height": 900})
            pg.goto(BASE + path, wait_until="networkidle")
            pg.wait_for_timeout(600)
            m = pg.evaluate(JS)
            out[name] = m
            pg.screenshot(path=os.path.join(SHOTS, f"{name}__laptop1180.png"), full_page=False)
            print(f"--- {name} @1180 ---")
            print(json.dumps(m, ensure_ascii=False, indent=2)[:1400])
        b.close()
    with open(os.path.join(ROOT, "laptop_report.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
