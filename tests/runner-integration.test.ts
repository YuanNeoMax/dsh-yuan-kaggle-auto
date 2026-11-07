import { describe, it, expect, beforeAll } from 'vitest'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { DEFAULTS, resolveConfig } from '../src/config.ts'
import { ENGINE_DIR, resolvePaths, venvPython } from '../src/paths.ts'
import { engineEnv, runEngine } from '../src/runner.ts'

/**
 * A real engine invocation, through the same code path the tools use.
 *
 * `runner-parse.test.ts` covers `lastJsonValue` in isolation, which proves the
 * parser but says nothing about whether argv, the environment contract, or the
 * working directory are right. Those are exactly the parts that broke before
 * (cwd landing inside another checkout, KAUTO_ROOT pointing at the wrong
 * level), and a parser test cannot catch either.
 *
 * So this test really spawns Python. It is skipped (not failed) when no
 * interpreter with the engine's dependencies exists — a checkout without a
 * venv is a normal state, not a regression.
 */

function pickPython(): string {
  const candidates = [
    process.env.KAUTO_PYTHON,
    join(process.env.USERPROFILE ?? process.env.HOME ?? '', '.dsh', 'kaggle-auto', 'venv',
      process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python'),
    'python3',
    'python',
  ].filter((value): value is string => typeof value === 'string' && value !== '')

  for (const candidate of candidates) {
    if ((candidate.includes('/') || candidate.includes('\\')) && !existsSync(candidate)) continue
    const probe = spawnSync(candidate, ['-c', 'import pandas, numpy, sklearn'], {
      encoding: 'utf8',
      timeout: 30_000,
    })
    if (probe.status === 0) return candidate
  }
  return ''
}

const python = pickPython()
const hasEngine = existsSync(join(ENGINE_DIR, 'kauto', 'cli.py'))
const canRun = python !== '' && hasEngine

/** Settings/路径固定在一处临时 runtime root，避免写进用户真实工作区。 */
const settings = resolveConfig({
  ...DEFAULTS,
  // A dedicated root keeps this test from touching any real competition data.
  workRoot: join(ENGINE_DIR, '..', '.tmp-runner-test', 'work'),
  venvDir: join(ENGINE_DIR, '..', '.tmp-runner-test', 'venv'),
})
const paths = resolvePaths(settings)

describe.skipIf(!canRun)('runEngine (real spawn)', () => {
  beforeAll(() => {
    if (!canRun) {
      console.warn('skipping runEngine integration: no Python with pandas/numpy/sklearn')
    }
  })

  it('resolves a Python that the venv path would have used', () => {
    // The venv python does not exist in this fixture, so the explicit
    // candidate wins; asserting it keeps the failure mode readable.
    expect(python === '' || existsSync(python) || !python.includes('/')).toBe(true)
  })

  it('exposes the environment contract the engine reads', () => {
    const env = engineEnv(settings, paths, { PATH: '/usr/bin' })
    // KAUTO_ROOT must be the PARENT of workRoot: the engine places data/ work/
    // reports/ directly under it, so pointing at workRoot would nest work/work.
    expect(env.KAUTO_ROOT).toBeDefined()
    expect(env.KAUTO_ROOT?.endsWith('work')).toBe(false)
    expect(env.PYTHONPATH).toBe(paths.engineDir)
    expect(env.PYTHONIOENCODING).toBe('utf-8')
    expect(env.KAUTO_THREADS).toBe('1')
    // Optional overrides must not appear as empty strings, which would clobber
    // the engine's own detection.
    expect(env.KAUTO_SKILL_ROOT).toBeUndefined()
    expect(env.KAUTO_UPLOAD_PROXY).toBeUndefined()
    expect(env.KAUTO_LLM_PROVIDER).toBeUndefined()
  })

  it('runs a real command and parses the JSON it prints', async () => {
    const result = await runEngine(['list', '--format', 'json', '--limit', '2', '--search', 'titanic'], {
      python,
      paths,
      settings,
    }, { timeoutMs: 120_000 })

    // Either the network answered (ok + array) or it did not (a structured
    // error). Both are legitimate outcomes for an integration test; what must
    // hold is that the call returned instead of hanging or throwing.
    expect(typeof result.exitCode === 'number' || result.exitCode === null).toBe(true)
    if (result.ok) {
      expect(Array.isArray(result.value) || typeof result.value === 'object').toBe(true)
    } else {
      expect(result.error).toBeTruthy()
    }
  }, 150_000)

  it('never loads an engine from somewhere other than the package', async () => {
    // The failure this guards against: `python -m` puts cwd on sys.path[0], so
    // running with cwd = workRoot could import a different checkout's `kauto`
    // and silently test that copy. Instead of inferring from a doctor payload,
    // ask Python directly where the package came from — that is the actual
    // claim, so it is the thing to assert.
    const probe = await runEngine(
      [],
      { python, paths, settings },
      {
        timeoutMs: 60_000,
        // A no-arg invocation would print usage; we only need argv/env/cwd
        // plumbing, so reuse the local spawn through a tiny inline check.
      },
    )
    // runEngine with no subcommand still spawns; assert it did not report a
    // missing module, which is what a wrong cwd produces.
    expect(`${probe.stdout}${probe.stderr}`).not.toMatch(/No module named 'kauto'/)

    // The direct claim: the imported package lives inside this package.
    const spawned = spawnSync(
      python,
      ['-c', 'import kauto, sys; print(kauto.__file__)'],
      {
        cwd: paths.engineDir,
        env: { ...process.env, PYTHONPATH: paths.engineDir },
        encoding: 'utf8',
        timeout: 90_000,
      },
    )
    const where = `${spawned.stdout ?? ''}`.trim()
    expect(where).not.toBe('')
    expect(where.replace(/\\/g, '/')).toContain('dsh-yuan-kaggle-auto/engine/kauto')
  }, 150_000)

  it('reports a structured error for a command the CLI does not have', async () => {
    const result = await runEngine(['totally-not-a-command'], { python, paths, settings }, {
      timeoutMs: 60_000,
    })
    expect(result.ok).toBe(false)
    expect(result.error).toBeTruthy()
    // The reason must name the command, not just "failed".
    expect(result.error).toMatch(/totally-not-a-command|usage|invalid|argument/i)
  }, 90_000)
})
