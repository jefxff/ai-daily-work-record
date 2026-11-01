// 视觉回归与无障碍基线（审计 FE-04）
// 前置：Django 服务运行在 BASE_URL；已生成 .auth/state.json（见 scripts/gen_visual_auth.py）
const { defineConfig, devices } = require('@playwright/test');

// 本地回归只打 127.0.0.1，绕开系统/沙箱代理（否则会被代理拦成 502 upstream connect failed）
const LOCAL_HOSTS = 'localhost,127.0.0.1,::1';
process.env.NO_PROXY = [process.env.NO_PROXY, LOCAL_HOSTS].filter(Boolean).join(',');
process.env.no_proxy = process.env.NO_PROXY;

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
    // 本地用自签证书跑 8443；CI 走 http 时该选项无副作用
    ignoreHTTPSErrors: true,
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
