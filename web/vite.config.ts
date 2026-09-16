import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 无 CDN 依赖：字体走系统字体栈，markdown 渲染器（marked）与 sanitizer（dompurify）
// 都是本地 npm 依赖，离线机器上 npm install 之后即可构建。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      // 后端（并行重写中）默认监听 8000。
      // SSE 走同一前缀，代理会原样透传流式响应。
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
