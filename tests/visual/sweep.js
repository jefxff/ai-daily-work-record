// 全站路由巡检：状态码 / 控制台错误 / h1 数量（B7 验收）
const { firefox } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE_URL || 'https://127.0.0.1:8443';
const AUTH = path.resolve(__dirname, '.auth/state.json');
const SKIP = /\/admin\/|delete|revoke|logout|refresh|read|run-|_run|upload|export|download\/|ajax|api\/|oplogs|status\/|favicon|probe|bootstrap|conversation\/latest|sync-batch|ai-config|template\/$/;

(async () => {
  const routes = fs.readFileSync(path.resolve(__dirname, '../../.shot_routes.txt'), 'utf8')
    .split('\n').map((s) => s.trim()).filter(Boolean)
    .filter((r) => !SKIP.test(r) && !r.includes('?'));

  const browser = await firefox.launch();
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true, storageState: AUTH, viewport: { width: 1440, height: 900 }, locale: 'zh-CN',
  });
  const page = await ctx.newPage();
  const problems = [];
  const h1bad = [];
  let ok = 0;

  for (const r of routes) {
    const errs = [];
    const onConsole = (m) => { if (m.type() === 'error') errs.push(m.text().slice(0, 160)); };
    const onError = (e) => errs.push('pageerror: ' + e.message.slice(0, 160));
    page.on('console', onConsole);
    page.on('pageerror', onError);
    let status = 0;
    try {
      const resp = await page.goto(BASE + r, { waitUntil: 'load', timeout: 20000 });
      status = resp ? resp.status() : 0;
      await page.waitForTimeout(120);
    } catch (e) {
      problems.push(`${r} | NAVFAIL | ${e.message.slice(0, 120)}`);
    }
    const counts = await page.evaluate(() => ({
      h1: document.querySelectorAll('h1').length,
      h1text: (document.querySelector('h1') || {}).textContent?.trim().slice(0, 30) || '',
    })).catch(() => ({ h1: -1, h1text: '' }));

    if (status !== 200) problems.push(`${r} | HTTP ${status}`);
    if (errs.length) problems.push(`${r} | console: ${errs.slice(0, 3).join(' || ')}`);
    if (counts.h1 !== 1) h1bad.push(`${r} | h1=${counts.h1} | ${counts.h1text}`);
    if (status === 200 && !errs.length) ok += 1;

    page.off('console', onConsole);
    page.off('pageerror', onError);
  }

  console.log(`巡检 ${routes.length} 条，健康 ${ok} 条`);
  console.log('=== 问题 ===');
  problems.slice(0, 40).forEach((p) => console.log(' ', p));
  console.log(`问题合计 ${problems.length}`);
  console.log('=== h1 数量不等于 1 的页面 ===');
  h1bad.slice(0, 40).forEach((p) => console.log(' ', p));
  console.log(`h1 异常合计 ${h1bad.length}`);
  await browser.close();
})();
