import { describe, it, expect, beforeEach, vi } from 'vitest'
import { invoke, fromRun, type ToolDeps } from '../src/tools.ts'
import { DEFAULTS, resolveConfig } from '../src/config.ts'
import { resolvePaths } from '../src/paths.ts'
import { createLock } from '../src/index.ts'
import type { RunResult } from '../src/runner.ts'

/**
 * `invoke` —— 25 个工具的共同底座，此前零覆盖。
 *
 * 每个 `kaggle_*` 工具最终都走到这里。它负责四件事：
 *
 * 1. **前置检查**：没有 Python 就立刻返回 `needsSetup`，不 spawn；
 * 2. **按 slug 串行**：同一场比赛的两次调用排队，不同比赛并发；
 * 3. **超时传递**：工具的 `timeoutMs` 必须到达引擎；
 * 4. **锁释放**：异常路径也要释放，否则一次失败会永久堵住那个 slug。
 *
 * 第 4 条是重点：`try/finally` 写错的话，运行期的表现是"第一次失败之后
 * 这场比赛再也不能跑"，而且完全不报错 —— 后续调用只是静静地等锁。
 */

const settings = resolveConfig(DEFAULTS)

function makeDeps(overrides: Partial<ToolDeps> = {}): ToolDeps & {
  locks: string[]
  released: string[]
  runs: Array<{ command: string[]; options: unknown }>
} {
  const locks: string[] = []
  const released: string[] = []
  const runs: Array<{ command: string[]; options: unknown }> = []
  const deps = {
    settings: () => settings,
    paths: () => resolvePaths(settings),
    python: async () => ({ ok: true as const, python: '/fake/python' }),
    egoAvailable: () => false,
    acquire: async (key: string) => {
      locks.push(key)
      return () => released.push(key)
    },
    locks,
    released,
    runs,
    ...overrides,
  }
  return deps as unknown as ToolDeps & typeof deps
}

describe('invoke', () => {
  it('refuses to spawn when no Python is available', async () => {
    let acquired = 0
    const deps = makeDeps({
      python: async () => ({
        ok: false as const,
        reason: 'no interpreter found',
        hint: 'install Python 3.10+',
      }),
      acquire: async () => {
        acquired += 1
        return () => undefined
      },
    })

    const result = await invoke(deps, ['doctor'])
    expect(result.ok).toBe(false)
    expect(result.needsSetup).toBe(true)
    // 三条信息缺一不可：原因、怎么修、以及"先初始化"这个动作指引。
    expect(String(result.error)).toContain('no interpreter found')
    expect(String(result.error)).toContain('install Python 3.10+')
    // 没有解释器时不该白白占锁 —— 那次 acquire 会挡住别人。
    expect(acquired).toBe(0)
  })

  it('locks per slug so two calls on the same competition serialise', async () => {
    const deps = makeDeps()
    await invoke(deps, ['iterate', 'titanic', '--rounds', '1'])
    expect(deps.locks).toEqual(['slug:titanic'])
    expect(deps.released).toEqual(['slug:titanic'])
  })

  it('does not serialise two different competitions', async () => {
    const deps = makeDeps()
    await invoke(deps, ['baseline', 'titanic'])
    await invoke(deps, ['baseline', 'digit-recognizer'])
    // Different keys mean they could run concurrently; sharing one key would
    // make two competitions queue behind each other for no reason.
    expect(deps.locks).toEqual(['slug:titanic', 'slug:digit-recognizer'])
  })

  it('uses an explicit lock key when the caller needs a global one', async () => {
    const deps = makeDeps()
    // `smoke` has no slug but must not run twice at once.
    await invoke(deps, ['smoke'], { lock: 'smoke' })
    expect(deps.locks).toEqual(['smoke'])
  })

  it('takes no lock at all for a command with no slug and no explicit key', async () => {
    const deps = makeDeps()
    await invoke(deps, ['doctor'])
    // `doctor` is read-only and cheap; making it queue behind a long run would
    // block the very check people run to diagnose a stuck system.
    expect(deps.locks).toEqual([])
  })

  it('releases the lock when the engine call throws', async () => {
    // 这是本文件最重要的一条：异常路径不释放锁，会让那个 slug 永久卡死，
    // 而且后续调用完全不报错 —— 只是静静地等。
    let released = false
    const deps = makeDeps({
      acquire: async () => () => {
        released = true
      },
    })

    // Force a throw from inside the locked section by giving a python that
    // resolves but whose paths() blows up.
    const broken = makeDeps({
      acquire: deps.acquire,
      paths: () => {
        throw new Error('paths exploded')
      },
    })

    await expect(invoke(broken, ['baseline', 'titanic'])).rejects.toThrow('paths exploded')
    expect(released).toBe(true)
  })

  it('passes the caller timeout through to the engine', async () => {
    // 超时决定长任务会不会被腰斩：`kaggle_run` / `kaggle_iterate` 传 0（不限），
    // `kaggle_smoke` 传 15 分钟。传错会让迭代被默认 30 分钟上限砍掉。
    //
    // 这条必须真的检查传给 runEngine 的 options，而不是"调用成功了"——
    // 早先我写了一个 `void seen` 然后只断言 result 为 truthy，
    // 那种测试用**错误的原因**也能通过。
    const calls: Array<{ command: string[]; options: Record<string, unknown> }> = []
    vi.resetModules()
    vi.doMock('../src/runner.ts', () => ({
      runEngine: async (command: string[], _ctx: unknown, options: Record<string, unknown>) => {
        calls.push({ command, options })
        return { ok: true, value: { ok: true }, stdout: '', stderr: '', exitCode: 0 }
      },
      lastJsonValue: () => undefined,
      explainFailure: () => '',
      engineEnv: () => ({}),
    }))

    const { invoke: freshInvoke } = await import('../src/tools.ts')
    const deps = makeDeps()
    await freshInvoke(deps, ['doctor'], { timeoutMs: 1_000 })
    expect(calls).toHaveLength(1)
    expect(calls[0].options.timeoutMs).toBe(1_000)

    // And the default when the caller says nothing.
    await freshInvoke(deps, ['doctor'])
    expect(calls[1].options.timeoutMs).toBe(30 * 60 * 1000)

    vi.doUnmock('../src/runner.ts')
    vi.resetModules()
  })

  it('includes the command in the result so failures are traceable', async () => {
    const deps = makeDeps()
    const result = await invoke(deps, ['baseline', 'titanic'])
    // 失败时模型需要知道是哪条命令炸的，尤其 run 的内部分步。
    expect(String(result.command)).toContain('baseline titanic')
  })
})

describe('createLock', () => {
  it('queues callers on the same key and runs them in order', async () => {
    const lock = createLock()
    const order: string[] = []

    const first = await lock('k')
    order.push('first-acquired')

    const secondPromise = lock('k').then(() => {
      order.push('second-acquired')
    })

    // The second must not have acquired yet.
    await Promise.resolve()
    expect(order).toEqual(['first-acquired'])

    first()
    await secondPromise
    expect(order).toEqual(['first-acquired', 'second-acquired'])
  })

  it('lets different keys through immediately', async () => {
    const lock = createLock()
    const a = await lock('a')
    // A held key must not block another key.
    const b = await lock('b')
    expect(typeof b).toBe('function')
    a()
    b()
  })

  it('does not deadlock when a release is called twice', async () => {
    // 释放函数被调用两次是容易出现的 bug（finally 里再调一次）。
    // 第二次不该 panic，也不该解锁别人的等待者。
    const lock = createLock()
    const release = await lock('k')
    release()
    expect(() => release()).not.toThrow()
  })
})

describe('fromRun shape contract used by invoke', () => {
  it('yields ok:false with the exit code on a failed run', () => {
    const run: RunResult = {
      ok: false,
      stdout: 'progress lines only',
      stderr: 'boom',
      exitCode: 3,
      error: 'kauto validate 失败（exit 3）',
    }
    const result = fromRun(run, { command: 'validate titanic' })
    expect(result.ok).toBe(false)
    expect(result.exitCode).toBe(3)
    expect(String(result.command)).toContain('validate titanic')
  })
})
