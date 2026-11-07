/**
 * dsh-yuan-kaggle-auto build: two bundles.
 *
 *   1. host  — src/index.ts → lib/index.js (Node ESM). DSH host services
 *      (cordis / schemastery / dsh-tools / dsh-settings) stay external: the
 *      running host provides them, and inlining a second copy of schemastery
 *      breaks the Loader's volatile-field handling (it must be DSH's own fork).
 *
 *   2. client — src/client/index.ts → lib/client.js wrapped in the DSH
 *      ModuleLoader factory (browser CJS). React and the client-* peers come
 *      from the host module table.
 *
 * The Python engine is shipped as source (engine/), never bundled: it runs in
 * its own venv under a different interpreter.
 */
import { defineConfig, type UserConfig } from 'tsdown'

const ID = 'dsh-yuan-kaggle-auto'

/** Host-provided singletons: never bundle, keep as runtime imports. */
const HOST_EXTERNALS = [
  '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-tools/invariant',
  '@deepseek-ai/dsh-settings',
  '@deepseek-ai/cordis',
  '@deepseek-ai/schemastery',
  /^@deepseek-ai\/dsh-client-/,
  'react',
  'react-dom',
  'react/jsx-runtime',
]

/** Browser-side peers the ModuleLoader resolves from the profile node_modules. */
const CLIENT_EXTERNALS = [
  'react',
  'react/jsx-runtime',
  'react-dom',
  /^@deepseek-ai\/dsh-client-/,
]

const host: UserConfig = {
  name: ID,
  entry: { index: 'src/index.ts' },
  outDir: 'lib',
  format: ['esm'],
  platform: 'node',
  target: 'es2022',
  dts: false,
  clean: true,
  external: HOST_EXTERNALS,
}

const client: UserConfig = {
  name: `${ID}/client`,
  entry: { client: 'src/client/index.tsx' },
  outDir: 'lib',
  format: ['cjs'],
  platform: 'browser',
  target: 'es2022',
  dts: false,
  sourcemap: false,
  clean: false,
  external: CLIENT_EXTERNALS,
  // The host webview has no `process` global; bake NODE_ENV at build time.
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  outputOptions: {
    entryFileNames: 'client.js',
    banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(ID)}, factory: (require) => {`,
    footer: 'return module.exports; } });',
    intro: 'var module = { exports: {} }; var exports = module.exports;',
  },
}

export default [host, client]
