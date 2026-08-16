import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 公開サイト（/）だけを Vue で作る。運用画面（/ops）は Express が
// server/ops.html をそのまま返すため、このビルドには含まれない。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` 中は API を Express 側に転送する。
    // 本番では Express が dist/ を配信するのでプロキシは不要。
    proxy: {
      '/api': 'http://localhost:3000',
      '/ops': 'http://localhost:3000',
    },
  },
})
