import { readFileSync } from 'fs';
import { fileURLToPath, URL } from 'url';
import { defineConfig, type Plugin } from 'vite';
import vue from '@vitejs/plugin-vue';
import vuetify from 'vite-plugin-vuetify';
import webfontDl from 'vite-plugin-webfont-dl';
// @ts-ignore — .mjs not in TS project scope; Vite resolves this at runtime
import { runMdiSubset } from './scripts/subset-mdi-font.mjs';

const t2iShikiRuntimePath = fileURLToPath(
  new URL('../astrbot/core/utils/t2i/template/shiki_runtime.iife.js', import.meta.url)
);

const astrbotInitPath = fileURLToPath(
  new URL('../astrbot/__init__.py', import.meta.url)
);

// Vite plugin: emit assets/version so the Core can verify dashboard compatibility
function dashboardVersionAsset(): Plugin {
  return {
    name: 'vite-plugin-dashboard-version',
    generateBundle() {
      try {
        const source = readFileSync(astrbotInitPath, 'utf-8');
        const match = source.match(/^\s*__version__\s*=\s*["']([^"']+)["']/m);
        if (!match) {
          this.warn('Skipping dashboard version asset: __version__ not found in astrbot/__init__.py');
          return;
        }
        this.emitFile({
          type: 'asset',
          fileName: 'assets/version',
          source: `v${match[1]}`
        });
      } catch (error) {
        this.warn(
          `Skipping dashboard version asset because astrbot/__init__.py could not be read: ${
            error instanceof Error ? error.message : String(error)
          }`
        );
      }
    }
  };
}

// Vite plugin: run MDI icon font subsetting (build only)
function mdiSubset() {
  return {
    name: 'vite-plugin-mdi-subset',
    async buildStart() {
      console.log('\n🔧 Running MDI icon font subsetting...');
      await runMdiSubset();
    },
  };
}

function t2iShikiRuntimeAsset(): Plugin {
  return {
    name: 'vite-plugin-t2i-shiki-runtime',
    configureServer(server) {
      server.middlewares.use('/t2i/shiki_runtime.iife.js', (_req, res) => {
        try {
          res.statusCode = 200;
          res.setHeader('Content-Type', 'text/javascript; charset=utf-8');
          res.end(readFileSync(t2iShikiRuntimePath));
        } catch (error) {
          res.statusCode = 404;
          res.end(`T2I Shiki runtime not found: ${error instanceof Error ? error.message : String(error)}`);
        }
      });
    },
    generateBundle() {
      try {
        this.emitFile({
          type: 'asset',
          fileName: 't2i/shiki_runtime.iife.js',
          source: readFileSync(t2iShikiRuntimePath)
        });
      } catch (error) {
        this.warn(
          `Skipping T2I Shiki runtime asset because it could not be read: ${
            error instanceof Error ? error.message : String(error)
          }`
        );
      }
    }
  };
}

// https://vitejs.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [
    // Only run MDI subsetting during production builds, skip in dev server
    ...(command === 'build' ? [mdiSubset()] : []),
    dashboardVersionAsset(),
    t2iShikiRuntimeAsset(),
    vue({
      template: {
        compilerOptions: {
          isCustomElement: (tag) => ['v-list-recognize-title'].includes(tag)
        }
      }
    }),
    vuetify({
      autoImport: true
    }),
    webfontDl()
  ],
  resolve: {
    alias: [
      {
        find: /^shiki$/,
        replacement: fileURLToPath(new URL('./src/utils/shikiLimitedBundle.js', import.meta.url))
      },
      {
        find: /^stream-monaco$/,
        replacement: fileURLToPath(new URL('./src/utils/streamMonacoDisabled.js', import.meta.url))
      },
      {
        find: 'mermaid',
        replacement: 'mermaid/dist/mermaid.js'
      },
      {
        find: '@',
        replacement: fileURLToPath(new URL('./src', import.meta.url))
      }
    ]
  },
  css: {
    preprocessorOptions: {
      scss: {}
    }
  },
  build: {
    sourcemap: false,
    chunkSizeWarningLimit: 1024 * 1024 // Set the limit to 1 MB
  },
  optimizeDeps: {
    exclude: ['vuetify'],
    entries: ['./src/**/*.vue']
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:6185/',
        changeOrigin: true,
        ws: true
      }
    }
  }
}));
