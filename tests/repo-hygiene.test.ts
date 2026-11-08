import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'

/**
 * 版本库里不能有真实比赛数据。
 *
 * ## 为什么需要这条
 *
 * `kauto prepare` 会把 Kaggle 下载的 zip 落在 `engine/` 根下。
 * 跑真实比赛或端到端测试时，这些文件就出现在工作区里，而
 * `git add -A` 会把它们一并提交 —— 本发现在仓库里就躺着一个
 * `engine/titanic.zip`（353 KB），是某轮跑真实比赛时留下的。
 *
 * 后果不只是仓库变大：
 *
 * - **许可**：Kaggle 数据集各有自己的许可，随代码再分发通常不允许；
 * - **体积**：真实比赛数据动辄几百 MB，一次提交就写进历史，之后
 *   只能靠 filter-branch 清理；
 * - **误导**：后来的人会以为这是夹具，于是在代码里引用它。
 *
 * 夹具（`engine/fixtures/`）是合成数据，可以入库；下载产物不行。
 */

const root = join(import.meta.dirname, '..')

/** 仓库里所有被跟踪的路径。 */
function trackedFiles(): string[] {
  const out = execFileSync('git', ['ls-files'], {
    cwd: root,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 60_000,
  }) as string
  return out.split('\n').filter((line) => line.trim() !== '')
}

describe('repository hygiene', () => {
  const files = trackedFiles()

  it('has tracked files to inspect at all', () => {
    // 若 git 不可用或不在仓库里，上面的 lib 会抛；这里兜一层，
    // 免得空数组让下面每条断言都"通过"。
    expect(files.length).toBeGreaterThan(100)
  })

  it('tracks no Kaggle download archives at the engine root', () => {
    // `prepare` 的落点：engine/<slug>.zip。夹具不在这里。
    const archives = files.filter((f) => /^engine\/[^/]+\.zip$/i.test(f))
    expect(archives, `tracked downloads: ${archives.join(', ')}`).toEqual([])
  })

  it('tracks no raw csv at the engine root', () => {
    // 同理：engine/<slug>/train.csv 之类的解压产物也不能进库。
    // `engine/fixtures/**.csv` 是合成夹具，允许；`engine/work/**.csv`
    // 是运行产物，已在 .gitignore 里。
    const stray = files.filter((f) => /^engine\/[^/]+\.csv$/i.test(f))
    expect(stray, `tracked raw csv: ${stray.join(', ')}`).toEqual([])
  })

  it('tracks nothing under engine/work or engine/data', () => {
    // 这两个目录是可写根的一部分，内容随每次运行变化。
    const runtime = files.filter((f) => /^engine\/(work|data)\//.test(f))
    expect(runtime, `tracked runtime files: ${runtime.slice(0, 5).join(', ')}`).toEqual([])
  })

  it('tracks no python bytecode or caches', () => {
    const noise = files.filter(
      (f) => f.includes('__pycache__') || f.endsWith('.pyc') || f.includes('.pytest_cache'),
    )
    expect(noise, `tracked caches: ${noise.slice(0, 5).join(', ')}`).toEqual([])
  })

  it('keeps the synthetic fixtures, which are supposed to be tracked', () => {
    // 反面保护：别为了"清理数据"把夹具也删了 —— 离线演练依赖它们。
    expect(files).toContain('engine/fixtures/demo-comp/train.csv')
    expect(files).toContain('engine/fixtures/demo-comp/test.csv')
  })

  it('ignores future downloads so they cannot be added by accident', () => {
    // 光删掉现有文件不够：下一轮跑真实比赛又会生成。
    const { readFileSync } = require('node:fs') as typeof import('node:fs')
    const ignore = readFileSync(join(root, '.gitignore'), 'utf8')
    expect(ignore).toMatch(/engine\/\*\.zip/)
    expect(ignore).toMatch(/engine\/\*\.csv/)
  })
})
