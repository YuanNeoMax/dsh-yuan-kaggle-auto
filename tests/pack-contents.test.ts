import { describe, it, expect, beforeAll } from 'vitest'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'

/**
 * 真正检查**打进 tarball 的内容**。
 *
 * ## 为什么单靠 `files` 数组不够
 *
 * `manifest.test.ts` 里那条 `ships every file the runtime actually reads`
 * 只比对 `files` 数组里的**字符串**：写了 `engine/kauto/*.py` 就算通过。
 * 而 `every publish pattern actually matches files on disk` 又只证明
 * 文件在磁盘上 —— 两者加起来仍然不能回答"用户装到的包里到底有什么"。
 *
 * 实测过这件事：把 `engine/fixtures/**` 从 `files` 里删掉，
 * 上面两条测试**全都还是绿的**，而 `npm pack` 已经不含夹具了 ——
 * 「跑通演练」会在每个全新安装上失败，且只在用户那边失败。
 *
 * 所以这里直接跑 `npm pack --dry-run` 读它的输出。这是唯一能证明
 * "发布物是完整的"的方式：npm 自己的文件选择逻辑说了算，不是我们的猜测。
 *
 * `--dry-run` 只列清单、不写 tarball，可以安全地在测试里跑。
 */

const root = join(import.meta.dirname, '..')

interface PackResult {
  files: string[]
  totalFiles: number
}

function runPack(): PackResult {
  const raw = execFileSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: root,
    encoding: 'utf8',
    // npm 会往 stderr 写进度；JSON 在 stdout。
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 120_000,
    shell: process.platform === 'win32',
  }) as string
  const parsed = JSON.parse(raw) as Array<{ files: Array<{ path: string }>; entryCount?: number }>
  const entry = parsed[0]
  const files = (entry?.files ?? []).map((f) => f.path.replace(/\\/g, '/'))
  return { files, totalFiles: entry?.entryCount ?? files.length }
}

describe('the published tarball actually contains what the runtime needs', () => {
  let pack: PackResult

  beforeAll(() => {
    pack = runPack()
  }, 150_000)

  it('packs a plausible number of files', () => {
    // A regression that drops a whole directory shows up here first.
    expect(pack.totalFiles).toBeGreaterThan(200)
    expect(pack.files.length).toBeGreaterThan(200)
  })

  it('includes both bundles and both manifest files', () => {
    for (const required of ['lib/index.js', 'lib/client.js', 'cordis.patch.yml', 'dsh-plugin.json']) {
      expect(pack.files, `missing from tarball: ${required}`).toContain(required)
    }
  })

  it('includes the whole engine source', () => {
    const modules = pack.files.filter((p) => /^engine\/kauto\/[^/]+\.py$/.test(p))
    // 24 today. A floor rather than an equality so adding modules is free,
    // while losing one shows up as a smaller number.
    expect(modules.length, `engine modules in tarball: ${modules.length}`).toBeGreaterThanOrEqual(20)
  })

  it('includes every engine test script', () => {
    const tests = pack.files.filter((p) => /^engine\/tests\/[^/]+\.py$/.test(p))
    expect(tests.length).toBeGreaterThanOrEqual(50)
    // The runner itself must travel too, or the shipped tests cannot be run.
    expect(pack.files).toContain('engine/run_all_tests.py')
  })

  it('includes the fixture the offline rehearsal reads', () => {
    // This is the assertion the `files`-array check could not make.
    // `kauto smoke` defaults to fixtures/demo-comp; without it the panel's
    // 「跑通演练」 fails on every fresh install.
    for (const file of ['train.csv', 'test.csv', 'sample_submission.csv']) {
      expect(pack.files, `fixture missing from tarball: ${file}`).toContain(
        `engine/fixtures/demo-comp/${file}`,
      )
    }
  })

  it('includes the dependency lock file', () => {
    // The setup step pip-installs from this exact file; without it a fresh
    // install has no dependency list at all.
    expect(pack.files).toContain('engine/requirements.lock.txt')
  })

  it('includes the docs users are told to read', () => {
    for (const doc of ['README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md']) {
      expect(pack.files).toContain(doc)
    }
  })

  it('carries no bytecode caches or scratch directories', () => {
    const noise = pack.files.filter(
      (p) => p.includes('__pycache__') || p.includes('.tmp-') || p.endsWith('.pyc'),
    )
    expect(noise, `unwanted entries: ${noise.slice(0, 5).join(', ')}`).toEqual([])
  })

  it('carries no absolute paths from the packing machine', () => {
    // A tarball that bakes in `C:\\Users\\...` is both a leak and a bug: the
    // fixture meta files used to contain exactly that (fixed by relocating
    // `work_dir` at load time, and by shipping them without it).
    const suspicious = pack.files.filter((p) => /^[A-Za-z]:/.test(p) || p.includes('..'))
    expect(suspicious).toEqual([])
  })
})
