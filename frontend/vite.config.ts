import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The browser talks only to the Vite origin; Vite forwards API and WebSocket traffic
// to the backend (docs/design/12 §22.3). In Docker the target is http://api:8000.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      // Health lives outside /api/v1 (docs/design/07 §13.3); paths are never rewritten.
      '/health': { target: apiTarget, changeOrigin: true },
      '/ws': { target: apiTarget, changeOrigin: true, ws: true },
    },
  },
})
