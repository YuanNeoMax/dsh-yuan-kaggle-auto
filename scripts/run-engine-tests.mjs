/**
 * scripts/run-engine-tests.mjs — run the Python engine's 66-case suite.
 *
 * Picks the interpreter the plugin would use (venv first, then PATH), so the
 * same command works on a developer machine and in CI. Fails loudly with the
 * install hint when no ≥3.10 Python exists.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const engineDir = join(root, 'engine')

const candidates = [
  process.env.KAUTO_PYTHON,
  join(process.env.DSH_HOME || join(process.env.USERPROFILE || process.env.HOME || '.', '.dsh'), 'kaggle-auto', 'venv',
    process.platform === 'win32' ? 'Scripts' : 'bin',
    process.platform === 'win32' ? 'python.exe' : 'python'),
  // A checkout-local venv is the common developer setup and the one that
  // actually has the engine dependencies; without it the probe falls through
  // to a bare `python3` that imports pandas and dies before any test runs.
  join(root, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin',
    process.platform === 'win32' ? 'python.exe' : 'python'),
  'python3',
  'python',
].filter(Boolean)

/**
 * Pick an interpreter that can actually import the engine's dependencies.
 *
 * Version alone is not enough: a bare `python3` from PATH answers 3.12 happily
 * and then dies on `import pandas`, turning the whole suite into 66 import
 * failures that look like code regressions. The probe therefore imports the
 * heavy modules too, and only the interpreter that passes runs the tests.
 */
function pickPython() {
  for (const candidate of candidates) {
    const isPath = candidate.includes('/') || candidate.includes('\\')
    if (isPath && !existsSync(candidate)) continue
    const probe = spawnSync(
      candidate,
      ['-c', 'import sys,pandas,numpy,sklearn;print("%d.%d"%sys.version_info[:2])'],
      { encoding: 'utf8' },
    )
    if (probe.status !== 0) continue
    const match = /(\d+)\.(\d+)/.exec(`${probe.stdout ?? ''}`.trim())
    if (!match) continue
    const major = Number(match[1])
    const minor = Number(match[2])
    if (major < 3 || (major === 3 && minor < 10)) continue
    return candidate
  }
  return ''
}

const python = pickPython()

if (!python) {
  console.error(
    '找不到能导入 pandas/numpy/scikit-learn 的 Python 3.10+。\n' +
      '处理：先跑一次引擎初始化（会在 <DSH_HOME>/kaggle-auto/venv 安装依赖），' +
      '或用 KAUTO_PYTHON 指向一个装好依赖的解释器。',
  )
  process.exit(2)
}

console.log(`engine tests → ${python}`)
const result = spawnSync(python, [join(engineDir, 'run_all_tests.py')], {
  cwd: engineDir,
  stdio: 'inherit',
  env: {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: engineDir,
    NO_PROXY: 'api.kaggle.com,www.kaggle.com,kaggle.com',
  },
})
process.exit(result.status ?? 1)
