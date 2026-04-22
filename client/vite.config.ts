import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  base: './',
  build: {
    outDir:   '../socialhome/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8099',
      '/ws':  { target: 'ws://localhost:8099', ws: true },
    },
  },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
})
