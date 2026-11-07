import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * 两处"说了和做的不一样"的缺陷，都在本轮修掉。
 *
 * 这类缺陷的共同点：代码能跑、测试能过、用户看到的是**描述**，
 * 而描述与实现不符时，没有任何东西会报错。
 */

const root = join(import.meta.dirname, '..')
const tools = readFileSync(join(root, 'src', 'tools.ts'), 'utf8')
const index = readFileSync(join(root, 'src', 'index.ts'), 'utf8')
const cli = readFileSync(join(root, 'engine', 'kauto', 'cli.py'), 'utf8')

/** 取一个工具定义块（从 `name: 'kaggle_x'` 到下一个 `make({`）。 */
function toolBlock(name: string): string {
  const start = tools.indexOf(`name: '${name}'`)
  if (start < 0) return ''
  const end = tools.indexOf('  make({', start)
  return end < 0 ? tools.slice(start) : tools.slice(start, end)
}

/** 引擎某个子命令的 `--opt` 默认值。 */
function engineDefault(command: string, option: string): number | undefined {
  const start = cli.indexOf(`add_parser("${command}"`)
  if (start < 0) return undefined
  const end = cli.indexOf('set_defaults', start)
  const block = cli.slice(start, end < 0 ? undefined : end)
  const m = new RegExp(`"${option}"[^)]*default=(\\d+)`).exec(block)
  return m ? Number(m[1]) : undefined
}

describe('kaggle_receipt 的等待上限描述必须与引擎一致', () => {
  it('引擎的 --wait-timeout 默认是 900 秒', () => {
    // 如果这条变了，下面那条"描述里不能出现错误数字"也要跟着改。
    expect(engineDefault('receipt', '--wait-timeout')).toBe(900)
  })

  it('工具描述不再声称 default 120（实际是 900）', () => {
    const block = toolBlock('kaggle_receipt')
    expect(block).not.toBe('')
    // 曾经的写法：'Poll ceiling in seconds (default 120).'
    // 用户按 120 秒规划等待，实际最长 15 分钟 —— 7.5 倍的误述。
    expect(block).not.toMatch(/default\s*120/i)
  })

  it('工具描述点名真实默认值', () => {
    const block = toolBlock('kaggle_receipt')
    expect(block).toMatch(/900/)
  })

  it('spawn 有上限，不是 timeoutMs: 0', () => {
    const block = toolBlock('kaggle_receipt')
    // timeoutMs: 0 意味着外层完全不管，一次卡住的轮询会把工具调用钉死。
    expect(block).not.toMatch(/timeoutMs:\s*0\s*[,}]/)
    expect(block).toMatch(/timeoutMs:\s*ceiling|const ceiling/)
  })

  it('下限保护：不接受小于 10 秒的等待', () => {
    const block = toolBlock('kaggle_receipt')
    expect(block).toMatch(/Math\.max\(10,/)
  })
})

describe('演练的锁在面板与工具之间共享', () => {
  it('工具用 smoke 作为锁键', () => {
    const block = toolBlock('kaggle_smoke')
    expect(block).toMatch(/lock:\s*'smoke'/)
  })

  it('面板路径也取同一把锁', () => {
    // bridge.smoke 直接调 runEngine，绕过了 tools.ts 的 invoke ——
    // 不显式加锁时，面板点一次 + 模型调一次会同时跑两个演练。
    // 每次演练训练 3 次 LightGBM，翻倍开销而信息量为零。
    expect(index).toMatch(/smokeLock\(\)/)
    expect(index).toMatch(/const release = await bridge\.smokeLock\(\)/)
  })

  it('锁一定被释放（try/finally）', () => {
    const start = index.indexOf('smoke: async')
    const block = index.slice(start, index.indexOf('\n    },', start))
    expect(block).toMatch(/finally\s*\{/)
    expect(block).toMatch(/release\(\)/)
  })

  it('只有一个锁表实例', () => {
    // 两个 createLock() 会得到两张独立的队列，锁就失去意义。
    // 注意排除函数定义那一行（`function createLock():` 也含 `createLock()`）。
    const calls = index
      .split('\n')
      .filter((line) => /createLock\(\)/.test(line) && !/function\s+createLock/.test(line))
    expect(calls.length).toBe(1)
  })
})
