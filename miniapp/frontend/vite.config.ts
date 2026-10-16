import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/static/app/',
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
  server: {
    host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:8765', '/healthz': 'http://127.0.0.1:8765' },
  },
});
