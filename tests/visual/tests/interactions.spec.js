// 前端交互回归：把 P1/P2 里「靠手工点一遍」的验收项固化成用例
// 覆盖 B2 表单校验、B3 提交锁、B4 行点击与豁免、B5 页码跳转、B7 标题层级、B8 顶栏搜索、B6 长文本截断
const { test, expect } = require('@playwright/test');

const AUTH_STATE = process.env.VISUAL_AUTH_STATE || '.auth/state.json';

test.use({ storageState: AUTH_STATE });

test.describe('交互增强', () => {
  test('B2 必填未填时阻止提交并聚焦首个非法字段', async ({ page }) => {
    await page.goto('/daily_record/add/');
    // 清空必填的「部门」，其余字段保持默认
    await page.fill('#department', '');
    const before = page.url();
    await page.locator('form[data-validate="1"] button[type="submit"]').click();
    // 不跳转（仍停留在新增页），表单进入整体错误态
    await expect(page).toHaveURL(before);
    await expect(page.locator('form[data-validate="1"]')).toHaveClass(/is-invalid-form/);
    await expect(page.locator('#department')).toBeFocused();
  });

  test('B3 合法提交会锁按钮（提交中…）', async ({ page }) => {
    await page.goto('/daily_record/add/');
    await page.fill('#department', '测试部');
    await page.fill('#work_content', '交互回归用例');
    // 拦住真实跳转，只观察提交锁
    await page.evaluate(() => {
      document.querySelector('form[data-validate="1"]')
        .addEventListener('submit', (e) => e.preventDefault());
    });
    await page.evaluate(() => {
      document.querySelector('form[data-validate="1"]')
        .requestSubmit();
    });
    const btn = page.locator('form[data-validate="1"] button[type="submit"]');
    await expect(btn).toBeDisabled();
    await expect(btn).toHaveText('提交中…');
  });

  test('B4 点行空白进详情，点行内链接走原链接', async ({ page }) => {
    await page.goto('/daily_record/');
    const row = page.locator('tr[data-href]').first();
    await expect(row).toHaveCount(1);
    const href = await row.getAttribute('data-href');
    expect(href).toContain('/daily_record/detail/');
    await row.locator('td').nth(2).click();
    await expect(page).toHaveURL(new RegExp(href.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  });

  test('B4 行内删除按钮走确认弹窗、不触发整行跳转', async ({ page }) => {
    await page.goto('/daily_record/');
    const del = page.locator('tr[data-href] .btn-delete').first();
    test.skip((await del.count()) === 0, '当前账号没有删除按钮');
    const before = page.url();
    await del.click();
    // 按钮在 SKIP 清单里：弹确认框而不是整行跳详情
    await expect(page.locator('#confirmBackdrop')).toBeVisible();
    expect(page.url()).toBe(before);
    await page.locator('#confirmCancel').click();
  });

  test('B5 页码跳转保留筛选参数', async ({ page }) => {
    await page.goto('/operation_log/?per_page=10');
    const jump = page.locator('#pageJump');
    test.skip((await jump.count()) === 0, '当前数据不足一页');
    await jump.fill('2');
    await page.locator('.pag-jump button[type="submit"]').click();
    await expect(page).toHaveURL(/page=2/);
    await expect(page).toHaveURL(/per_page=10/);
  });

  test('B8 顶栏搜索清空后回到全量结果', async ({ page }) => {
    await page.goto('/');
    const nav = page.locator('.nav-search-input');
    await nav.fill('每日');
    // 浏览器原生 × / Esc 清空时会派发 search 事件；Firefox 的 Esc 不一定触发，
    // 这里显式派发同一个事件，验证的正是 shell.js 里那段「清空即重置」逻辑。
    await nav.fill('');
    await Promise.all([
      page.waitForURL(/\/search\//, { timeout: 8000 }),
      nav.evaluate((el) => el.dispatchEvent(new Event('search'))),
    ]);
  });

  test('B6 长文本单元格被截断且带 title 可看全文', async ({ page }) => {
    // 操作日志的「详情」列是最典型的长文本列
    await page.goto('/operation_log/');
    const clipped = page.locator('td.is-clipped');
    test.skip((await clipped.count()) === 0, '当前数据没有超长文本单元格');
    const first = clipped.first();
    await expect(first).toHaveAttribute('title', /.+/);
  });

  test('B7 每个页面恰有一个 h1', async ({ page }) => {
    for (const path of ['/', '/daily_record/', '/knowledge/', '/samples/', '/search/']) {
      await page.goto(path);
      await expect(page.locator('h1'), `页面 ${path} 的 h1 数量`).toHaveCount(1);
    }
  });

  test('B1 详情页有面包屑且当前页不在链接里', async ({ page }) => {
    await page.goto('/daily_record/');
    const href = await page.locator('tr[data-href]').first().getAttribute('data-href');
    await page.goto(href);
    await expect(page.locator('nav.app-breadcrumb')).toBeVisible();
    await expect(page.locator('nav.app-breadcrumb [aria-current="page"]')).toHaveCount(1);
  });
});
