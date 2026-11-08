import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  readStamp,
  writeStamp,
  STAMP_FILENAME,
  type VenvStamp,
} from '../src/python.ts'
import { venvPython } from '../src/paths.ts'
import { ENGINE_CONTRACT_VERSION } from '../src/setup.ts'

/**
 * venv 的 stamp 是"要不要重装依赖"的唯一判据。
 *
 * ## 它决定的用户体验
 *
 * 用户每次升级插件后都会点「初始化环境」。stamp 判断错误会走向两个极端：
 *
 * - **判得过严**：无谓重跑几分钟的 pip，用户以为插件坏了或很慢；
 * - **判得过宽**：跳过必要的依赖更新，然后在 baseline 阶段炸在
 *   `ModuleNotFoundError` —— 而那时已经等了十分钟。
 *
 * ## 三个字段各自回答一个问题
 *
 * | 字段 | 回答 | 不匹配时的动作 |
 * |---|---|---|
 * | `requirementsHash` | 依赖清单变了吗 | 重装依赖 |
 * | `engineVersion` | 引擎布局变了吗 | 重装依赖 |
 * | `pythonVersion` | 解释器换了吗 | **重建 venv** |
 *
 * 第三条与另外两条的区别很重要：解释器换了，旧的 venv 里的二进制
 * （lightgbm 的 .so / .pyd）与新的解释器 ABI 不匹配，光重装依赖不够。
 */

let root = ''

function freshVenv(): string {
  const dir = join(root, `venv-${Math.random().toString(36).slice(2, 8)}`)
  mkdirSync(join(venvPython(dir), '..'), { recursive: true })
  writeFileSync(venvPython(dir), '', 'utf8')
  return dir
}

function stampOf(over: Partial<VenvStamp> = {}): VenvStamp {
  return {
    engineVersion: ENGINE_CONTRACT_VERSION,
    requirementsHash: 'hash-a',
    pythonVersion: '3.12.0',
    builtAt: new Date().toISOString(),
    ...over,
  }
}

describe('stamp round-trip', () => {
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'ykg-stamp-'))
  })

  afterEach(() => {
    rmSync(root, { recursive: true, force: true })
  })

  it('writes and reads back every field', () => {
    const venvDir = freshVenv()
    const stamp = stampOf({ requirementsHash: 'abc123' })
    writeStamp(venvDir, stamp)
    const back = readStamp(venvDir)!
    expect(back).toEqual(stamp)
  })

  it('creates the venv directory when it is missing', () => {
    // 首次安装时 venv 还不存在；writeStamp 不该因此失败。
    const venvDir = join(root, 'not-yet')
    expect(existsSync(venvDir)).toBe(false)
    writeStamp(venvDir, stampOf())
    expect(existsSync(join(venvDir, STAMP_FILENAME))).toBe(true)
  })

  it('returns undefined when no stamp exists', () => {
    const venvDir = freshVenv()
    expect(readStamp(venvDir)).toBeUndefined()
  })

  it('treats a corrupt stamp as absent instead of throwing', () => {
    // 一个半写入的文件（磁盘满、进程被杀）不该让整个初始化崩掉 ——
    // "读不出来" 与 "没有" 对调用方的正确动作是一样的：重装依赖。
    const venvDir = freshVenv()
    writeFileSync(join(venvDir, STAMP_FILENAME), '{ this is not json', 'utf8')
    expect(() => readStamp(venvDir)).not.toThrow()
    expect(readStamp(venvDir)).toBeUndefined()
  })

  it('rejects a stamp that lacks requirementsHash', () => {
    // 没有 hash 就无法判断依赖是否过期，等于没有 stamp。
    const venvDir = freshVenv()
    writeFileSync(join(venvDir, STAMP_FILENAME), JSON.stringify({ engineVersion: '1' }), 'utf8')
    expect(readStamp(venvDir)).toBeUndefined()
  })

  it('rejects a stamp whose requirementsHash is not a string', () => {
    const venvDir = freshVenv()
    writeFileSync(join(venvDir, STAMP_FILENAME), JSON.stringify({ requirementsHash: 42 }), 'utf8')
    expect(readStamp(venvDir)).toBeUndefined()
  })

  it('writes human-readable JSON with a trailing newline', () => {
    // 用户可能手工打开这个文件来诊断；一行压缩 JSON 没法看。
    // 尾换行让 git diff / 编辑器行为正常。
    const venvDir = freshVenv()
    writeStamp(venvDir, stampOf())
    const raw = readFileSync(join(venvDir, STAMP_FILENAME), 'utf8')
    expect(raw.endsWith('\n')).toBe(true)
    expect(raw.split('\n').length).toBeGreaterThan(2)
    expect(raw).toContain('requirementsHash')
  })
})

describe('stamp is the venv-reuse decision', () => {
  /** 复刻 installEngine 的 fast-path 条件，验证判据本身。 */
  function shouldReuse(
    stamp: VenvStamp | undefined,
    options: { wantHash: string; engineVersion: string; pythonVersion: string; force?: boolean },
  ): boolean {
    return Boolean(
      stamp &&
      !options.force &&
      stamp.requirementsHash === options.wantHash &&
      stamp.engineVersion === options.engineVersion &&
      stamp.pythonVersion === options.pythonVersion,
    )
  }

  const want = {
    wantHash: 'hash-a',
    engineVersion: ENGINE_CONTRACT_VERSION,
    pythonVersion: '3.12.0',
  }

  it('reuses when everything matches', () => {
    expect(shouldReuse(stampOf(), want)).toBe(true)
  })

  it('refreshes when the dependency list changed', () => {
    // 用户升级插件，requirements 变了 —— 必须重装依赖。
    expect(shouldReuse(stampOf({ requirementsHash: 'hash-old' }), want)).toBe(false)
  })

  it('refreshes when the engine contract version changed', () => {
    // 引擎布局变了（新增模块、改了入口），旧依赖组合可能不成立。
    expect(shouldReuse(stampOf({ engineVersion: '0' }), want)).toBe(false)
  })

  it('rebuilds when the interpreter changed', () => {
    // 重中之重：解释器换了，旧 venv 里的二进制与新 ABI 不匹配，
    // 只重装依赖不够，必须丢弃整个 venv。
    expect(shouldReuse(stampOf({ pythonVersion: '3.11.0' }), want)).toBe(false)
  })

  it('honours force even when the stamp matches', () => {
    // 「强制重建」是用户对可疑状态的唯一手段，必须能压过 stamp。
    expect(shouldReuse(stampOf(), { ...want, force: true })).toBe(false)
  })

  it('refreshes when there is no stamp at all', () => {
    expect(shouldReuse(undefined, want)).toBe(false)
  })
})

describe('readStatus treats an outdated stamp as warn, not missing', () => {
  /** 复刻 readStatus 里 stampFresh 的计算。 */
  function stampFresh(stamp: VenvStamp | undefined, wantHash: string): boolean {
    return Boolean(stamp && stamp.requirementsHash === wantHash && stamp.engineVersion === ENGINE_CONTRACT_VERSION)
  }

  it('is fresh when hash and contract version match', () => {
    expect(stampFresh(stampOf(), 'hash-a')).toBe(true)
  })

  it('is stale when the hash differs', () => {
    expect(stampFresh(stampOf({ requirementsHash: 'old' }), 'hash-a')).toBe(false)
  })

  it('is stale when the contract version differs', () => {
    expect(stampFresh(stampOf({ engineVersion: '0' }), 'hash-a')).toBe(false)
  })

  it('is stale when there is no stamp', () => {
    // venv 目录存在但没有 stamp（用户手工建过、或旧版本留下的）：
    // 该报 warn 并提示刷新，而不是当成"已就绪"。
    expect(stampFresh(undefined, 'hash-a')).toBe(false)
  })

  it('does not consider pythonVersion in the status probe', () => {
    // 状态盘不检查解释器版本：它变了的代价是重建，而那时用户会被
    // 初始化流程直接拦下（fast-path 不通过），不需要额外一条警告。
    expect(stampFresh(stampOf({ pythonVersion: '3.11.0' }), 'hash-a')).toBe(true)
  })
})

describe('STAMP_FILENAME', () => {
  it('is hidden (dot-prefixed) so it does not clutter the venv listing', () => {
    expect(STAMP_FILENAME.startsWith('.')).toBe(true)
  })

  it('ends with .json so editors and tools treat it as data', () => {
    expect(STAMP_FILENAME.endsWith('.json')).toBe(true)
  })
})
