// 全站逐页巡检：页面健康 + 每个链接可达性 + 每页整页截图
//
// 用途：手工点一遍全站不现实，这里用真实浏览器从登录态出发，
// 沿站内链接广度优先爬一遍，逐页记录状态、控制台错误、布局异常，
// 再把页面上出现过的每个 href 单独点一次（HTTP 校验），最后整页截图。
//
// 用法：
//   BASE_URL=https://127.0.0.1:8443 node crawl.js
//   产物：.shot/pages/<slug>.png、.shot/crawl-report.json、.shot/links.json
//
// 安全：所有会改数据的路径（delete/revoke/reset/logout/export/...)一律不导航、
// 不请求——巡检只读，不能因为点链接把生产数据点没了。

const { firefox } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE_URL || 'https://127.0.0.1:8443';
const ORIGIN = new URL(BASE).origin;
const AUTH = path.resolve(__dirname, '.auth/state.json');
// 截图目录可用 SHOT_DIR 覆盖，避免每轮都要清理旧目录
const SHOT_DIR = process.env.SHOT_DIR
  ? path.resolve(process.env.SHOT_DIR)
  : path.resolve(__dirname, '../../.shot/pages');
const REPORT = path.resolve(__dirname, '../../.shot/crawl-report.json');
const LINKS = path.resolve(__dirname, '../../.shot/links.json');
const MAX_PAGES = Number(process.env.MAX_PAGES || 1200);
const MAX_DEPTH = Number(process.env.MAX_DEPTH || 3);

// 只读巡检：这些路径允许出现在页面上，但绝不跟着点
const DENY = /(delete|revoke|remove|clear|reset|logout|signout|export|download|upload|import\/confirm|backup\/start|backup\/restore|run-|_run|sync|refresh|ajax|api\/|csp-report|favicon|\.(csv|xlsx|pdf|zip|png|jpg|svg|ico)(\?|$))/i;

// 种子里的纯端点（非页面）：返回 JSON 或只接受 POST
const SKIP_SEED = /(api-token-auth|copilot\/ask|copilot\/feedback|copilot\/message\/draft|customer_ledger\/import\/confirm|insurance\/renew|insurance\/subjects\/detail|insurance\/subjects\/one-off|system\/backup|system\/health|system\/logs|system\/perf)/;

const slug = (u) => {
  const s = u.replace(ORIGIN, '').replace(/^\/|\/$/g, '').replace(/[^\w\u4e00-\u9fa5-]+/g, '_');
  return (s || 'root').slice(0, 90);
};

const sameSite = (href, base) => {
  try {
    const u = new URL(href, base || ORIGIN);
    return u.origin === ORIGIN ? u : null;
  } catch {
    return null;
  }
};

// 开发服务器在密集请求下会偶发重置连接，重试两次再判定
const fetchWithRetry = async (req, url) => {
  let last = '';
  for (let i = 0; i < 3; i++) {
    try {
      const r = await req.get(url, { maxRedirects: 5, timeout: 25000, failOnStatusCode: false });
      return { status: r.status() };
    } catch (e) {
      last = String(e).split('\n')[0].slice(0, 120);
      await new Promise((res) => setTimeout(res, 200 * (i + 1)));
    }
  }
  return { status: 'ERR:' + last };
};

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });

  const seeds = fs.readFileSync(path.resolve(__dirname, '../../.shot_routes.txt'), 'utf8')
    .split('\n').map((s) => s.trim()).filter(Boolean)
    .filter((r) => !SKIP_SEED.test(r) && !DENY.test(r));

  const browser = await firefox.launch();
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true,
    storageState: AUTH,
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  const page = await ctx.newPage();

  const seen = new Set();
  const queue = seeds.map((r) => ({ url: r, depth: 0, from: '(seed)' }));
  const pages = [];
  const linkTargets = new Map(); // url -> Set(来源页)
  const pathVariants = new Map(); // pathname -> 已收集变体数

  const consoleErrs = [];
  let currentUrl = '';
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrs.push({ page: currentUrl, text: m.text().slice(0, 300) });
  });
  page.on('pageerror', (e) => consoleErrs.push({ page: currentUrl, text: 'PAGEERROR ' + String(e).slice(0, 300) }));

  while (queue.length && pages.length < MAX_PAGES) {
    const { url, depth, from } = queue.shift();
    if (seen.has(url)) continue;
    seen.add(url);
    currentUrl = url;
    consoleErrs.length = 0;

    let resp = null;
    let navErr = null;
    try {
      resp = await page.goto(ORIGIN + url, { waitUntil: 'load', timeout: 25000 });
    } catch (e) {
      navErr = String(e).split('\n')[0].slice(0, 160);
    }
    const status = resp ? resp.status() : 0;
    const ct = resp ? (resp.headers()['content-type'] || '') : '';
    const isHtml = ct.includes('text/html');

    if (navErr || status >= 400 || !isHtml) {
      pages.push({ url, status, type: isHtml ? 'html' : 'other', navErr, from, skipped: true });
      continue;
    }

    // 等图表/懒加载落位
    await page.waitForTimeout(120);

    const info = await page.evaluate(() => {
      const d = document.documentElement;
      const main = document.querySelector('main') || document.body;
      const anchors = Array.from(document.querySelectorAll('a[href]'));
      const imgs = Array.from(document.querySelectorAll('img'));
      return {
        title: document.title,
        h1: document.querySelectorAll('h1').length,
        textLen: (main.innerText || '').trim().length,
        scrollW: d.scrollWidth,
        viewW: window.innerWidth,
        links: anchors.map((a) => a.getAttribute('href')).filter(Boolean),
        brokenImgs: imgs.filter((i) => i.complete && i.naturalWidth === 0).map((i) => i.getAttribute('src')),
        tableCount: document.querySelectorAll('table').length,
      };
    });

    const shot = path.join(SHOT_DIR, slug(url) + '.png');
    try {
      await page.screenshot({ path: shot, fullPage: true });
    } catch (e) {
      /* 超长页截图失败不算页面故障 */
    }

    pages.push({
      url, status, title: info.title, h1: info.h1, textLen: info.textLen,
      overflowX: info.scrollW - info.viewW, brokenImgs: info.brokenImgs,
      links: info.links.length, tables: info.tableCount,
      console: consoleErrs.slice(0, 5), shot: path.basename(shot), from,
    });

    for (const href of info.links) {
      // 相对链接（?o=1.2.3、?page=2 这类）必须按「当前页」解析，不能按站点根
      const u = sameSite(href, ORIGIN + url);
      if (!u) continue;
      const clean = u.pathname + (u.search || '');
      const n = (pathVariants.get(u.pathname) || 0) + 1;
      pathVariants.set(u.pathname, n);
      // 同一路径的排序/筛选变体最多留 3 个，否则 1000+ 组合查不动
      if (n <= 3 || !u.search) {
        if (!linkTargets.has(clean)) linkTargets.set(clean, new Set());
        linkTargets.get(clean).add(url);
      }
      // 爬取只按路径扩散，query 变体不再入队；Django 后台走单独一套页面，
      // 进去会带出几千个 model 链接把额度烧光，这里只记录链接不深入
      const pathOnly = u.pathname;
      if (depth < MAX_DEPTH && !DENY.test(pathOnly) && !pathOnly.startsWith('/admin/') && !seen.has(pathOnly)) {
        queue.push({ url: pathOnly, depth: depth + 1, from: url });
      }
    }
  }

  // 逐个链接做 HTTP 可达性校验
  const linkResults = [];
  const req = ctx.request;
  for (const [target, froms] of linkTargets) {
    // Django 后台不是本项目的模板，且非超管大量 403，单独计一类
    if (target.startsWith('/admin/')) {
      linkResults.push({ url: target, status: 'skipped-admin', from: [...froms].slice(0, 3) });
      continue;
    }
    if (DENY.test(target)) {
      linkResults.push({ url: target, status: 'skipped', from: [...froms].slice(0, 3) });
      continue;
    }
    const { status } = await fetchWithRetry(req, ORIGIN + target);
    linkResults.push({ url: target, status, from: [...froms].slice(0, 3) });
    await new Promise((r) => setTimeout(r, 40)); // 别把开发服务器压到重置连接
  }

  await browser.close();

  fs.writeFileSync(REPORT, JSON.stringify({ base: BASE, pages }, null, 1), 'utf8');
  fs.writeFileSync(LINKS, JSON.stringify(linkResults, null, 1), 'utf8');

  // ---- 汇总 ----
  const real = pages.filter((p) => !p.skipped);
  const bad = pages.filter((p) => p.skipped && p.status !== 302 && p.status !== 301);
  const withConsole = real.filter((p) => p.console.length);
  const overflow = real.filter((p) => p.overflowX > 2);
  const blank = real.filter((p) => p.textLen < 30);
  const h1bad = real.filter((p) => p.h1 !== 1);
  const brokenLink = linkResults.filter((l) => typeof l.status === 'number' && l.status >= 400);
  const errLink = linkResults.filter((l) => String(l.status).startsWith('ERR'));
  const okLink = linkResults.filter((l) => l.status === 200 || l.status === 302 || l.status === 301);

  console.log(`抓取页面 ${pages.length} 个（HTML 正常 ${real.length}）`);
  console.log(`站内链接 ${linkResults.length} 个：可达 ${okLink.length}，4xx/5xx ${brokenLink.length}，请求失败 ${errLink.length}，主动跳过 ${linkResults.filter((l) => l.status === 'skipped').length}，后台链接 ${linkResults.filter((l) => l.status === 'skipped-admin').length}`);
  console.log(`控制台有错 ${withConsole.length} 页 / 横向溢出 ${overflow.length} 页 / 内容为空 ${blank.length} 页 / h1 异常 ${h1bad.length} 页`);
  if (bad.length) {
    console.log('--- 非页面端点（预期可忽略）---');
    bad.slice(0, 15).forEach((p) => console.log(`  ${p.url} | ${p.status || p.navErr}`));
  }
  if (brokenLink.length) {
    console.log('--- 坏链 ---');
    brokenLink.slice(0, 40).forEach((l) => console.log(`  ${l.status} ${l.url}  ← ${l.from.join(', ')}`));
  }
  if (errLink.length) {
    console.log('--- 请求失败 ---');
    errLink.slice(0, 20).forEach((l) => console.log(`  ${l.url} | ${l.status}`));
  }
  if (withConsole.length) {
    console.log('--- 控制台错误 ---');
    withConsole.slice(0, 20).forEach((p) => console.log(`  ${p.url} | ${p.console[0].text}`));
  }
  if (overflow.length) {
    console.log('--- 横向溢出 ---');
    overflow.slice(0, 20).forEach((p) => console.log(`  ${p.url} | +${p.overflowX}px`));
  }
  if (blank.length) console.log('--- 内容为空 ---\n  ' + blank.map((p) => p.url).join('\n  '));
  if (h1bad.length) console.log('--- h1 异常 ---\n  ' + h1bad.map((p) => `${p.url} (${p.h1})`).join('\n  '));
})();
