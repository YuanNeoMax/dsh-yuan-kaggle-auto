import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { installCompanion } from '../src/installer.ts'

/**
 * `installCompanion` 的**成功路径**。
 *
 * 上一轮覆盖了回滚（失败时文件逐字节不变、不留孤儿包）。这一轮补主干：
 * 真的装成功时，到底发生了什么、`loader.create` 收到什么。
 *
 * ## 为什么 loader.create 的参数是重点
 *
 * 它是"免重启生效"的唯一机制。`id` 或 `name` 传错，会出现两种静默后果：
 *
 * - `name` 错 → Loader 用错的 specifier 去 require，插件加载不出来，
 *   但 `installCompanion` 仍然报成功（步骤里 `load.ok` 是 true）；
 * - `id` 错 → 设置面板的命名空间对不上，面板静默消失。
 *
 * 这两种都不会抛异常，所以只能靠断言入参来防。
 *
 * pnpm 用桩替换（不联网）。loader 也用假的 —— 这里验证的是"我们传了什么"，
 * 不是宿主怎么实现 create。
 */

function makeProfile(root: string, manifest: Record<string, unknown>): string {
  const dir = join(root, 'profiles', 'desktop')
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
  return dir
}

/**
 * 造一个会「成功安装」的 pnpm 桩。
 *
 * 关键点：**真实 pnpm 会写 dependencies**。实测
 * `pnpm add is-number --save-exact` 之后 package.json 变成
 * `{"dependencies":{"is-number":"7.0.0"}}`。
 *
 * 第一版桩只创建了 node_modules，于是 `dependencies` 断言失败 ——
 * 那不是产品缺陷，是桩不真实。桩的行为要覆盖被测代码依赖的那部分契约，
 * 否则测试在验证一个不存在的行为。
 */
function stubPnpm(root: string, version = '0.9.7'): void {
  const isWin = process.platform === 'win32'
  const bin = join(root, 'bin')
  mkdirSync(bin, { recursive: true })
  const fake = join(bin, isWin ? 'pnpm.cmd' : 'pnpm')
  const script = isWin
    ? [
        '@echo off',
        `mkdir "node_modules\\dsh-ego-browser" 2>nul`,
        `echo {"name":"dsh-ego-browser","version":"${version}"}> "node_modules\\dsh-ego-browser\\package.json"`,
        // 真实 pnpm 会把依赖写进清单；用 node 改写 JSON 比 echo 可靠。
        `node -e "const f='package.json',fs=require('fs');const m=JSON.parse(fs.readFileSync(f,'utf8'));m.dependencies=m.dependencies||{};m.dependencies['dsh-ego-browser']='${version}';fs.writeFileSync(f,JSON.stringify(m,null,2))"`,
        'exit /b 0',
        '',
      ].join('\r\n')
    : [
        '#!/bin/sh',
        'mkdir -p node_modules/dsh-ego-browser',
        `echo '{"name":"dsh-ego-browser","version":"${version}"}' > node_modules/dsh-ego-browser/package.json`,
        `node -e "const f='package.json',fs=require('fs');const m=JSON.parse(fs.readFileSync(f,'utf8'));m.dependencies=m.dependencies||{};m.dependencies['dsh-ego-browser']='${version}';fs.writeFileSync(f,JSON.stringify(m,null,2))"`,
        'exit 0',
        '',
      ].join('\n')
  writeFileSync(fake, script, { mode: 0o755 })
  process.env.PATH = `${bin}${isWin ? ';' : ':'}${process.env.PATH ?? ''}`
}

describe('installCompanion success path', () => {
  let root: string
  let savedPath: string | undefined

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'ykg-install-ok-'))
    savedPath = process.env.PATH
  })

  afterEach(() => {
    if (savedPath !== undefined) process.env.PATH = savedPath
    rmSync(root, { recursive: true, force: true })
  })

  it('writes the dependency and the bundle list, then hot-loads', async () => {
    const dir = makeProfile(root, { name: 'desktop-profile', dependencies: {}, dsh: { profile: { bundles: [] } } })
    stubPnpm(root)

    const calls: Array<{ id: string; name: string; config?: Record<string, unknown> }> = []
    const loader = {
      create: async (options: { id: string; name: string; config?: Record<string, unknown> }) => {
        calls.push(options)
        return {}
      },
    }

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      loader,
      { ...process.env, DSH_HOME: root },
    )

    expect(outcome.ok).toBe(true)
    expect(outcome.version).toBe('0.9.7')

    const manifest = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')) as {
      dependencies?: Record<string, string>
      dsh?: { profile?: { bundles?: string[] } }
    }
    expect(manifest.dependencies?.['dsh-ego-browser']).toBeTruthy()
    expect(manifest.dsh?.profile?.bundles).toContain('dsh-ego-browser')

    // The hot-load is what makes this "no restart needed"; its arguments decide
    // whether the Loader and the settings panel can actually find the plugin.
    expect(calls).toHaveLength(1)
    expect(calls[0].name).toBe('dsh-ego-browser')
    expect(calls[0].id).toBe('ego-browser')
  }, 60_000)

  it('reports every step in order so the panel can show progress', async () => {
    const dir = makeProfile(root, { name: 'p', dependencies: {}, dsh: { profile: { bundles: [] } } })
    void dir
    stubPnpm(root)
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      { create: async () => ({}) },
      { ...process.env, DSH_HOME: root },
    )
    const order = outcome.steps.map((s) => s.step)
    // 顺序本身是承诺：pnpm 必须早于 manifest（否则清单改了却没装上），
    // load 必须在最后（它依赖前面的产物）。
    expect(order.indexOf('pnpm')).toBeLessThan(order.indexOf('manifest'))
    expect(order.indexOf('manifest')).toBeLessThan(order.indexOf('load'))
    expect(order[order.length - 1]).toBe('verify')
    expect(outcome.steps.every((s) => s.ok)).toBe(true)
  }, 60_000)

  it('still succeeds when the host offers no loader, but says a restart is needed', async () => {
    // 无 loader 的宿主（例如 headless）不该让安装失败 —— 清单已经写好，
    // 重启就会生效。但必须在 detail 里说清楚，否则用户会以为已经热装好了。
    makeProfile(root, { name: 'p', dependencies: {}, dsh: { profile: { bundles: [] } } })
    stubPnpm(root)
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      undefined,
      { ...process.env, DSH_HOME: root },
    )
    expect(outcome.ok).toBe(true)
    const load = outcome.steps.find((s) => s.step === 'load')!
    expect(load.ok).toBe(false)
    expect(String(load.detail)).toContain('重启')
  }, 60_000)

  it('still succeeds when the hot-load throws, and says a restart will fix it', async () => {
    // loader.create 失败只该损失"免重启"，不该让整个安装失败 ——
    // 文件与清单都已经就位了。但同样要说清楚。
    makeProfile(root, { name: 'p', dependencies: {}, dsh: { profile: { bundles: [] } } })
    stubPnpm(root)
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      {
        create: async () => {
          throw new Error('fiber refused')
        },
      },
      { ...process.env, DSH_HOME: root },
    )
    expect(outcome.ok).toBe(true)
    const load = outcome.steps.find((s) => s.step === 'load')!
    expect(load.ok).toBe(false)
    expect(String(load.detail)).toContain('重启')
    expect(String(load.detail)).toContain('fiber refused')
  }, 60_000)

  it('records the installed version from the package that pnpm left behind', async () => {
    makeProfile(root, { name: 'p', dependencies: {}, dsh: { profile: { bundles: [] } } })
    stubPnpm(root, '1.2.3')
    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      { create: async () => ({}) },
      { ...process.env, DSH_HOME: root },
    )
    // Read from node_modules, not from the requested range: what actually landed
    // is the only thing worth reporting.
    expect(outcome.version).toBe('1.2.3')
    expect(outcome.steps.find((s) => s.step === 'verify')!.detail).toContain('1.2.3')
  }, 60_000)

  it('does not run pnpm at all when everything is already in place', async () => {
    const dir = makeProfile(root, {
      name: 'p',
      dependencies: { 'dsh-ego-browser': '0.9.7' },
      dsh: { profile: { bundles: ['dsh-ego-browser'] } },
    })
    const installed = join(dir, 'node_modules', 'dsh-ego-browser')
    mkdirSync(installed, { recursive: true })
    writeFileSync(join(installed, 'package.json'), JSON.stringify({ name: 'dsh-ego-browser', version: '0.9.7' }), 'utf8')

    // A pnpm stub that would record a call if it were invoked.
    const isWin = process.platform === 'win32'
    const bin = join(root, 'bin')
    mkdirSync(bin, { recursive: true })
    const marker = join(root, 'pnpm-was-called')
    writeFileSync(
      join(bin, isWin ? 'pnpm.cmd' : 'pnpm'),
      isWin ? `@echo off\r\necho called> "${marker}"\r\nexit /b 0\r\n` : `#!/bin/sh\ntouch "${marker}"\nexit 0\n`,
      { mode: 0o755 },
    )
    process.env.PATH = `${bin}${isWin ? ';' : ':'}${savedPath ?? ''}`

    const outcome = await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'ego-browser', packageName: 'dsh-ego-browser', profile: 'desktop' },
      { create: async () => ({}) },
      { ...process.env, DSH_HOME: root },
    )
    expect(outcome.ok).toBe(true)
    expect(outcome.already).toBe(true)
    // Re-running pnpm on an already-installed package would be a slow no-op that
    // also risks touching the user's lockfile for nothing.
    expect(existsSync(marker)).toBe(false)
  }, 60_000)

  it('keeps the requested entry id and package name apart', async () => {
    // id 与 name 是两个不同的标识：entry id 是设置命名空间，name 是包名。
    // 混用会让面板找不到自己的配置段。
    makeProfile(root, { name: 'p', dependencies: {}, dsh: { profile: { bundles: [] } } })
    stubPnpm(root)
    const calls: Array<{ id: string; name: string }> = []
    await installCompanion(
      { spec: 'dsh-ego-browser@^0.9', entryId: 'custom-entry', packageName: 'dsh-ego-browser', profile: 'desktop' },
      { create: async (o: { id: string; name: string }) => { calls.push(o); return {} } },
      { ...process.env, DSH_HOME: root },
    )
    expect(calls[0].id).toBe('custom-entry')
    expect(calls[0].name).toBe('dsh-ego-browser')
  }, 60_000)
})
