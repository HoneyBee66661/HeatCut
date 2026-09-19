import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        // HEATCUT_API_TARGET points the dev proxy at a STUB api for UI tests
        // (scripts/campaign_captions_history_e2e.py); default = local backend.
        target: process.env.HEATCUT_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      }
    }
  }
})

