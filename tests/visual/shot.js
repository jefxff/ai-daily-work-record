// 视觉回归截图 + 布局度量采集（前端缺陷逐项修复前后对比用）
//
//   node shot.js <config.json> <outDir> <label>
//
// config 为用例数组，每项：
//   { name, url, firstHref?, click?, waitFor?, measure? }
//   - firstHref：先打开 url，取匹配元素的 href 或 data-href 再导航（用于详情页）
//   - click：截图前点击的选择器（用于展开「更多」菜单）
//   - measure：浏览器内求值的 JS 表达式字符串，结果写入 *_metrics.json
//
// 使用项目已有的登录态 .auth/state.json；Firefox 与 Playwright 依赖见 package.json。
const { firefox } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE_URL || 'https://127.0.0.1:8443';
const LOCAL_HOSTS = 'localhost,127.0.0.1,::1';
process.env.NO_PROXY = [process.env.NO_PROXY, LOCAL_HOSTS].filter(Boolean).join(',');
process.env.no_proxy = process.env.NO_PROXY;

(async () => {
  const [cfgPath, outDirArg, label] = process.argv.slice(2);
  if (!cfgPath || !outDirArg || !label) {
    throw new Error('usage: node shot.js <config.json> <outDir> <label>');
  }
  const allCases = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  // ONLY=01 只跑某一项，便于「修一项、截一张、比一张」
  const cases = process.env.ONLY
    ? allCases.filter((c) => c.name.includes(process.env.ONLY))
    : allCases;
  const outDir = path.resolve(outDirArg);
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await firefox.launch();
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true,
    storageState: path.resolve(__dirname, '.auth/state.json'),
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });

  const results = [];
  for (const c of cases) {
    const page = await ctx.newPage();
    const consoleErrors = [];
    page.on('pageerror', (e) => consoleErrors.push(String(e).slice(0, 160)));
    let status = 0;
    let url = c.url.startsWith('http') ? c.url : BASE + c.url;
    try {
      let resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      status = resp ? resp.status() : 0;
      await page.waitForTimeout(700);

      if (c.firstHref) {
        const href = await page.evaluate((sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          return el.getAttribute('href') || el.getAttribute('data-href') || null;
        }, c.firstHref);
        if (!href) throw new Error(`firstHref 未找到: ${c.firstHref}`);
        resp = await page.goto(new URL(href, BASE).href, { waitUntil: 'domcontentloaded', timeout: 30000 });
        status = resp ? resp.status() : 0;
        await page.waitForTimeout(700);
      }

      if (c.click) {
        const el = page.locator(c.click).first();
        await el.waitFor({ state: 'visible', timeout: 8000 });
        await el.click();
        await page.waitForTimeout(500);
      }
      if (c.waitFor) {
        await page.waitForSelector(c.waitFor, { timeout: 8000 }).catch(() => {});
      }

      url = page.url();
      const title = await page.title();
      const loginRedirect = /login/.test(url);
      let measures = null;
      if (c.measure) {
        measures = await page.evaluate(c.measure).catch((e) => ({ error: String(e).slice(0, 200) }));
      }
      const file = path.join(outDir, `${label}_${c.name}.png`);
      await page.screenshot({ path: file, animations: 'disabled' });
      results.push({ name: c.name, status, url, title, loginRedirect, measures, consoleErrors, file });
    } catch (e) {
      results.push({ name: c.name, status, url, error: String(e).split('\n')[0].slice(0, 200) });
    }
    await page.close();
  }

  fs.writeFileSync(
    path.join(outDir, `${label}_metrics.json`),
    JSON.stringify(results, null, 2),
    'utf8',
  );
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
