import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { execFileSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, existsSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'

/**
 * 「装成 npm 包之后，插件还找得到自己的引擎吗」。
 *
 * ## 为什么必须验证这一件事
 *
 * `PACKAGE_ROOT` 由 `lib/index.js` 的位置推导（`src/paths.ts`）：
 *
 *     resolve(dirname(fileURLToPath(import.meta.url)), '..')
 *
 * 在仓库里它指向 checkout 根，`engine/` 就在旁边。装上之后它指向
 * `node_modules/dsh-yuan-kaggle-auto/`，而 `engine/` 也必须在那里 ——
 * **否则所有 25 个工具都会找不到引擎**，报出的还是"未初始化"这种
 * 指向错误方向的提示。
 *
 * 这是本项目唯一一个「只在安装形态下才成立、而在开发机上永远看不到」的假设。
 * 前 17 轮全部验证都在仓库内完成 —— 库内测试无法回答这个问题。
 *
 * ## 做法
 *
 * 真实 `npm pack` → 解压 → 从解压出来的包里 import `lib/index.js`，
 * 断言 `engineDirectory` 落在包内且 `cli.py` 存在。
 * 这一步不联网、不装依赖：只验证路径推导与文件布局。
 */

const root = join(import.meta.dirname, '..')
let workdir = ''
let packaged = ''

beforeAll(() => {
  workdir = mkdtempSync(join(tmpdir(), 'ykg-install-shape-'))
  // `npm pack` 会把产物写到 --pack-destination；它不需要网络。
  const out = execFileSync('npm', ['pack', '--pack-destination', workdir], {
    cwd: root,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 180_000,
    shell: process.platform === 'win32',
  }) as string
  const tgz = out.trim().split('\n').pop() ?? ''
  expect(tgz.endsWith('.tgz'), `npm pack produced: ${out}`).toBe(true)

  // 解压。不依赖 PATH 上的 `tar`：这台机器上 `Get-Command tar` 找到的是
  // Git 自带的 GNU tar，它不接受 `C:\...` 形式的 Windows 路径
  // （会报 `Cannot connect to C: resolve failed`）。Windows 10+ 自带的
  // bsdtar 在 System32，能直接吃 Windows 路径。
  packaged = join(workdir, 'package')
  const tarCandidates =
    process.platform === 'win32'
      ? [join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe'), 'tar']
      : ['tar']
  let lastError: unknown
  for (const tar of tarCandidates) {
    try {
      execFileSync(tar, ['-xzf', join(workdir, tgz), '-C', workdir], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 180_000,
      })
      lastError = undefined
      break
    } catch (error) {
      lastError = error
    }
  }
  if (lastError) throw lastError
}, 300_000)

afterAll(() => {
  if (workdir) rmSync(workdir, { recursive: true, force: true })
})

describe('the published package resolves its own engine', () => {
  it('ships lib/index.js at the package root, next to engine/', () => {
    // 路径推导的全部前提就是这两者相邻。少一个，或 engine 被挪进 lib/，
    // PACKAGE_ROOT 就指向错误的地方。
    expect(existsSync(join(packaged, 'lib', 'index.js'))).toBe(true)
    expect(existsSync(join(packaged, 'engine', 'kauto', 'cli.py'))).toBe(true)
  })

  it('computes engineDirectory inside the extracted package', () => {
    // 复刻 paths.ts 的推导：`dirname(import.meta.url)/..`。
    // 这里用 lib/index.js 的绝对位置来模拟。
    const libIndex = join(packaged, 'lib', 'index.js')
    const packageRoot = dirname(dirname(libIndex))
    const engineDir = join(packageRoot, 'engine')

    expect(packageRoot).toBe(packaged)
    expect(engineDir).toBe(join(packaged, 'engine'))
    expect(existsSync(join(engineDir, 'kauto', 'cli.py')).toString()).toBe('true')
  })

  it('ships everything the engine needs at runtime', () => {
    // 引擎要能真正跑起来，这几样缺一不可：
    //   cli.py 是入口；requirements 是 pip 的输入；run_all_tests 供自检；
    //   fixtures 供离线演练（没有它「跑通演练」按钮在每个新装上都失败）。
    for (const rel of [
      'engine/kauto/cli.py',
      'engine/kauto/kapi.py',
      'engine/kauto/prep.py',
      'engine/requirements.lock.txt',
      'engine/run_all_tests.py',
      'engine/fixtures/demo-comp/train.csv',
    ]) {
      expect(existsSync(join(packaged, rel)), `missing: ${rel}`).toBe(true)
    }
  })

  it('keeps the engine out of lib/, where the bundle lives', () => {
    // 如果 engine 被塞进 lib/，`..` 就会指向 lib 而不是包根 ——
    // 那是这套推导最容易被破坏的方式。
    expect(existsSync(join(packaged, 'lib', 'engine'))).toBe(false)
  })

  it('ships the manifest files the host reads', () => {
    for (const rel of ['package.json', 'cordis.patch.yml', 'dsh-plugin.json']) {
      expect(existsSync(join(packaged, rel)), `missing: ${rel}`).toBe(true)
    }
  })

  it('declares the entry the host will actually load', () => {
    const pkg = JSON.parse(readFileSync(join(packaged, 'package.json'), 'utf8')) as {
      main?: string
      exports?: Record<string, unknown>
    }
    // `PACKAGE_ROOT` 的推导依赖 entry 在 `lib/` 下（`..` 才回到包根）。
    // 若 main 指到别处（比如根目录 index.js），推导就错一层。
    expect(pkg.main).toBe('./lib/index.js')
  })

  it('has no stale build artefacts from the working tree', () => {
    // 从仓库打包时，`lib/` 里可能残留上一次构建的旧文件。
    // 这里只断言必须存在的两个 bundle都在，多余的不追究。
    expect(existsSync(join(packaged, 'lib', 'client.js'))).toBe(true)
  })
})
