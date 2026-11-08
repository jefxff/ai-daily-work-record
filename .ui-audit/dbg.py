import json
import os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(ROOT, "mobile.py"), encoding="utf-8").read()
a = src.index('FIX_MOBILE = """') + len('FIX_MOBILE = """')
z = src.index('"""', a)
FIX = src[a:z]

CHECK = """() => {
  const td = document.querySelector('.table--cards tbody td:first-child');
  const cs = getComputedStyle(td);
  const r = td.getBoundingClientRect();
  const tr = td.parentElement;
  const trcs = getComputedStyle(tr);
  return {
    td_position: cs.position, td_top: cs.top, td_right: cs.right,
    td_width: cs.width, td_display: cs.display,
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    tr_position: trcs.position,
    tr_class: tr.className,
    firstChildLabel: td.getAttribute('data-label'),
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
    pg.wait_for_timeout(400)
    print("BEFORE:", json.dumps(pg.evaluate(CHECK), ensure_ascii=False))
    pg.add_style_tag(content=FIX)
    pg.wait_for_timeout(400)
    print("AFTER :", json.dumps(pg.evaluate(CHECK), ensure_ascii=False))
    # 列出该 td 命中的所有规则
    print("RULES:", json.dumps(pg.evaluate("""() => {
      const td = document.querySelector('.table--cards tbody td:first-child');
      const out = [];
      for (const sheet of document.styleSheets) {
        let rs; try { rs = sheet.cssRules } catch(e) { continue }
        const walk = (rules, ctx) => {
          for (const r of rules) {
            if (r.cssRules) { walk(r.cssRules, (ctx||'') + (r.conditionText||r.media?.mediaText||'') + ' > '); continue; }
            if (!r.selectorText) continue;
            try { if (td.matches(r.selectorText)) out.push((ctx||'') + r.selectorText + ' {' + r.style.cssText.slice(0,120) + '}'); } catch(e){}
          }
        };
        walk(rs, '');
      }
      return out.filter(x => /position|width|display|background/.test(x)).slice(-14);
    }"""), ensure_ascii=False, indent=1))
    b.close()
