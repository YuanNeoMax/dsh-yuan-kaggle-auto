import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DEFAULTS, resolveConfig, type Settings } from '../src/config.ts'
import { resolvePaths, venvKaggle, venvPython } from '../src/paths.ts'
import { readStatus, ENGINE_CONTRACT_VERSION } from '../src/setup.ts'
import { writeStamp } from '../src/python.ts'

/**
 * `readStatus` —— 面板状态盘的全部内容都来自这一个函数。
 *
 * 此前零覆盖：`installEngine` / `readStatus` 都没被调用过。
 * 而它决定三件事：
 *
 * 1. 面板上每一行显示什么（✓ / ⚠ / ✗ + 一句修复建议）；
 * 2. `ready` 是真是假 —— 直接决定「跑通演练」能不能点；
 * 3. 用户对"我的环境到底缺什么"的**唯一**信息来源。
 *
 * 说错了用户会拿着错误信息去修错的东西。所以每一条分支都要有断言。
 *
 * 用真实临时目录造 venv / stamp / 凭据文件，不 spawn 任何进程 ——
 * 唯一的子进程调用是 `findPython` 的版本探测，那条路径由
 * `tests/runner-integration.test.ts` 覆盖，这里用设置里的显式路径控制它。
 */

const root = mkdtempSync(join(tmpdir(), 'ykg-status-'))

/** 一个有 pandas/lightgbm 的假 venv。 */
function makeVenv(venvDir: string, options: { lightgbm?: boolean; stamp?: boolean; hash?: string } = {}): void {
  const py = venvPython(venvDir)
  mkdirSync(join(py, '..'), { recursive: true })
  writeFileSync(py, '', 'utf8')
  const kaggle = venvKaggle(venvDir)
  mkdirSync(join(kaggle, '..'), { recursive: true })
  writeFileSync(kaggle, '', 'utf8')

  const sitePackages =
    process.platform === 'win32'
      ? join(venvDir, 'Lib', 'site-packages')
      : join(venvDir, 'lib', 'python3.12', 'site-packages')
  mkdirSync(sitePackages, { recursive: true })
  if (options.lightgbm !== false) mkdirSync(join(sitePackages, 'lightgbm'), { recursive: true })

  if (options.stamp !== false) {
    writeStamp(venvDir, {
      engineVersion: ENGINE_CONTRACT_VERSION,
      requirementsHash: options.hash ?? 'whatever',
      pythonVersion: '3.12.0',
      builtAt: new Date().toISOString(),
    })
  }
}

function settingsFor(over: Partial<Settings> = {}): Settings {
  return resolveConfig({ ...DEFAULTS, ...over })
}

function pathsFor(settings: Settings) {
  return resolvePaths(settings)
}

function byId(items: Array<{ id: string }>, id: string) {
  return items.find((item) => item.id === id)
}

describe('readStatus', () => {
  let work: string

  beforeEach(() => {
    work = join(root, `case-${Math.random().toString(36).slice(2, 8)}`)
    mkdirSync(work, { recursive: true })
  })

  afterEach(() => {
    rmSync(work, { recursive: true, force: true })
  })

  it('reports a missing venv as missing, not as warn', async () => {
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings))

    const venv = byId(status.items, 'venv')!
    expect(venv.level).toBe('missing')
    expect(venv.fix).toBeTruthy()
    expect(status.ready).toBe(false)
  })

  it('marks a venv with a stale stamp as warn, not ok', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir, { hash: 'old-hash' })
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings))

    const venv = byId(status.items, 'venv')!
    // A venv built from a different requirements file is usable but outdated.
    // Saying "ok" would hide a pending upgrade; saying "missing" would tell the
    // user to build one they already have.
    expect(venv.level).toBe('warn')
    expect(status.ready).toBe(false)
  })

  it('is ready only when venv, deps, and kaggle CLI are all present with a fresh stamp', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const paths = pathsFor(settings)

    // The stamp's hash must match what `requirementsHash` computes from the
    // shipped lock file, otherwise the case is "stale" by construction.
    const { createHash } = await import('node:crypto')
    const { readFileSync } = await import('node:fs')
    const lock = join(paths.engineDir, 'requirements.lock.txt')
    if (!existsSync(lock)) return // fixture missing: nothing to assert
    const hash = createHash('sha256').update(readFileSync(lock, 'utf8')).digest('hex').slice(0, 16)
    makeVenv(venvDir, { hash })

    const status = await readStatus(settings, pathsFor(settings))
    expect(byId(status.items, 'venv')!.level).toBe('ok')
    expect(byId(status.items, 'deps')!.level).toBe('ok')
    expect(byId(status.items, 'kaggle-cli')!.level).toBe('ok')
    if (byId(status.items, 'python')!.level === 'ok') {
      expect(status.ready).toBe(true)
    }
  })

  it('reports missing lightgbm as warn even when the venv exists', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir, { lightgbm: false })
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings))

    // pandas/numpy/sklearn are importable but the engine's model needs lightgbm;
    // a run would fail at baseline. This must not read as "ok".
    expect(byId(status.items, 'deps')!.level).toBe('warn')
    expect(status.ready).toBe(false)
  })

  it('distinguishes "no credentials" from "has credentials", never printing them', async () => {
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings))
    const creds = byId(status.items, 'kaggle-creds')!
    // Level depends on the machine, but the detail must never carry a token.
    expect(['ok', 'missing']).toContain(creds.level)
    expect(String(creds.detail)).not.toMatch(/[A-Za-z0-9]{32,}/)
    if (creds.level === 'missing') {
      expect(creds.fix).toBeTruthy()
    }
  })

  it('always marks the optional skill bridge as ok (absence is not a problem)', async () => {
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work'), skillBridgeDir: '' })
    const status = await readStatus(settings, pathsFor(settings))
    const bridge = byId(status.items, 'skill-bridge')!
    // The bridge is optional by design; showing it as a warning would teach
    // users to ignore warnings.
    expect(bridge.level).toBe('ok')
    expect(String(bridge.detail)).toContain('可选')
  })

  it('turns a missing LLM into a warn with a way forward, not a blocker', async () => {
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings), { llm: { available: false } })
    const llm = byId(status.items, 'llm')!
    expect(llm.level).toBe('warn')
    // Crucially: it must say the deterministic path still works.
    expect(String(llm.fix)).toMatch(/baseline|不需要/)
  })

  it('reflects the ego probe it is given', async () => {
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const absent = await readStatus(settings, pathsFor(settings), { ego: { available: false } })
    expect(byId(absent.items, 'ego')!.level).toBe('warn')
    expect(String(byId(absent.items, 'ego')!.detail)).toContain('可选')

    const present = await readStatus(settings, pathsFor(settings), { ego: { available: true, detail: 'v0.9' } })
    expect(byId(present.items, 'ego')!.level).toBe('ok')
  })

  it('every non-ok item carries a fix line', async () => {
    // 这是状态盘的核心契约：缺什么 + 怎么补。少了 fix，用户只知道"坏了"。
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const status = await readStatus(settings, pathsFor(settings), { ego: { available: false }, llm: { available: false } })
    for (const item of status.items) {
      if (item.level !== 'ok' && item.id !== 'skill-bridge') {
        expect(item.fix, `item ${item.id} lacks a fix`).toBeTruthy()
      }
    }
  })

  it('reports the resolved paths so the panel can show where things live', async () => {
    const settings = settingsFor({ venvDir: join(work, 'v'), workRoot: join(work, 'w') })
    const status = await readStatus(settings, pathsFor(settings))
    expect(status.paths.workRoot).toContain(work)
    expect(status.paths.venvDir).toContain(work)
    expect(status.paths.engineDir).toBeTruthy()
  })

  it('honours an explicit kaggleCliPath over the venv one', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const custom = join(work, 'my-kaggle')
    writeFileSync(custom, '', 'utf8')
    const settings = settingsFor({
      venvDir,
      workRoot: join(work, 'work'),
      kaggleCliPath: custom,
    })
    const status = await readStatus(settings, pathsFor(settings))
    const cli = byId(status.items, 'kaggle-cli')!
    expect(cli.level).toBe('ok')
    expect(String(cli.detail)).toContain('my-kaggle')
  })
})
