import { defineConfig } from 'vite';
import { libraryApi } from './server/library.js';

export default defineConfig({
  plugins: [{
    name: 'seekdb-library',
    configureServer(server) { server.middlewares.use('/api', libraryApi()); },
    configurePreviewServer(server) { server.middlewares.use('/api', libraryApi()); },
  }],
  optimizeDeps: { exclude: ['@huggingface/transformers'] },
});
