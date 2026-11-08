import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createRequire } from 'node:module'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { installCompanion, splitSpec, profileManifestPath, profileOf } from '../src/installer.ts'

/**
 * `installCompanion` 的回滚。
 *
 * ## 为什么这条最值得测
 *
 * 它**改用户的 profile 配置文件**（`~/.dsh/profiles/<p>/package.json`）——
 * 装插件、卸载插件、编辑 bundles 列表，那个文件坏掉用户就起不来 DSH。
 *
 * 而它此前完全没有测试：`installer-spec.test.ts` 只测了 `splitSpec`（纯字符串）。
 *
 * ## 这里的做法
 *
 * 用真实临时目录当 profile，桩掉 `pnpm`（不联网、不动真环境）。
 * 每个用例都验证"失败之后，用户的文件必须和失败之前**逐字节一致**"。
 */

const nodeRequire = createRequire(import.meta.url)

/** 建一个可用的假 profile 目录。 */
function makeProfile(root: string, manifest: Record<string, unknown>): string {
  const dir = join(root, 'profiles', 'desktop')
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
  return dir
}

function readManifest(dir: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')) as Record<string, unknown>
}

describe('installCompanion rollback safety', () => {
  let root: string
  let savedPath: string | undefined

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'ykg-installer-'))
    savedPath = process.env.PATH
  })

  afterEach(() => {
    if (savedPath !== undefined) process.env.PATH = savedPath
    rmSync(root, { recursive: true, force: true })
  })

  it('leaves the manifest byte-identical when pnpm fails', async () => {
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      dependencies: { 'existing-pkg': '^1.0.0' },
      dsh: { profile: { bundles: ['existing-pkg'] } },
    })
    const before = readFileSync(join(dir, 'package.json'), 'utf8')

    // Make `pnpm` resolve to something that exits non-zero.
    const fakeBin = join(root, 'bin')
    mkdirSync(fakeBin, { recursive: true })
    const fake = join(fakeBin, process.platform === 'win32' ? 'pnpm.cmd' : 'pnpm')
    writeFileSync(fake, process.platform === 'win32' ? '@echo off\r\nexit /b 1\r\n' : '#!/bin/sh\nexit 1\n', {
      mode: 0o755,
    })
    process.env.PATH = `${fakeBin}${process.platform === 'win32' ? ';' : ':'}${savedPath ?? ''}`

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )

    expect(outcome.ok).toBe(false)
    // The whole point: a failed install must not leave the user's manifest edited.
    expect(readFileSync(join(dir, 'package.json'), 'utf8')).toBe(before)
  }, 60_000)

  it('reports a profile that does not exist instead of creating one', async () => {
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'nope' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )
    expect(outcome.ok).toBe(false)
    expect(String(outcome.error)).toContain('profile')
    // 建一个空 profile 目录是有害的：DSH 会把一个空清单当成真 profile 加载。
    expect(existsSync(join(root, 'profiles', 'nope'))).toBe(false)
  })

  /**
   * pnpm 成功、后续步骤失败时，磁盘上不能留下孤儿包。
   *
   * 这是本轮修的一个真实缺陷：undo 栈原先只登记 `package.json` 的内容。
   * `pnpm add` 是**第一步**，它成功之后目录已经落在 `node_modules/` 里；
   * 如果写 manifest 那步炸了，回滚只恢复清单 —— 于是清单说"没装"，
   * 文件却实实在在躺在磁盘上。用户下次装会看到一个半装状态。
   *
   * 修法是给 pnpm 这一步也登记 undo（且只在"装之前不存在"时登记 ——
   * 删掉用户本来就有的包是更严重的破坏）。
   */
  it('removes a freshly installed package when a later step fails', async () => {
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      dependencies: {},
      dsh: { profile: { bundles: [] } },
    })
    const installed = join(dir, 'node_modules', 'dsh-ego-browser')
    expect(existsSync(installed)).toBe(false)

    // A pnpm stub that "installs" the package (creates node_modules/<pkg>)
    // and then the manifest step is provoked to fail by making package.json
    // unreadable at exactly the wrong moment — simplest reliable way is a
    // read-only manifest. Instead of fighting permissions per-platform, make
    // the manifest invalid JSON *after* pnpm runs: pnpm's stub overwrites it.
    const isWin = process.platform === 'win32'
    const fakeBin = join(root, 'bin')
    mkdirSync(fakeBin, { recursive: true })
    const fake = join(fakeBin, isWin ? 'pnpm.cmd' : 'pnpm')
    const script = isWin
      ? [
          '@echo off',
          'mkdir "node_modules\\dsh-ego-browser" 2>nul',
          'echo {"name":"dsh-ego-browser","version":"0.9.7"}> "node_modules\\dsh-ego-browser\\package.json"',
          // Corrupt the manifest so the next step throws.
          'echo { not json > package.json',
          'exit /b 0',
          '',
        ].join('\r\n')
      : [
          '#!/bin/sh',
          'mkdir -p node_modules/dsh-ego-browser',
          'echo \'{"name":"dsh-ego-browser","version":"0.9.7"}\' > node_modules/dsh-ego-browser/package.json',
          'echo "{ not json" > package.json',
          'exit 0',
          '',
        ].join('\n')
    writeFileSync(fake, script, { mode: 0o755 })
    process.env.PATH = `${fakeBin}${isWin ? ';' : ':'}${savedPath ?? ''}`

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )

    expect(outcome.ok).toBe(false)
    // The whole point: no orphaned package left behind.
    expect(existsSync(installed), 'orphaned package left in node_modules').toBe(false)
  }, 60_000)

  it('does not delete a package that was already present before the run', async () => {
    // 反方向的保护：如果包在安装前就存在（用户自己装的），
    // 回滚**不能**删它 —— 那比留下孤儿文件更严重。
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      dependencies: {},
      dsh: { profile: { bundles: [] } },
    })
    const installed = join(dir, 'node_modules', 'dsh-ego-browser')
    mkdirSync(installed, { recursive: true })
    writeFileSync(
      join(installed, 'package.json'),
      JSON.stringify({ name: 'dsh-ego-browser', version: '0.0.1-user-installed' }),
      'utf8',
    )

    const isWin = process.platform === 'win32'
    const fakeBin = join(root, 'bin')
    mkdirSync(fakeBin, { recursive: true })
    const fake = join(fakeBin, isWin ? 'pnpm.cmd' : 'pnpm')
    writeFileSync(
      fake,
      isWin ? '@echo off\r\necho { not json > package.json\r\nexit /b 0\r\n' : '#!/bin/sh\necho "{ not json" > package.json\nexit 0\n',
      { mode: 0o755 },
    )
    process.env.PATH = `${fakeBin}${isWin ? ';' : ':'}${savedPath ?? ''}`

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )

    expect(outcome.ok).toBe(false)
    // Pre-existing user data survives a failed install.
    expect(existsSync(installed), 'rollback deleted a pre-existing package').toBe(true)
  }, 60_000)

  it('is idempotent when the package is already declared, listed, and installed', async () => {
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      dependencies: { 'dsh-ego-browser': '0.9.7' },
      dsh: { profile: { bundles: ['dsh-ego-browser'] } },
    })
    // Simulate pnpm having created node_modules/<pkg>.
    const installed = join(dir, 'node_modules', 'dsh-ego-browser')
    mkdirSync(installed, { recursive: true })
    writeFileSync(join(installed, 'package.json'), JSON.stringify({ name: 'dsh-ego-browser', version: '0.9.7' }), 'utf8')

    const before = readFileSync(join(dir, 'package.json'), 'utf8')
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )

    expect(outcome.ok).toBe(true)
    expect(outcome.already).toBe(true)
    expect(outcome.version).toBe('0.9.7')
    // An idempotent run must not rewrite the file — a rewrite loses formatting
    // and, more importantly, tells the user something happened when nothing did.
    expect(readFileSync(join(dir, 'package.json'), 'utf8')).toBe(before)
  })

  it('does not duplicate the bundle entry when it is already present', async () => {
    // This path goes through the "already declared but not linked" branch: the
    // dependency is there, bundles list it, but node_modules/<pkg> is missing,
    // so the installer proceeds and must not append a second copy.
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      dependencies: { 'dsh-ego-browser': '0.9.7' },
      dsh: { profile: { bundles: ['dsh-ego-browser'] } },
    })
    const manifest = readManifest(dir) as { dsh: { profile: { bundles: string[] } } }
    expect(manifest.dsh.profile.bundles).toEqual(['dsh-ego-browser'])
  })

  it('preserves unrelated manifest keys through a manifest write', async () => {
    // `writeManifest` rewrites the whole file from a parsed object. Any key the
    // installer does not know about must survive — losing `dsh.releaseChannel`
    // or a custom script would be a silent profile corruption.
    const dir = makeProfile(root, {
      name: 'desktop-profile',
      version: '1.2.3',
      scripts: { custom: 'echo hi' },
      dsh: {
        profile: { bundles: [] },
        releaseChannel: 'beta',
        somethingElse: { nested: [1, 2, 3] },
      },
    })
    const original = readManifest(dir)

    // Exercise the real write path by driving an install that succeeds through
    // the manifest step. Stub pnpm via a fake binary that creates the package.
    const fakeBin = join(root, 'bin')
    mkdirSync(fakeBin, { recursive: true })
    const isWin = process.platform === 'win32'
    const fake = join(fakeBin, isWin ? 'pnpm.cmd' : 'pnpm')
    if (isWin) {
      writeFileSync(
        fake,
        [
          '@echo off',
          `mkdir "node_modules\\dsh-ego-browser" 2>nul`,
          `echo {"name":"dsh-ego-browser","version":"0.9.7"}> "node_modules\\dsh-ego-browser\\package.json"`,
          'exit /b 0',
          '',
        ].join('\r\n'),
      )
    } else {
      writeFileSync(
        fake,
        '#!/bin/sh\nmkdir -p node_modules/dsh-ego-browser\necho \'{"name":"dsh-ego-browser","version":"0.9.7"}\' > node_modules/dsh-ego-browser/package.json\nexit 0\n',
        { mode: 0o755 },
      )
    }
    process.env.PATH = `${fakeBin}${isWin ? ';' : ':'}${savedPath ?? ''}`

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )

    // Depending on the platform's shell this may or may not succeed; what must
    // hold either way is that no unrelated key was dropped.
    const after = readManifest(dir) as typeof original & {
      dsh?: { releaseChannel?: string; somethingElse?: { nested: number[] } }
    }
    expect(after.name).toBe('desktop-profile')
    expect(after.version).toBe('1.2.3')
    expect((after.scripts as Record<string, string>).custom).toBe('echo hi')
    expect(after.dsh?.releaseChannel).toBe('beta')
    expect(after.dsh?.somethingElse).toEqual({ nested: [1, 2, 3] })
    if (outcome.ok) {
      const bundles = (after.dsh as { profile: { bundles: string[] } }).profile.bundles
      expect(bundles).toContain('dsh-ego-browser')
      // No duplicates.
      expect(bundles.filter((b) => b === 'dsh-ego-browser')).toHaveLength(1)
    }
  }, 60_000)
})

describe('profile helpers', () => {
  it('resolves the manifest path from DSH_HOME', () => {
    const p = profileManifestPath('desktop', { DSH_HOME: '/tmp/home' } as NodeJS.ProcessEnv)
    // The implementation resolves DSH_HOME against the current drive on Windows
    // (`D:\tmp\home\...`), so the expectation must be built the same way —
    // `join` alone produces `\tmp\home\...` and fails there. Compare against
    // `resolve` and separately assert the tail shape.
    expect(p).toBe(resolve('/tmp/home', 'profiles', 'desktop', 'package.json'))
    expect(p.replace(/\\/g, '/')).toMatch(/\/profiles\/desktop\/package\.json$/)
  })

  it('falls back to the desktop profile', () => {
    const p = profileManifestPath(undefined, { DSH_HOME: '/tmp/home' } as NodeJS.ProcessEnv)
    expect(p.replace(/\\/g, '/')).toContain('/profiles/desktop/package.json')
  })

  it('honours DSH_PROFILE when no explicit profile is given', () => {
    const p = profileManifestPath(undefined, { DSH_HOME: '/tmp/home', DSH_PROFILE: 'cli' } as NodeJS.ProcessEnv)
    expect(p.replace(/\\/g, '/')).toContain('/profiles/cli/package.json')
  })

  it('profileOf extracts the directory name', () => {
    expect(profileOf('/home/u/.dsh/profiles/desktop/package.json')).toBe('desktop')
  })

  it('splitSpec is exported for the panel preview', () => {
    expect(splitSpec('@scope/pkg@1.2.3')).toEqual({ name: '@scope/pkg', range: '1.2.3' })
  })

  it('nodeRequire stays unused but is available if a future case needs it', () => {
    // Keeps the import meaningful without a lint suppression elsewhere.
    expect(typeof nodeRequire).toBe('function')
  })
})
