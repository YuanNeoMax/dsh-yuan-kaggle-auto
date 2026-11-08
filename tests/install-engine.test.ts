import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DEFAULTS, resolveConfig, type Settings } from '../src/config.ts'
import { resolvePaths, venvPython, ENGINE_DIR } from '../src/paths.ts'
import { pipArgs, installEngine, ENGINE_CONTRACT_VERSION } from '../src/setup.ts'
import { writeStamp, readStamp } from '../src/python.ts'

/**
 * `installEngine` —— 唯一会创建 venv 并跑 pip 的路径，此前零覆盖。
 *
 * 它是最慢的一步（几分钟），失败场景也最多：磁盘、代理、wheel 平台不匹配。
 * 而用户看到的只有"初始化失败"，所以**哪一步失败**必须准确。
 *
 * 这里用 `vi.doMock` 拦截 `findPython` / `runStreaming`，不 spawn 真进程 ——
 * 被测逻辑是"步骤编排与失败判定"，不是 pip 本身。
 */

const root = mkdtempSync(join(tmpdir(), 'ykg-install-engine-'))

function settingsFor(over: Partial<Settings> = {}): Settings {
  return resolveConfig({ ...DEFAULTS, ...over })
}

/** 一个 venv 目录，含 python 可执行文件（可指定是否已就绪）。 */
function makeVenv(venvDir: string, options: { python?: boolean } = {}): void {
  if (options.python === false) return
  const py = venvPython(venvDir)
  mkdirSync(join(py, '..'), { recursive: true })
  writeFileSync(py, '', 'utf8')
}

describe('pipArgs', () => {
  const paths = resolvePaths(settingsFor({ venvDir: join(root, 'v'), workRoot: join(root, 'w') }))

  it('always passes --proxy on Windows, empty when unset', () => {
    // 实测：清空 HTTP_PROXY 不够，pip 会读注册表里的系统代理，
    // 而那个代理会破坏到 PyPI 的 TLS。只有命令行 --proxy 能覆盖它。
    if (process.platform !== 'win32') return
    const args = pipArgs(paths, settingsFor({ uploadProxy: '' }))
    const i = args.indexOf('--proxy')
    expect(i).toBeGreaterThan(-1)
    expect(args[i + 1]).toBe('')
  })

  it('uses the configured proxy on Windows instead of silently ignoring it', () => {
    // 这是本轮修的一个真实缺陷：原实现在 Windows 上配了代理就什么都不加，
    // 配置被静默忽略，pip 继续用会坏 TLS 的系统代理。
    if (process.platform !== 'win32') return
    const args = pipArgs(paths, settingsFor({ uploadProxy: 'http://127.0.0.1:7890' }))
    const i = args.indexOf('--proxy')
    expect(i).toBeGreaterThan(-1)
    expect(args[i + 1]).toBe('http://127.0.0.1:7890')
  })

  it('adds --proxy only when configured on POSIX', () => {
    if (process.platform === 'win32') return
    const unset = pipArgs(paths, settingsFor({ uploadProxy: '' }))
    expect(unset).not.toContain('--proxy')
    const set = pipArgs(paths, settingsFor({ uploadProxy: 'http://127.0.0.1:7890' }))
    expect(set).toContain('--proxy')
  })

  it('always installs from the shipped lock file', () => {
    const args = pipArgs(paths, settingsFor())
    expect(args).toContain('-r')
    expect(args[args.indexOf('-r') + 1]).toContain('requirements.lock.txt')
  })

  it('is non-interactive so a run can never hang on a prompt', () => {
    const args = pipArgs(paths, settingsFor())
    // 一次卡在 "Do you want to continue?" 的 pip 会看起来像死机。
    expect(args).toContain('--no-input')
    expect(args).toContain('--disable-pip-version-check')
  })
})

describe('installEngine', () => {
  let work: string

  beforeEach(() => {
    work = join(root, `case-${Math.random().toString(36).slice(2, 8)}`)
    mkdirSync(work, { recursive: true })
    vi.resetModules()
  })

  afterEach(() => {
    rmSync(work, { recursive: true, force: true })
    vi.doUnmock('../src/python.ts')
    vi.resetModules()
  })

  /** 装好 mock 后重新 import，拿到打桩版本的 installEngine。 */
  async function withMocks(options: {
    python?: { ok: true; version: string } | { ok: false; reason: string; hint: string }
    stream?: Array<{ code: number; output: string }>
  }) {
    const calls: Array<{ command: string; args: string[] }> = []
    const results = options.stream ? [...options.stream] : []

    vi.doMock('../src/python.ts', async () => {
      const actual = await vi.importActual<typeof import('../src/python.ts')>('../src/python.ts')
      return {
        ...actual,
        findPython: async () =>
          options.python ?? {
            ok: true as const,
            // 真实 findPython 返回 candidate；漏了它 setup.ts 会在
            // `found.candidate.command` 上崩 —— 说明 mock 必须覆盖
            // 被测代码实际读到的每一个字段。
            candidate: { command: 'python3', prefix: [] as string[], source: 'path' as const },
            version: '3.12.0',
          },
        runStreaming: async (command: string, args: string[]) => {
          calls.push({ command, args })
          const next = results.shift() ?? { code: 0, output: '' }
          // venv 创建成功后，真实 python 会产出可执行文件；mock 里手动补上。
          if (next.code === 0 && args.includes('venv')) {
            const dir = args[args.length - 1]
            const py = venvPython(dir)
            mkdirSync(join(py, '..'), { recursive: true })
            writeFileSync(py, '', 'utf8')
          }
          return { code: next.code, signal: null, output: next.output }
        },
      }
    })

    const mod = await import('../src/setup.ts')
    return { installEngine: mod.installEngine, calls }
  }

  it('fails early with an actionable hint when no interpreter exists', async () => {
    const { installEngine: fresh } = await withMocks({
      python: {
        ok: false,
        reason: 'no Python 3.10+',
        hint: 'install Python and set the path',
      },
    })
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const result = await fresh(settings, resolvePaths(settings))

    expect(result.ok).toBe(false)
    expect(String(result.error)).toContain('no Python 3.10+')
    // 没有解释器时 `tail` 要带安装指引 —— 否则用户只知道"失败"。
    expect(String(result.tail)).toContain('install Python')
    expect(result.steps[0].step).toBe('venv')
    expect(result.steps[0].ok).toBe(false)
  })

  it('reports which step failed, not just "setup failed"', async () => {
    // 用户看到 "创建虚拟环境失败" 时应该知道是 venv 那步，
    // 而不是去怀疑网络或依赖。
    const { installEngine: fresh } = await withMocks({
      stream: [{ code: 1, output: 'venv: error: unable to create directory' }],
    })
    const settings = settingsFor({ venvDir: join(work, 'venv'), workRoot: join(work, 'work') })
    const result = await fresh(settings, resolvePaths(settings))

    expect(result.ok).toBe(false)
    expect(String(result.error)).toContain('虚拟环境')
    const venv = result.steps.find((s) => s.step === 'venv')!
    expect(venv.ok).toBe(false)
    expect(String(venv.detail)).toContain('exited 1')
    // tail 要能定位 pip/venv 的真实报错。
    expect(String(result.tail)).toContain('unable to create directory')
  })

  it('reuses a venv whose stamp matches, without running pip again', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    // 用当前 requirements 的真实 hash，否则会被判为 stale。
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const paths = resolvePaths(settings)
    const { createHash } = await import('node:crypto')
    const { readFileSync } = await import('node:fs')
    const lock = join(paths.engineDir, 'requirements.lock.txt')
    if (!existsSync(lock)) return
    const hash = createHash('sha256').update(readFileSync(lock, 'utf8')).digest('hex').slice(0, 16)
    writeStamp(venvDir, {
      engineVersion: ENGINE_CONTRACT_VERSION,
      requirementsHash: hash,
      pythonVersion: '3.12.0',
      builtAt: new Date().toISOString(),
    })

    const { installEngine: fresh, calls } = await withMocks({})
    const result = await fresh(settings, paths)

    expect(result.ok).toBe(true)
    expect(result.reused).toBe(true)
    // 命中 stamp 就完全不该 spawn 任何进程 —— 这是"秒回"的全部实现。
    expect(calls).toHaveLength(0)
  })

  it('rebuilds when force is set even though the stamp matches', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const paths = resolvePaths(settings)
    const { createHash } = await import('node:crypto')
    const { readFileSync, existsSync: exists } = await import('node:fs')
    const lock = join(paths.engineDir, 'requirements.lock.txt')
    if (!exists(lock)) return
    const hash = createHash('sha256').update(readFileSync(lock, 'utf8')).digest('hex').slice(0, 16)
    writeStamp(venvDir, {
      engineVersion: ENGINE_CONTRACT_VERSION,
      requirementsHash: hash,
      pythonVersion: '3.12.0',
      builtAt: new Date().toISOString(),
    })

    const { installEngine: fresh, calls } = await withMocks({})
    const result = await fresh(settings, paths, { force: true })

    expect(result.reused).toBe(false)
    // force 必须真的去跑 pip，而不是又走 fast-path。
    expect(calls.length).toBeGreaterThan(0)
  })

  it('refreshes dependencies when the lock file changed', async () => {
    // requirements 变了但 stamp 还旧：venv 可复用，但依赖要重装。
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    writeStamp(venvDir, {
      engineVersion: ENGINE_CONTRACT_VERSION,
      requirementsHash: 'stale-hash',
      pythonVersion: '3.12.0',
      builtAt: new Date().toISOString(),
    })
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })

    // 队列：pip 升级 → 安装依赖 → 冒烟导入。venv 已可用，所以没有 venv 这一步。
    const { installEngine: fresh, calls } = await withMocks({
      stream: [
        { code: 0, output: 'pip upgraded' },
        { code: 0, output: 'Successfully installed' },
        { code: 0, output: 'engine-ok' },
      ],
    })
    const result = await fresh(settings, resolvePaths(settings))

    expect(result.ok).toBe(true)
    expect(result.reused).toBe(false)
    // 不该重建 venv（它已可用），但必须跑 pip。
    expect(calls.some((c) => c.args.includes('venv'))).toBe(false)
    expect(calls.some((c) => c.args.includes('pip'))).toBe(true)
    // stamp 要被刷新成新 hash，否则下次又会重装一遍。
    const stamp = readStamp(venvDir)!
    expect(stamp.requirementsHash).not.toBe('stale-hash')
  })

  it('fails the requirements step when pip exits non-zero, with its output', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    // 没有 stamp → 走"刷新依赖"路径（venv 已在，不再创建）。
    const { installEngine: fresh } = await withMocks({
      stream: [
        { code: 0, output: 'pip upgraded' },
        { code: 1, output: 'ERROR: Could not find a version that satisfies' },
      ],
    })
    const result = await fresh(settings, resolvePaths(settings))

    expect(result.ok).toBe(false)
    const req = result.steps.find((s) => s.step === 'requirements')!
    expect(req.ok).toBe(false)
    expect(String(result.tail)).toContain('Could not find a version')
  })

  it('fails the verify step when imports break despite pip succeeding', async () => {
    // pip 成功但 wheel 平台不匹配时会这样 —— 最容易被误判成"装好了"。
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const { installEngine: fresh } = await withMocks({
      stream: [
        { code: 0, output: 'pip upgraded' },
        { code: 0, output: 'Successfully installed lightgbm' },
        { code: 1, output: 'ImportError: libgomp.so.1: cannot open shared object file' },
      ],
    })
    const result = await fresh(settings, resolvePaths(settings))

    expect(result.ok).toBe(false)
    const verify = result.steps.find((s) => s.step === 'verify')!
    expect(verify.ok).toBe(false)
    expect(String(result.error)).toContain('导入')
    expect(String(result.tail)).toContain('libgomp')
  })

  it('keeps the step list ordered so the panel can render progress', async () => {
    const venvDir = join(work, 'venv')
    makeVenv(venvDir)
    const settings = settingsFor({ venvDir, workRoot: join(work, 'work') })
    const { installEngine: fresh } = await withMocks({
      stream: [
        { code: 0, output: 'pip upgraded' },
        { code: 0, output: 'installed' },
        { code: 0, output: 'engine-ok' },
      ],
    })
    const result = await fresh(settings, resolvePaths(settings))

    const order = result.steps.map((s) => s.step)
    // venv 已可用 → 跳过创建，但记录一条"复用"的 venv 步骤。
    expect(order).toEqual(['venv', 'pip', 'requirements', 'verify'])
    expect(result.steps.every((s) => s.ok)).toBe(true)
  })
})
