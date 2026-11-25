import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.booklab.app',
  appName: '书析',
  webDir: 'www',
  bundledWebRuntime: false,
  android: {
    allowMixedContent: true,
    // 允许明文 HTTP（本地资源无需，但避免意外）
    webContentsDebuggingEnabled: false,
  },
  server: {
    // 用本地资源，不连服务器
    androidScheme: 'https',
  },
};

export default config;
