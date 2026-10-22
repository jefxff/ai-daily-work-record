// 视觉回归与无障碍基线（审计 FE-04）
// 前置：Django 服务运行在 BASE_URL；已生成 .auth/state.json（见 scripts/gen_visual_auth.py）
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.02, animations: 'disabled' },
  },
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: process.env.BASE_URL || 'http://127.0.0.1:8080',
    locale: 'zh-CN',
    colorScheme: 'light',
  },
  projects: [
    {
      name: 'desktop',
      // 本机 Chromium 下载受限，基线统一使用已安装的 Firefox；CI 可改回 Desktop Chrome
      use: { ...devices['Desktop Firefox'], viewport: { width: 1440, height: 900 } },
      testIgnore: /mobile\.spec\.js/,
    },
    {
      name: 'mobile',
      use: {
        ...devices['Desktop Firefox'],
        viewport: { width: 393, height: 851 },
        isMobile: false,
        hasTouch: true,
      },
      testMatch: /mobile\.spec\.js/,
    },
  ],
});
