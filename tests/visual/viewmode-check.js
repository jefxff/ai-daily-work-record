// 视图模式（全宽 / 限宽居中）与台账表格规范验证
//
//   node viewmode-check.js
//
// 关键场景：27 寸 QHD（2560×1440）——限宽模式的价值只在这种宽度下才体现。
const { firefox } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE = process.env.BASE_URL || 'https://127.0.0.1:8443';
const LOCAL = 'localhost,127.0.0.1,::1';
process.env.NO_PROXY = [process.env.NO_PROXY, LOCAL].filter(Boolean).join(',');
process.env.no_proxy = process.env.NO_PROXY;

const SHOT_DIR = path.join(__dirname, '..', '..', '.shot', 'viewmode');
fs.mkdirSync(SHOT_DIR, { recursive: true });

const PAGES = [
  ['decoration', '/decoration/', true],
  ['daily', '/daily_record/', true],
  ['price', '/price_evaluation/', true],
  ['complaint', '/complaint/', true],
  ['samples', '/samples/samples/', true],
  ['archive', '/archive/', true],
  ['decoration_add(表单页)', '/decoration/add/', false],
];

async function measure(page) {
  return page.evaluate(() => {
    const main = document.getElementById('main');
    const btn = document.getElementById('viewModeToggle');
    const r = main ? main.getBoundingClientRect() : null;
    const cs = main ? getComputedStyle(main) : null;
    // 侧栏是 fixed 且贴左，内容轨道的「可用区」= 侧栏右缘 → 视口右缘（去掉外壳 padding）。
    // 居中要相对这个可用区判断，而不是相对视口，否则会把「侧栏占位」误判成不居中。
    let leftGap = null, rightGap = null;
    if (main && main.parentElement) {
      const wrap = main.parentElement;
      const wr = wrap.getBoundingClientRect();
      const wcs = getComputedStyle(wrap);
      const padL = parseFloat(wcs.paddingLeft) || 0;
      const padR = parseFloat(wcs.paddingRight) || 0;
      leftGap = Math.round(r.left - (wr.left + padL));
      rightGap = Math.round((wr.right - padR) - r.right);
    }
    const iconContract = document.querySelector('.vm-icon--contract');
    const iconExpand = document.querySelector('.vm-icon--expand');
    const vis = (el) => !!el && getComputedStyle(el).display !== 'none';

    // 表格规范抽样
    const table = document.querySelector('table.table-ledger');
    const numTh = document.querySelector('table th.col-num');
    let numTd = null;
    if (numTh) {
      const idx = Array.prototype.indexOf.call(numTh.parentNode.children, numTh) + 1;
      numTd = document.querySelector('table tbody td:nth-child(' + idx + ')');
    }
    const trunc = document.querySelector('table td.col-truncate');

    return {
      viewMode: document.documentElement.getAttribute('data-view-mode'),
      mainWidth: r ? Math.round(r.width) : null,
      mainLeft: r ? Math.round(r.left) : null,
      mainRight: r ? Math.round(window.innerWidth - r.right) : null,
      leftGap: leftGap,
      rightGap: rightGap,
      maxWidth: cs ? cs.maxWidth : null,
      hasLayoutLimited: main ? main.classList.contains('layout-limited') : false,
      btnExists: !!btn,
      btnLabel: btn ? btn.getAttribute('aria-label') : null,
      btnPressed: btn ? btn.getAttribute('aria-pressed') : null,
      iconContract: vis(iconContract),
      iconExpand: vis(iconExpand),
      ledgerTables: document.querySelectorAll('table.table-ledger').length,
      numThCount: document.querySelectorAll('table th.col-num').length,
      numTdAlign: numTd ? getComputedStyle(numTd).textAlign : null,
      numTdFont: numTd ? getComputedStyle(numTd).fontVariantNumeric : null,
      truncCount: document.querySelectorAll('table td.col-truncate').length,
      truncTitle: trunc ? (trunc.getAttribute('title') || '').slice(0, 20) : null,
      truncMax: trunc ? getComputedStyle(trunc).maxWidth : null,
    };
  });
}

(async () => {
  const browser = await firefox.launch();
  const ctx = await browser.newContext({
    viewport: { width: 2560, height: 1440 },
    storageState: path.join(__dirname, '.auth', 'state.json'),
    ignoreHTTPSErrors: true,
  });
  const page = await ctx.newPage();

  const log = [];
  const bad = [];

  // ---------- 1. 默认全宽 ----------
  await page.goto(BASE + '/decoration/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);
  let m = await measure(page);
  log.push(['默认状态', m]);
  if (m.viewMode !== 'full') bad.push('默认 viewMode 不是 full: ' + m.viewMode);
  if (m.mainWidth < 2000) bad.push('全宽模式主内容只有 ' + m.mainWidth + 'px，未铺满');
  if (!m.iconContract || m.iconExpand) bad.push('全宽模式图标显示错误');
  await page.screenshot({ path: path.join(SHOT_DIR, '01-full-2560.png'), fullPage: false });

  // ---------- 2. 点击切到限宽 ----------
  await page.click('#viewModeToggle');
  await page.waitForTimeout(300);
  m = await measure(page);
  log.push(['切换后(limited)', m]);
  if (m.viewMode !== 'limited') bad.push('点击后 viewMode 未变为 limited');
  if (m.mainWidth !== 1440) bad.push('限宽宽度不是 1440：' + m.mainWidth);
  if (!m.hasLayoutLimited) bad.push('main 未加 .layout-limited');
  if (Math.abs(m.leftGap - m.rightGap) > 2) {
    bad.push('内容区未居中：左留白 ' + m.leftGap + ' / 右留白 ' + m.rightGap);
  }
  if (!m.iconExpand || m.iconContract) bad.push('限宽模式图标未切成展开图标');
  if (m.btnPressed !== 'true') bad.push('aria-pressed 未同步');
  await page.screenshot({ path: path.join(SHOT_DIR, '02-limited-2560.png'), fullPage: false });

  // ---------- 3. 刷新保持 ----------
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);
  m = await measure(page);
  log.push(['刷新后', m]);
  if (m.viewMode !== 'limited') bad.push('刷新后状态丢失');
  if (m.mainWidth !== 1440) bad.push('刷新后宽度不对：' + m.mainWidth);

  // ---------- 4. 跨页面跳转保持 + 各页表格规范 ----------
  for (const [name, url, expectsTable] of PAGES) {
    await page.goto(BASE + url, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(350);
    const mm = await measure(page);
    log.push([name + ' ' + url, mm]);
    if (mm.viewMode !== 'limited') bad.push(name + ' 跳转后状态丢失');
    if (mm.mainWidth !== 1440) bad.push(name + ' 宽度不是 1440：' + mm.mainWidth);
    if (!expectsTable) continue;
    if (mm.ledgerTables < 1) bad.push(name + ' 未命中 .table-ledger');
    if (mm.numThCount > 0 && mm.numTdAlign !== 'right') {
      bad.push(name + ' 数值列表体未右对齐：' + mm.numTdAlign);
    }
    if (mm.numThCount > 0 && (mm.numTdFont || '').indexOf('tabular-nums') === -1) {
      bad.push(name + ' 数值列未启用等宽数字：' + mm.numTdFont);
    }
    if (mm.truncCount > 0 && !mm.truncTitle) bad.push(name + ' col-truncate 缺 title');
    await page.screenshot({ path: path.join(SHOT_DIR, 'limited-' + name + '.png') });
  }

  // ---------- 5. 切回全宽 ----------
  await page.click('#viewModeToggle');
  await page.waitForTimeout(300);
  m = await measure(page);
  log.push(['切回全宽', m]);
  if (m.viewMode !== 'full') bad.push('切回失败');
  if (m.mainWidth < 2000) bad.push('切回后宽度未恢复：' + m.mainWidth);
  if (!m.iconContract || m.iconExpand) bad.push('切回后图标未恢复');

  // ---------- 6. 小屏回归：限宽不应对窄屏产生任何影响 ----------
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.click('#viewModeToggle'); // 切到 limited
  await page.waitForTimeout(250);
  const narrow = await measure(page);
  log.push(['1280 限宽(应等于可用宽)', narrow]);
  if (narrow.mainWidth > 1280) bad.push('窄屏限宽宽度异常：' + narrow.mainWidth);

  console.log(JSON.stringify(log, null, 2));

  await browser.close();

  if (bad.length) {
    console.log('\n=== FAIL ===');
    bad.forEach((b) => console.log(' - ' + b));
    process.exit(1);
  }
  console.log('\n=== ALL PASS ===');
})().catch((e) => {
  console.error('ERROR', e);
  process.exit(2);
});
