/* UI 响应式整改复测（只读验收工具）
 *
 * 用途：
 *   1) 在真实页面 + 目标浏览器上复现 P0-2 侧栏遮罩问题（不再用 setContent 内存探针）；
 *   2) 把审计报告里「按 1180px 文档宽线性外推」的 1024/1280/1366/1512/1600/1920
 *      补成真实浏览器量测，并连同原有 5 档一起落盘，供 G1 验收核对。
 *
 * 只读保证：不提交表单、不点击任何 delete/export/revoke/logout 链接；
 * 唯一交互是点击 #mobileMenuToggle 打开/关闭侧栏（纯前端状态）。
 *
 * 用法：
 *   NO_PROXY=localhost,127.0.0.1,::1 BASE_URL=https://127.0.0.1:8443 \
 *     node tests/visual/ui_responsive_recheck.js
 * 输出：.ui-audit/report_full.json
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE_URL || 'https://127.0.0.1:8443';
const ROOT = path.resolve(__dirname, '..', '..');
const STATE = path.join(ROOT, 'tests', 'visual', '.auth', 'state.json');
const OUT = path.join(ROOT, '.ui-audit', 'report_full.json');

/* 含审计原始 5 档、报告宣称的九档、以及 961px 翻转断点两侧 */
const WIDTHS = [320, 390, 768, 960, 961, 1024, 1180, 1280, 1366, 1440, 1512, 1600, 1920];

const PAGES = [
  ['home', '/'],
  ['workspace', '/workspace/'],
  ['dashboard', '/dashboard/'],
  ['daily_record_list', '/daily_record/'],
  ['daily_record_form', '/daily_record/add/'],
  ['complaint_list', '/complaint/'],
  ['price_eval_list', '/price_evaluation/'],
  ['customer_credit_list', '/customer_credit/'],
  ['decoration_list', '/decoration/'],
  ['knowledge', '/knowledge/'],
  ['data_download', '/data_download/'],
];

const MEASURE = () => {
  const de = document.documentElement;
  const vw = de.clientWidth;
  return {
    vw,
    path: location.pathname,
    title: document.title,
    h1: document.querySelectorAll('h1').length,
    docScrollW: de.scrollWidth,
    bodyScrollW: document.body.scrollWidth,
    overflowX: Math.max(0, de.scrollWidth - vw),
    isLogin: /login/i.test(location.pathname),
  };
};

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const ctx = await browser.newContext({
    storageState: STATE,
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 900 },
  });
  const page = await ctx.newPage();
  const result = {
    generatedAt: new Date().toISOString(),
    base: BASE,
    browser: browser.version(),
    widths: WIDTHS,
    source: '真实浏览器 DOM 量测（非外推）',
    pages: {},
    p0_2: null,
    errors: [],
  };

  /* ---------- P0-2 真实页面复现（390px） ---------- */
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(250);
  const snap = () => page.evaluate(() => {
    const el = document.getElementById('sidebarScrim');
    const cs = el ? getComputedStyle(el) : null;
    const r = el ? el.getBoundingClientRect() : null;
    return {
      bodyHasSidebarOpen: document.body.classList.contains('sidebar-open'),
      sidebarShow: document.getElementById('sidebar')?.classList.contains('show') ?? null,
      scrimExists: !!el,
      scrimHasHidden: el ? el.hasAttribute('hidden') : null,
      scrimDisplay: cs ? cs.display : null,
      scrimVisibility: cs ? cs.visibility : null,
      scrimOpacity: cs ? cs.opacity : null,
      scrimZIndex: cs ? cs.zIndex : null,
      scrimSize: r ? [Math.round(r.width), Math.round(r.height)] : null,
      ariaExpanded: document.getElementById('mobileMenuToggle')?.getAttribute('aria-expanded') ?? null,
    };
  });
  const before = await snap();
  /* 入场动画会让 Playwright 的 element-is-stable 检查超时，用原生 click 绕过 */
  await page.evaluate(() => document.getElementById('mobileMenuToggle').click());
  await page.waitForTimeout(350);
  const opened = await snap();
  await page.evaluate(() => document.getElementById('sidebarScrim').click());
  await page.waitForTimeout(250);
  const closed = await snap();
  result.p0_2 = { before, opened, closed };
  console.log('[P0-2] 打开后:', JSON.stringify(opened));
  console.log('[P0-2] 点遮罩关闭后:', JSON.stringify(closed));

  /* ---------- 13 档 × 11 页横向溢出实测 ---------- */
  let loginRedirects = 0;
  for (const [name, url] of PAGES) {
    result.pages[name] = {};
    for (const w of WIDTHS) {
      await page.setViewportSize({ width: w, height: 900 });
      try {
        await page.goto(BASE + url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForTimeout(60);
        const m = await page.evaluate(MEASURE);
        result.pages[name][String(w)] = m;
        if (m.isLogin) loginRedirects++;
      } catch (e) {
        result.errors.push(`${name}@${w}: ${String(e.message).slice(0, 120)}`);
        result.pages[name][String(w)] = { error: String(e.message).slice(0, 120) };
      }
    }
    const bad = WIDTHS.filter((w) => (result.pages[name][String(w)].overflowX || 0) > 0);
    console.log(`[溢出] ${name.padEnd(24)} ${bad.length}/${WIDTHS.length} 档: ${bad.map((w) => w + ':' + result.pages[name][String(w)].overflowX).join(', ') || '无'}`);
  }
  result.loginRedirects = loginRedirects;

  fs.writeFileSync(OUT, JSON.stringify(result, null, 2), 'utf8');
  console.log('\n落盘:', OUT);
  console.log('登录跳转次数（应为 0）:', loginRedirects, '| 错误数:', result.errors.length);
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
