// 移动端（Pixel 5）：验证小屏无横向溢出，并留存视觉基线
const { test, expect } = require('@playwright/test');

const AUTH_STATE = process.env.VISUAL_AUTH_STATE || '.auth/state.json';
// CI 不做像素比对（Linux 与本地 Windows 基线存在渲染差异），只保留溢出断言
const A11Y_ONLY = process.env.CI_A11Y_ONLY === '1';

const PAGES = [
  { name: 'home-mobile', path: '/' },
  { name: 'daily-records-mobile', path: '/daily_record/' },
  { name: 'insurance-subjects-mobile', path: '/insurance/subjects/' },
];

test.use({ storageState: AUTH_STATE });

for (const item of PAGES) {
  test(`${item.name} 无横向溢出`, async ({ page }) => {
    await page.goto(item.path);
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
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `${item.path} 横向溢出 ${overflow}px`).toBeLessThanOrEqual(1);
    if (!A11Y_ONLY) {
      await expect(page).toHaveScreenshot(`${item.name}.png`, { fullPage: false });
    }
  });
}
