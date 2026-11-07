import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Manifest coherence.
 *
 * Three files restate the plugin's identity, and DSH matches them by string:
 *   - `package.json` `name` must equal the Loader entry's package name;
 *   - `cordis.patch.yml` `name:` must equal `package.json` `name` (the client
 *     scanner matches the Loader specifier against the package name, so an
 *     alias makes the row look like a non-client row and the panel silently
 *     disappears);
 *   - `dsh-plugin.json` `id` and `version` must agree with the package.
 *
 * None of these failures produce an error message — they produce a plugin that
 * loads without its panel. That is exactly the kind of defect a test should own.
 */

const root = join(import.meta.dirname, '..')
const read = (name: string): string => readFileSync(join(root, name), 'utf8')

const pkg = JSON.parse(read('package.json')) as {
  name: string
  version: string
  files?: string[]
  dsh?: { bundle?: { patch?: string }; client?: { platform?: string } }
}
const manifest = JSON.parse(read('dsh-plugin.json')) as {
  id: string
  name: string
  version: string
  manifestVersion: string
}
const patch = read('cordis.patch.yml')

describe('manifest coherence', () => {
  it('keeps package.json and dsh-plugin.json versions in step', () => {
    expect(manifest.version).toBe(pkg.version)
  })

  it('uses one package name everywhere the Loader will look', () => {
    expect(manifest.id).toBe(pkg.name)
    // The patch file's `name:` must be the package name, not the entry id.
    expect(patch).toContain(`name: "${pkg.name}"`)
  })

  it('declares the entry id the settings namespace expects', () => {
    expect(patch).toContain('id: yuan-kaggle-auto')
    expect(manifest.name).toBe('yuan-kaggle-auto')
  })

  it('points the bundle at the patch file it ships', () => {
    expect(pkg.dsh?.bundle?.patch).toBe('./cordis.patch.yml')
  })

  it('declares a web client so the panel is injected', () => {
    expect(pkg.dsh?.client?.platform).toBe('web')
  })

  it('ships every file the runtime actually reads', () => {
    const files = pkg.files ?? []
    // Each of these is loaded or executed at runtime; dropping one from the
    // publish list produces a plugin that installs and then cannot run.
    for (const required of [
      'lib/index.js',
      'lib/client.js',
      'engine/kauto/*.py',
      'engine/run_all_tests.py',
      'engine/requirements.lock.txt',
      'cordis.patch.yml',
      'dsh-plugin.json',
    ]) {
      expect(files).toContain(required)
    }
  })

  it('declares a manifest version the host recognises', () => {
    expect(manifest.manifestVersion).toBe('0.15')
  })
})
