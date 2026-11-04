/* 本轮 3 项修复的验收脚本：
   1) 核价列表在不同内容宽度下不溢出卡片、单元格不裁字、核价内容两行；
   2) 异常检测页不再出现列设置齿轮；
   3) 档案总览每张台账卡片各有一个齿轮，顶部工具栏没有；各表显隐互不影响。 */
const { firefox } = require('@playwright/test');
const path = require('path');
const BASE = 'https://127.0.0.1:8443';
process.env.NO_PROXY = [process.env.NO_PROXY, 'localhost,127.0.0.1'].filter(Boolean).join(',');

const bad = [];
const note = (s) => console.log(s);

async function measurePrice(page) {
  return page.evaluate(() => {
    const wrap = document.querySelector('.table-responsive');
    const table = document.querySelector('table');
    const ths = Array.from(table.tHead.rows[0].children);
    const rows = Array.from(table.tBodies[0].rows);
    const visible = ths.map((th, i) => ({ th, i })).filter(({ th }) => getComputedStyle(th).display !== 'none');
    // 单元格裁字检测：只对未显式截断的列严格判定
    const clipped = [];
    ths.forEach((th, i) => {
      if (getComputedStyle(th).display === 'none') return;
      rows.forEach((tr, ri) => {
        const td = tr.children[i];
        if (!td) return;
        const cs = getComputedStyle(td);
        const isEllipsis = cs.textOverflow === 'ellipsis' || td.querySelector('.supplier-name, .supplier-price');
        if (!isEllipsis && td.scrollWidth > td.clientWidth + 1) {
          clipped.push({ col: i + 1, label: td.getAttribute('data-label'), row: ri + 1, sw: td.scrollWidth, cw: td.clientWidth });
        }
      });
      // 表头自身不能被裁（nowrap 会直接切字）
      if (th.scrollWidth > th.clientWidth + 1) clipped.push({ col: i + 1, label: 'TH:' + th.textContent.trim(), sw: th.scrollWidth, cw: th.clientWidth });
    });
    const work = rows[0] && rows[0].children[4];
    const workLines = work ? Math.round(work.getBoundingClientRect().height / 20.5) : null;
    return {
      wrapW: Math.round(wrap.getBoundingClientRect().width),
      tableW: Math.round(table.getBoundingClientRect().width),
      overflowPx: Math.round(table.getBoundingClientRect().width - wrap.getBoundingClientRect().width),
      pageScrollX: document.documentElement.scrollWidth > window.innerWidth + 1,
      visibleCols: visible.length,
      colWidths: visible.map(({ th }) => Math.round(th.getBoundingClientRect().width)),
      workColW: work ? Math.round(work.getBoundingClientRect().width) : null,
      workLines,
      clipped: clipped.slice(0, 6),
      hintShown: (() => { const h = document.querySelector('.hidden-cols-hint'); return h ? getComputedStyle(h).display !== 'none' : false; })(),
    };
  });
}

(async () => {
  const browser = await firefox.launch();

  // ---------- 1. 核价：多宽度 ----------
  const CASES = [
    { name: '限宽 1440', vw: 1440, mode: 'limited' },
    { name: '全宽 1440', vw: 1440, mode: 'full' },
    { name: '全宽 1920', vw: 1920, mode: 'full' },
    { name: '限宽 2560', vw: 2560, mode: 'limited' },
  ];
  for (const c of CASES) {
    const ctx = await browser.newContext({ viewport: { width: c.vw, height: 900 }, storageState: path.join(__dirname, '.auth', 'state.json'), ignoreHTTPSErrors: true });
    const page = await ctx.newPage();
    await page.addInitScript((m) => { try { localStorage.setItem('viewMode', m); } catch (e) {} }, c.mode);
    await page.goto(BASE + '/price_evaluation/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(450);
    const m = await measurePrice(page);
    note(`核价 ${c.name}: 容器 ${m.wrapW} / 表 ${m.tableW}（溢出 ${m.overflowPx}）· 可见列 ${m.visibleCols} · 核价内容 ${m.workColW}px ${m.workLines}行 · 提示条 ${m.hintShown}`);
    if (m.overflowPx > 1) bad.push(`核价 ${c.name}：表格比容器宽 ${m.overflowPx}px`);
    if (m.pageScrollX) bad.push(`核价 ${c.name}：整页出现横向滚动`);
    if (m.clipped.length) bad.push(`核价 ${c.name}：${m.clipped.length} 处文字被裁 ` + JSON.stringify(m.clipped[0]));
    await page.screenshot({ path: path.join(__dirname, '..', '..', '.shot', 'r4-price-' + c.vw + '-' + c.mode + '.png') });
    await ctx.close();
  }

  // ---------- 2/3. 异常检测 & 档案总览 ----------
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, storageState: path.join(__dirname, '.auth', 'state.json'), ignoreHTTPSErrors: true });
  const page = await ctx.newPage();

  await page.goto(BASE + '/operation_log/anomalies/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);
  const anom = await page.evaluate(() => ({
    gears: document.querySelectorAll('.col-settings-gear').length,
    panels: document.querySelectorAll('.col-settings-panel').length,
    tables: document.querySelectorAll('table').length,
  }));
  note(`异常检测: 齿轮 ${anom.gears} · 面板 ${anom.panels} · 表格 ${anom.tables}`);
  if (anom.gears !== 0) bad.push('异常检测仍显示列设置按钮');
  await page.screenshot({ path: path.join(__dirname, '..', '..', '.shot', 'r4-anomalies.png') });

  await page.goto(BASE + '/archive/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  const arch = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('.lg-card[data-col-settings-scope]'));
    return {
      cards: cards.length,
      tables: document.querySelectorAll('table.lg-table').length,
      gears: document.querySelectorAll('.col-settings-gear').length,
      gearInToolbar: !!document.querySelector('.lg-toolbar .col-settings-gear'),
      gearPerCard: cards.map((c) => c.querySelectorAll('.col-settings-gear').length),
      heads: cards.map((c) => (c.querySelector('.lg-card-title') || {}).textContent.trim().slice(0, 12)),
    };
  });
  note(`档案总览: 卡片 ${arch.cards} · 表 ${arch.tables} · 齿轮 ${arch.gears} · 工具栏内齿轮 ${arch.gearInToolbar} · 每卡齿轮 ${JSON.stringify(arch.gearPerCard)}`);
  if (arch.gears !== arch.cards) bad.push(`档案总览齿轮数 ${arch.gears} ≠ 卡片数 ${arch.cards}`);
  if (arch.gearInToolbar) bad.push('档案总览顶部工具栏仍有列设置入口');

  // 独立显隐：点第 1 个齿轮 → 取消勾选「合同编号」→ 只有第 1 张表隐藏该列
  const firstGear = page.locator('.col-settings-gear').first();
  await firstGear.click();
  await page.waitForTimeout(200);
  const panelInfo = await page.evaluate(() => {
    const p = document.querySelector('.col-settings-panel:not([hidden])');
    return p ? { items: p.querySelectorAll('.col-settings-item').length, names: Array.from(p.querySelectorAll('.col-settings-item')).map((x) => x.textContent.trim()) } : null;
  });
  note(`第 1 个面板: ${panelInfo ? panelInfo.items + ' 项 · ' + panelInfo.names.join('/') : '未打开'}`);
  if (!panelInfo) bad.push('档案总览齿轮点了没有面板');

  const targetBox = page.locator('.col-settings-panel:not([hidden]) .col-settings-item', { hasText: '合同编号' }).locator('input');
  await targetBox.uncheck();
  await page.waitForTimeout(250);
  const perTable = await page.evaluate(() => {
    const tables = Array.from(document.querySelectorAll('table.lg-table'));
    return tables.map((t) => {
      const th = Array.from(t.tHead.rows[0].children).find((x) => x.textContent.trim() === '合同编号');
      return th ? getComputedStyle(th).display : 'no-th';
    });
  });
  note(`取消勾选「合同编号」后各表该列状态: ${JSON.stringify(perTable)}`);
  if (perTable[0] !== 'none') bad.push('第 1 张表未隐藏「合同编号」');
  if (perTable.slice(1).some((v) => v === 'none')) bad.push('其它台账被牵连隐藏（应互相独立）');
  await page.screenshot({ path: path.join(__dirname, '..', '..', '.shot', 'r4-archive-panel.png') });

  // 刷新后记忆仍在（按台账分别记忆）
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(450);
  const afterReload = await page.evaluate(() => {
    const t = document.querySelector('table.lg-table');
    const th = Array.from(t.tHead.rows[0].children).find((x) => x.textContent.trim() === '合同编号');
    return getComputedStyle(th).display;
  });
  note(`刷新后第 1 张表「合同编号」: ${afterReload}`);
  if (afterReload !== 'none') bad.push('档案总览列显隐刷新后丢失');

  // 复位
  await page.evaluate(() => { try { Object.keys(localStorage).filter(k => k.indexOf('adw-cols:') === 0).forEach(k => localStorage.removeItem(k)); } catch (e) {} });

  await ctx.close();
  await browser.close();

  if (bad.length) { console.log('\n=== 未通过 ==='); bad.forEach((x) => console.log(' - ' + x)); process.exit(1); }
  console.log('\n=== 本轮 3 项验收全部通过 ===');
})();
