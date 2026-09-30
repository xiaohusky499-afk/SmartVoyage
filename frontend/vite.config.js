import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxy = { '/api': 'http://127.0.0.1:8088' }

export default defineConfig({
  plugins: [vue()],
  server: { proxy },
  preview: { proxy },
})
