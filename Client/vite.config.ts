// vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'
import path from 'path'
import tailwindcss from 'tailwindcss'
import autoprefixer from 'autoprefixer'

// https://vite.dev/config/
export default defineConfig({
  // Load .env from monorepo root (one level up)
  envDir: path.resolve(__dirname, '..'),   // gig-connect/

  // Only load variables starting with VITE_
  envPrefix: ['VITE_'],

  server: {
    host: true,
    port: 5173,
    open: true,

    proxy: {
      // Proxy all /api requests to Flask backend on port 5000
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true,
        secure: false,
        // Keep /api prefix — Flask expects it
        rewrite: (path) => path,
      },

      // Proxy WebSocket (Socket.IO)
      '/socket.io': {
        target: 'http://localhost:5000',
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },

    // Allow larger payloads for file uploads / Socket.IO
    hmr: {
      clientPort: 5173,
    },
  },

  plugins: [
    react(),
  ],

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
    // Critical: Force pre-bundling of Supabase packages
    include: [
      '@supabase/supabase-js',
      '@supabase/auth-js',
      '@supabase/auth-js/dist/module/GoTrueClient',
      '@supabase/auth-js/dist/module/GoTrueAdminApi',
      '@supabase/gotrue-js',
      '@supabase/postgrest-js',
      '@supabase/realtime-js',
      '@supabase/storage-js',
    ],
    esbuildOptions: {
      // Modern target + browser platform fixes ESM resolution issues
      target: 'es2020',
      platform: 'browser',
      // Reduce noise from esbuild
      logLevel: 'silent',
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2020',  // Modern browsers
    minify: 'esbuild',
    commonjsOptions: {
      // Ignore dynamic requires in Supabase (prevents build errors)
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
      plugins: [
        tailwindcss(),
        autoprefixer(),
      ],
    },
  },

  // Suppress noisy esbuild warnings
  esbuild: {
    logOverride: {
      'this-is-undefined-in-esm': 'silent',
      'tsconfig-json-not-found': 'silent',
    },
  },

  // For vite preview (production-like testing)
  preview: {
    port: 4173,
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true,
        secure: false,
      },
      '/socket.io': {
        target: 'http://localhost:5000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})