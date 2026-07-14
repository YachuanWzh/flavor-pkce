import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/login': 'http://localhost:8000',
      '/register': 'http://localhost:8000',
      '/authorize': 'http://localhost:8000',
      '/consent': 'http://localhost:8000',
      '/token': 'http://localhost:8000',
    },
  },
})
