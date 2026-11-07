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

  /**
   * 上一条只比对 `files` 数组里的**字符串**：写了 `engine/kauto/*.py` 就算通过，
   * 哪怕磁盘上一个 `.py` 都没有。而用户装的是这个包 ——
   * "本地全绿、装上就坏"正是从这种缝隙里长出来的。
   *
   * 所以这一条真的去 glob 磁盘，核对每个模式都实际匹配到文件。
   */
  it('every publish pattern actually matches files on disk', () => {
    const files = pkg.files ?? []
    const { globSync } = require('node:fs') as typeof import('node:fs')
    // 只检查非通配的确定路径；`**` 类的全量匹配太慢且脆弱。
    const patterns = files.filter((p) => !p.includes('**'))
    expect(patterns.length).toBeGreaterThan(0)
    for (const pattern of patterns) {
      const matched = globSync(pattern, { cwd: root })
      expect(matched.length, `pattern matched nothing: ${pattern}`).toBeGreaterThan(0)
    }
  })

  it('the engine ships as a complete package', () => {
    const { globSync } = require('node:fs') as typeof import('node:fs')
    const modules = globSync('engine/kauto/*.py', { cwd: root })
    // 24 modules today; the floor lets modules be added freely while catching
    // the directory going missing.
    expect(modules.length).toBeGreaterThanOrEqual(20)
    // Windows globs return backslashes; normalise before matching, or this
    // passes on the author's mac and fails here (or the reverse).
    const normalised = modules.map((p) => p.replace(/\\/g, '/'))
    for (const entry of ['cli.py', 'kapi.py', 'prep.py']) {
      expect(
        normalised.some((p) => p.endsWith(`/${entry}`)),
        `missing engine/kauto/${entry}`,
      ).toBe(true)
    }
  })

  it('ships the fixture the offline rehearsal needs', () => {
    const { globSync } = require('node:fs') as typeof import('node:fs')
    // `kauto smoke` defaults to fixtures/demo-comp. If the fixture is not
    // published, the panel's 「跑通演练」 fails on every fresh install — and it
    // would fail for users only, never in the repo.
    for (const file of ['train.csv', 'test.csv', 'sample_submission.csv']) {
      const found = globSync(`engine/fixtures/demo-comp/${file}`, { cwd: root })
      expect(found.length, `fixture missing: ${file}`).toBe(1)
    }
  })

  it('never publishes caches or scratch directories', () => {
    const files = pkg.files ?? []
    // A stray `__pycache__` in the tarball is not fatal, but users pay for it
    // and it can carry absolute paths from the machine that packed it.
    for (const noise of ['**/__pycache__/**', '**/.tmp-*/**', '**/*.pyc']) {
      expect(files).not.toContain(noise)
    }
  })
})
