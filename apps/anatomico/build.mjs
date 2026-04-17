/**
 * Build script: compile TypeScript → GJS-compatible ES modules.
 *
 * GJS imports like `gi://St` and `resource:///org/gnome/shell/...`
 * are left as-is (marked external) — GJS resolves them at runtime.
 */

import { build } from 'esbuild';
import { cpSync, mkdirSync, existsSync } from 'fs';

const DIST = 'dist';

// Ensure dist exists
mkdirSync(DIST, { recursive: true });

const shared = {
  outdir: DIST,
  format: 'esm',
  target: 'es2022',
  platform: 'neutral',
  bundle: true,            // Bundle but mark GJS imports as external
  sourcemap: false,
  minify: false,           // Keep readable for debugging in GJS
  external: [
    'gi://*',
    'resource:///*',
  ],
  loader: { '.ts': 'ts' },
};

await Promise.all([
  build({ ...shared, entryPoints: ['src/extension.ts'] }),
  build({ ...shared, entryPoints: ['src/prefs.ts'] }),
]);

// Copy static files to dist
cpSync('src/metadata.json', `${DIST}/metadata.json`);
cpSync('src/stylesheet.css', `${DIST}/stylesheet.css`);

// Copy and compile schemas
if (existsSync('src/schemas')) {
  cpSync('src/schemas', `${DIST}/schemas`, { recursive: true });
}

console.log('✓ Built to dist/');
console.log('  Run: bash install.sh');
