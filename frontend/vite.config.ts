import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { port: 5180, strictPort: true },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    // Tests live beside the module they cover (see .pddrc, frontend context).
    // Vitest's default glob only matches `*.test.*` / `*.spec.*`, but pdd names
    // a generated test `test_<stem>.tsx` — Python's convention, which it applies
    // to every language. Without these extra patterns such a file lands in the
    // right folder and is silently never run, which is worse than no test.
    include: [
      'src/**/*.{test,spec}.{ts,tsx}',
      'src/**/test_*.{ts,tsx}',
      'src/**/*_test.{ts,tsx}',
    ],
    exclude: ['node_modules', 'dist'],
  },
});
