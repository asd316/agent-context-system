import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: { chunkSizeWarningLimit: 2000 },
  server: { port: 5181, proxy: { '/api': 'http://127.0.0.1:5180' } },
})
