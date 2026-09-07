import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: '/dios/',
  plugins: [react()],
  server: {
    proxy: {
      '/dios/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        rewrite: (path) => path.replace(/^\/dios\/api/, '/api'),
      },
    },
  },
})
