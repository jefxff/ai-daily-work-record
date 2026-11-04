// Chromium 内核复核：用户截图里的表格内嵌竖向滑块在 Firefox 下不复现，
// 用系统 Edge 再验一次，确认取消 max-height/overflow 后没有滑块、也没有页面横向溢出。
const { chromium } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = 'https://127.0.0.1:8443';
const OUT = path.resolve(__dirname, '../../.shot/ui-fix');
process.env.NO_PROXY = 'localhost,127.0.0.1,::1';
process.env.no_proxy = process.env.NO_PROXY;

const PAGES = [
  ['anomalies', '/operation_log/anomalies/'],
  ['renewal', '/insurance/renewal_report/'],
  ['daily', '/daily_record/'],
];

const MEASURE = `(() => {
  const els = [...document.querySelectorAll('.table-responsive, .table-container')];
  const scrollable = els.filter((el) => {
    const cs = getComputedStyle(el);
    return cs.maxHeight !== 'none' || cs.overflowY === 'auto' || cs.overflowY === 'scroll';
  });
  const overflowing = els.filter((el) => el.scrollHeight > el.clientHeight + 2);
  return {
    containers: els.length,
    innerScrollable: scrollable.length,
    verticalOverflow: overflowing.length,
    pageHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
    pageScrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  };
})()`;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge' });
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true,
    storageState: path.resolve(__dirname, '.auth/state.json'),
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  const out = [];
  for (const [name, url] of PAGES) {
    const page = await ctx.newPage();
    const r = await page.goto(BASE + url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(900);
    const m = await page.evaluate(MEASURE);
    await page.screenshot({ path: path.join(OUT, `edge_${name}.png`), animations: 'disabled' });
    out.push({ name, status: r.status(), ...m });
    await page.close();
  }
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch((e) => { console.error(String(e).split('\n')[0]); process.exit(1); });
