import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/login': 'http://localhost:8091',
      '/register': 'http://localhost:8091',
      '/authorize': 'http://localhost:8091',
      '/consent': 'http://localhost:8091',
      '/token': 'http://localhost:8091',
      // Gateway admin/audit APIs (production path prefix is /gw/* via Caddy;
      // dev proxies and strips the prefix so the gateway sees /api/*).
      '/gw': {
        target: 'http://localhost:8092',
        rewrite: (path) => path.replace(/^\/gw/, ''),
      },
    },
  },
})
