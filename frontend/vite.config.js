import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { resolve } from 'path';
export default defineConfig({
    plugins: [vue()],
    resolve: {
        alias: {
            '@': resolve(__dirname, 'src'),
        },
    },
    server: {
        port: 3000,
        strictPort: false,
        host: true,
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
                ws: true,
                rewrite: function (path) { return path.replace(/^\/api/, '/api'); },
            },
            '/data_init': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
            '/static': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
            '/media': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
        },
    },
    build: {
        outDir: 'dist',
        assetsDir: 'assets',
        sourcemap: false,
        target: 'es2020',
        minify: 'terser',
        terserOptions: {
            compress: {
                drop_console: true,
                drop_debugger: true,
            },
            output: {
                comments: false,
            },
        },
        rollupOptions: {
            output: {
                manualChunks: {
                    'element-plus': ['element-plus'],
                    'vue-vendor': ['vue', 'vue-router', 'pinia'],
                    'echarts': ['echarts'],
                },
            },
        },
        chunkSizeWarningLimit: 500,
    },
    optimizeDeps: {
        include: ['element-plus', 'vue', 'vue-router', 'pinia', '@element-plus/icons-vue'],
    },
});
