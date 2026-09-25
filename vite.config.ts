import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // The repo root contains a `.venv` with faster-whisper/onnxruntime — tens of
    // thousands of files. Watching it burns the host's inotify budget
    // (`ENOSPC: System limit for number of file watchers reached`), which kills
    // any SECOND dev server started next to this one (UI tests run their own on
    // :5199). Nothing under these paths is ever served by vite.
    watch: {
      ignored: ['**/.venv/**', '**/__pycache__/**', '**/.git/**', '**/logs/**', '**/dist/**'],
    },
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

