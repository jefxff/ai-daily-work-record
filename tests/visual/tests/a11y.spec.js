// 桌面端：关键页面无障碍扫描 + 视觉基线
const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const AUTH_STATE = process.env.VISUAL_AUTH_STATE || '.auth/state.json';

const PAGES = [
  { name: 'home', path: '/', marker: '待处理' },
  { name: 'dashboard', path: '/dashboard/', marker: '数据展示' },
  { name: 'daily-records', path: '/daily_record/', marker: '每日记录' },
  { name: 'insurance-subjects', path: '/insurance/subjects/', marker: '保险标的' },
  { name: 'knowledge', path: '/knowledge/', marker: '知识库' },
];

async function freeze(page) {
  // 结束页面进场动画（.main-content 的 opacity 过渡）：否则 axe 会按半透明
  // 状态计算对比度，产生「#7a818d 对比度不足」这类动画中间态误报。
  await page.evaluate(() => {
    for (const animation of document.getAnimations()) {
      try {
        animation.finish();
      } catch (e) {
        animation.cancel();
      }
    }
  });
  await page.addStyleTag({
    content:
      '*,*::before,*::after{animation:none!important;transition:none!important;}'
      + '.main-content{opacity:1!important;transform:none!important;}',
  });
  await page.waitForTimeout(120);
}

async function assertA11y(page, label) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  const serious = results.violations.filter((v) => ['serious', 'critical'].includes(v.impact));
  expect(
    serious.map((v) => ({ id: v.id, help: v.help, nodes: v.nodes.length })),
    `${label} 存在严重/致命级无障碍问题`,
  ).toEqual([]);
}

test.describe('登录页（未登录）', () => {
  test('无障碍与视觉基线', async ({ page }) => {
    await page.goto('/accounts/login/');
    await freeze(page);
    await expect(page.locator('h1')).toContainText('账号登录');
    await assertA11y(page, 'login');
    await expect(page).toHaveScreenshot('login.png', { fullPage: true });
  });
});

test.describe('已登录关键页面', () => {
  test.use({ storageState: AUTH_STATE });

  for (const item of PAGES) {
    test(`${item.name} 无障碍与视觉基线`, async ({ page }) => {
      const resp = await page.goto(item.path);
      expect(resp.status(), `${item.path} 状态码`).toBeLessThan(400);
      await freeze(page);
      if (item.marker) {
        await expect(page.locator('body')).toContainText(item.marker);
      }
      await assertA11y(page, item.name);
      await expect(page).toHaveScreenshot(`${item.name}.png`, { fullPage: true });
    });
  }
});
