import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pythonCandidates, MIN_PYTHON } from '../src/python.ts'
// `venvPython` / `venvKaggle` 定义在 paths.ts；python.ts 只是内部引用它们，
// 没有 re-export。从错的地方导入不会报错，只会在调用时得到
// "is not a function" —— 而那已经是运行时了。
import { venvPython, venvKaggle } from '../src/paths.ts'

/**
 * `findPython` 的候选顺序 —— 一份**契约**，而不是实现细节。
 *
 * 顺序决定"用户填的设置到底算不算数"。四个来源里任何一个排错位置，
 * 表现出来都是"我配了但没生效"，而用户没有办法自己诊断：
 *
 * 1. **显式 `pythonPath`** 必须最优先 —— 用户填它就是为了覆盖一切自动判断。
 *    如果它排到 venv 后面，用户会发现"填了也没用"，因为 venv 已经存在。
 * 2. **`KAUTO_PYTHON` 环境变量** 次之 —— CI 与临时调试用它，
 *    它应该盖过自动探测，但不该盖过用户在界面上的显式选择。
 * 3. **插件自己的 venv** 再次 —— 它是唯一装了 pandas/lightgbm 的那个，
 *    所以要比系统 PATH 上的 `python3` 优先。反过来会让插件用一个
 *    没有依赖的解释器，然后在 baseline 阶段报 `ModuleNotFoundError`。
 * 4. **PATH** 兜底。
 *
 * 这些规则没有一条是"随便哪个都行"，所以必须有测试钉住。
 */

// `root` 必须是每个用例重建的：`afterEach` 会删掉它，
// 若写成模块级常量，第二个用例起就在往一个已删除的目录里写文件 ——
// 表现为"文件创建成功了但 existsSync 为假"，很难看懂。
let root = ''

function makeVenv(dir: string, platform: NodeJS.Platform = process.platform): string {
  // 平台必须与断言用的 platform 一致：`pythonCandidates` 用 `venvPython(dir, platform)`
  // 判断存在性，若这里按当前平台写文件、断言按 'linux' 查，就会得到"文件在但看不到"。
  const py = venvPython(dir, platform)
  mkdirSync(join(py, '..'), { recursive: true })
  writeFileSync(py, '', 'utf8')
  return py
}

describe('pythonCandidates ordering', () => {
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'ykg-candidates-'))
  })

  afterEach(() => {
    rmSync(root, { recursive: true, force: true })
  })

  it('puts the explicit setting first', () => {
    // 用户填了它就期待它生效，哪怕 venv 已经存在。
    const venvDir = join(root, 'venv-1')
    makeVenv(venvDir, 'linux')
    const list = pythonCandidates(
      { pythonPath: '/custom/python', venvDir },
      { KAUTO_PYTHON: '/env/python' },
      'linux',
    )
    expect(list[0]).toEqual({ command: '/custom/python', prefix: [], source: 'setting' })
  })

  it('puts the environment variable before the venv', () => {
    const venvDir = join(root, 'venv-2')
    makeVenv(venvDir, 'linux')
    const list = pythonCandidates(
      { pythonPath: '', venvDir },
      { KAUTO_PYTHON: '/env/python' },
      'linux',
    )
    expect(list[0].source).toBe('env')
    expect(list[1].source).toBe('venv')
  })

  it('puts the venv before any PATH fallback', () => {
    // 只有 venv 里装了 pandas/lightgbm；用 PATH 上的裸 python3 会在
    // baseline 阶段炸在 ModuleNotFoundError 上，而那时用户已经等了十分钟。
    const venvDir = join(root, 'venv-3')
    makeVenv(venvDir, 'linux')
    const list = pythonCandidates({ pythonPath: '', venvDir }, {}, 'linux')
    const venvIndex = list.findIndex((c) => c.source === 'venv')
    const pathIndex = list.findIndex((c) => c.source === 'path')
    expect(venvIndex).toBeGreaterThanOrEqual(0)
    expect(venvIndex).toBeLessThan(pathIndex)
  })

  it('omits the venv when it does not exist on disk', () => {
    // 不存在就别浪费时间探测它 —— probeVersion 会对每个候选 spawn 一次。
    const list = pythonCandidates({ pythonPath: '', venvDir: join(root, 'nope') }, {}, 'linux')
    expect(list.some((c) => c.source === 'venv')).toBe(false)
  })

  it('omits the explicit and env entries when they are blank', () => {
    // 空串不能变成一个"命令名为空"的候选，那会让 probeVersion 抛错。
    const list = pythonCandidates({ pythonPath: '   ', venvDir: join(root, 'nope') }, { KAUTO_PYTHON: '  ' }, 'linux')
    expect(list.every((c) => c.command.trim() !== '')).toBe(true)
    expect(list.some((c) => c.source === 'setting')).toBe(false)
    expect(list.some((c) => c.source === 'env')).toBe(false)
  })

  it('adds the py -3 launcher first on Windows only', () => {
    const win = pythonCandidates({ pythonPath: '', venvDir: join(root, 'nope') }, {}, 'win32')
    const posix = pythonCandidates({ pythonPath: '', venvDir: join(root, 'nope') }, {}, 'linux')

    const py = win.find((c) => c.command === 'py')
    // Windows 上 `py -3` 是官方推荐的启动方式；它比裸 `python` 更可靠，
    // 因为裸 `python` 可能指向 Store 里的占位符。
    expect(py).toEqual({ command: 'py', prefix: ['-3'], source: 'path' })
    expect(posix.some((c) => c.command === 'py')).toBe(false)
    // 两种平台都要有 python3 / python 兜底。
    for (const list of [win, posix]) {
      expect(list.some((c) => c.command === 'python3')).toBe(true)
      expect(list.some((c) => c.command === 'python')).toBe(true)
    }
  })

  it('keeps python3 before python on every platform', () => {
    // `python` 在部分发行版上是 Python 2 的别名；先试 python3 才不会
    // 拿一个 2.x 然后因为版本门槛逐个失败。
    for (const platform of ['linux', 'win32', 'darwin'] as const) {
      const list = pythonCandidates({ pythonPath: '', venvDir: join(root, 'nope') }, {}, platform)
      const i3 = list.findIndex((c) => c.command === 'python3')
      const ip = list.findIndex((c) => c.command === 'python')
      expect(i3).toBeLessThan(ip)
    }
  })

  it('uses the platform-correct venv python path', () => {
    // venvPython 是唯一知道 Scripts/ 与 bin/ 差异的地方；用错会让
    // "venv 存在"这个判断永远为假。
    //
    // 期望值用 join 构造，不写字面量：Windows 上 `join` 会用 `\`，
    // 写死 `/v/Scripts/python.exe` 会在本机失败、在作者 mac 上通过。
    const winPy = venvPython('/v', 'win32')
    const posixPy = venvPython('/v', 'linux')
    const winKaggle = venvKaggle('/v', 'win32')
    const posixKaggle = venvKaggle('/v', 'linux')

    expect(winPy).toBe(join('/v', 'Scripts', 'python.exe'))
    expect(posixPy).toBe(join('/v', 'bin', 'python'))
    expect(winKaggle).toBe(join('/v', 'Scripts', 'kaggle.exe'))
    expect(posixKaggle).toBe(join('/v', 'bin', 'kaggle'))

    // 布局关键词必须真的不同，否则说明 platform 分支没生效。
    expect(winPy).toContain('Scripts')
    expect(posixPy).toContain('bin')
    expect(winPy.endsWith('.exe')).toBe(true)
    expect(posixPy.endsWith('.exe')).toBe(false)
  })

  it('declares a minimum Python the engine can actually run on', () => {
    // 引擎用了 `str | None` 注解（PEP 604），3.10 起才有效。
    // 门槛写低了会在 import 阶段崩，写高了会拒绝可用的解释器。
    expect(MIN_PYTHON.major).toBe(3)
    expect(MIN_PYTHON.minor).toBe(10)
  })
})
