/**
 * scripts/smoke.mjs — prove the shipped engine answers through the host half.
 *
 * Runs the three things a fresh install must be able to do, without any
 * competition data:
 *   1. resolve a Python and read a status;
 *   2. run `kauto doctor` through the same runner the tools use;
 *   3. run `kauto list` and confirm JSON comes back.
 *
 * Exits non-zero on the first failure, printing what the engine said.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const engineDir = join(root, 'engine')

function findPython() {
  const runtimeRoot = join(process.env.DSH_HOME || join(process.env.USERPROFILE || '.', '.dsh'), 'kaggle-auto')
  const venv = join(runtimeRoot, 'venv', process.platform === 'win32' ? 'Scripts' : 'bin',
    process.platform === 'win32' ? 'python.exe' : 'python')
  const candidates = [process.env.KAUTO_PYTHON, venv, 'python3', 'python'].filter(Boolean)
  for (const candidate of candidates) {
    if (candidate.includes('venv') && !existsSync(candidate)) continue
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' })
    const text = `${probe.stdout ?? ''}${probe.stderr ?? ''}`
    if (/Python\s+3\.(\d+)/.test(text) && Number(/Python\s+3\.(\d+)/.exec(text)[1]) >= 10) return candidate
  }
  return ''
}

const python = findPython()
if (!python) {
  console.error('smoke: 没有 Python 3.10+，先安装或用 KAUTO_PYTHON 指定')
  process.exit(2)
}
console.log(`smoke → ${python}`)

const env = {
  ...process.env,
  PYTHONPATH: engineDir,
  PYTHONIOENCODING: 'utf-8',
  PYTHONUNBUFFERED: '1',
  NO_PROXY: 'api.kaggle.com,www.kaggle.com,kaggle.com',
}

let failed = 0
const run = (label, argv) => {
  // cwd must be a neutral directory, not KAUTO_ROOT: running from inside
  // another checkout would let that checkout's `kauto` package win on
  // sys.path[0] (cwd precedes PYTHONPATH), and we would be testing the wrong
  // engine. `root` is this package, whose own `kauto/` does not exist.
  const result = spawnSync(python, ['-m', 'kauto.cli', ...argv], {
    cwd: root,
    encoding: 'utf8',
    env,
    timeout: 300_000,
  })
  const out = `${result.stdout ?? ''}`.trim()
  const err = `${result.stderr ?? ''}`.trim()
  const ok = result.status === 0
  console.log(`\n[${ok ? 'ok' : 'FAIL'}] ${label}  (exit ${result.status})`)
  if (err) console.log(err.split('\n').slice(-6).join('\n'))
  if (out) console.log(out.split('\n').slice(0, 14).join('\n'))
  if (!ok) failed += 1
  return out
}

run('doctor', ['doctor'])
// `list` prints TSV by default (grep-friendly for the standalone CLI); the
// plugin's tool always asks for JSON, so the smoke test exercises that path.
const listing = run('list --format json', ['list', '--search', 'titanic', '--limit', '3', '--format', 'json'])
let parsed = false
try {
  const value = JSON.parse(listing)
  parsed = Array.isArray(value) && value.length > 0
} catch {
  parsed = false
}
if (!parsed) {
  console.log('\n[FAIL] list --format json 没有返回可解析的数组')
  failed += 1
} else {
  console.log(`        → 解析出 ${JSON.parse(listing).length} 条比赛`)
}

console.log(`\n${failed === 0 ? 'smoke 通过' : `smoke 失败（${failed} 项）`}`)
process.exit(failed === 0 ? 0 : 1)
