import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    outDir: '../src/agent3dify/resources/web',
    emptyOutDir: true,
    chunkSizeWarningLimit: 650,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:7860' },
  },
});
