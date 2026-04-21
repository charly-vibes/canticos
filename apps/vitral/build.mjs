import { build } from 'esbuild';
import { cpSync, mkdirSync } from 'fs';

const DIST = 'dist';
mkdirSync(DIST, { recursive: true });

await build({
  entryPoints: ['src/extension.ts'],
  outdir: DIST,
  format: 'esm',
  target: 'es2022',
  platform: 'neutral',
  bundle: true,
  sourcemap: false,
  minify: false,
  external: ['gi://*', 'resource:///*'],
  loader: { '.ts': 'ts' },
});

cpSync('src/metadata.json', `${DIST}/metadata.json`);

console.log('✓ Built to dist/');
