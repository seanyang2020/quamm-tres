import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  // 相对 base：同一份构建可自适应根路径(9100/cpolar)与 Caddy 子路径 /quamm/；
  // 需要固定子路径时用 VITE_BASE=/quamm/ npm run build 覆盖。
  base: process.env.VITE_BASE || './',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8080'
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
