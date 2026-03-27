// Client/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'
import path from 'path'
import tailwindcss from 'tailwindcss'
import autoprefixer from 'autoprefixer'

export default defineConfig({
  // Load .env from monorepo root
  envDir: path.resolve(__dirname, '..'),

  // Only load variables starting with VITE_
  envPrefix: ['VITE_'],

  server: {
    host: true,
    port: 5173,
    open: true,

    proxy: {
      // === MAIN PROXY: All API calls go through the API Gateway ===
      '/api': {
        target: 'http://127.0.0.1:5000',        // ← API Gateway (port 5000)
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path,                 // Keep /api prefix
      },

      // === Socket.IO WebSocket Proxy ===
      '/socket.io': {
        target: 'http://127.0.0.1:5000',        // API Gateway handles Socket.IO
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },

    hmr: {
      clientPort: 5173,
    },
  },

  plugins: [react()],

  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@components': path.resolve(__dirname, './src/components'),
      '@pages': path.resolve(__dirname, './src/pages'),
      '@lib': path.resolve(__dirname, './src/lib'),
      '@context': path.resolve(__dirname, './src/context'),
      '@hooks': path.resolve(__dirname, './src/hooks'),
    },
  },

  optimizeDeps: {
    include: [
      '@supabase/supabase-js',
      '@supabase/auth-js',
      '@supabase/gotrue-js',
      '@supabase/postgrest-js',
      '@supabase/realtime-js',
      '@supabase/storage-js',
    ],
    esbuildOptions: {
      target: 'es2020',
      platform: 'browser',
      logLevel: 'silent',
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2020',
    minify: 'esbuild',
    commonjsOptions: {
      ignoreDynamicRequires: true,
      transformMixedEsModules: true,
    },
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', '@supabase/supabase-js'],
          ui: [
            '@radix-ui/*',
            'class-variance-authority',
            'tailwind-merge',
            'lucide-react',
          ],
        },
      },
    },
  },

  css: {
    postcss: {
      plugins: [tailwindcss(), autoprefixer()],
    },
  },

  esbuild: {
    logOverride: {
      'this-is-undefined-in-esm': 'silent',
      'tsconfig-json-not-found': 'silent',
    },
  },

  // Preview mode (for testing built version)
  preview: {
    port: 4173,
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
        secure: false,
      },
      '/socket.io': {
        target: 'http://127.0.0.1:5000',
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },
  },
})