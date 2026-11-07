/**
 * src/python.ts — find a usable Python, and own the engine venv's lifecycle.
 *
 * Resolution is a chain of *evidence*, not a single guess: an explicit path,
 * then an environment override, then the venv this plugin built, then PATH.
 * Each failure carries an instruction the user can act on — a plugin that says
 * "python not found" and stops is useless to someone who has never installed one.
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { isWindows, venvPython } from './paths.ts'

/** Minimum the engine needs (it uses `str | None` annotations and `match`-free 3.10 syntax). */
export const MIN_PYTHON = { major: 3, minor: 10 } as const

export interface PythonCandidate {
  /** What to execute. */
  command: string
  /** Extra argv entries that must precede the script/module args (e.g. `-3`). */
  prefix: string[]
  /** How this candidate was found — surfaced in status so the choice is auditable. */
  source: 'setting' | 'env' | 'venv' | 'path'
}

export interface ProbeResult {
  ok: boolean
  version?: string
  /** Populated only when ok === false. */
  reason?: string
  /** Actionable next step, always populated on failure. */
  hint?: string
}

const INSTALL_HINT =
  'Install Python 3.10 or newer, then set 设置 → Kaggle 自动参赛 → Python 解释器 指向它。' +
  (isWindows()
    ? ' Windows: 从 python.org 安装并勾选 "Add python.exe to PATH"，或执行 `winget install Python.Python.3.12`。'
    : ' macOS: `brew install python@3.12`；Linux: 用发行版包管理器安装 `python3`。')

/** Run `<command> <prefix...> --version` and parse a 3.x version out of it. */
export function probeVersion(command: string, prefix: string[] = [], timeoutMs = 8_000): Promise<ProbeResult> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (result: ProbeResult): void => {
      if (settled) return
      settled = true
      resolve(result)
    }
    let child
    try {
      child = spawn(command, [...prefix, '--version'], {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      finish({
        ok: false,
        reason: `${command} could not be started: ${error instanceof Error ? error.message : String(error)}`,
        hint: INSTALL_HINT,
      })
      return
    }
    let text = ''
    child.stdout?.on('data', (chunk: Buffer | string) => { text += String(chunk) })
    child.stderr?.on('data', (chunk: Buffer | string) => { text += String(chunk) })
    const timer = setTimeout(() => {
      child.kill()
      finish({ ok: false, reason: `${command} did not answer --version within ${timeoutMs} ms`, hint: INSTALL_HINT })
    }, timeoutMs)
    child.on('error', (error) => {
      clearTimeout(timer)
      finish({ ok: false, reason: `${command}: ${error.message}`, hint: INSTALL_HINT })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (code !== 0) {
        finish({ ok: false, reason: `${command} --version exited ${code}: ${text.trim().slice(0, 120)}`, hint: INSTALL_HINT })
        return
      }
      const match = /Python\s+(\d+)\.(\d+)\.(\d+)/.exec(text)
      if (!match) {
        finish({ ok: false, reason: `could not read a version from ${command}: ${text.trim().slice(0, 120)}`, hint: INSTALL_HINT })
        return
      }
      const major = Number(match[1])
      const minor = Number(match[2])
      if (major < MIN_PYTHON.major || (major === MIN_PYTHON.major && minor < MIN_PYTHON.minor)) {
        finish({
          ok: false,
          version: `${major}.${minor}.${match[3]}`,
          reason: `${command} is Python ${major}.${minor}, the engine needs ≥ ${MIN_PYTHON.major}.${MIN_PYTHON.minor}`,
          hint: INSTALL_HINT,
        })
        return
      }
      finish({ ok: true, version: `${major}.${minor}.${match[3]}` })
    })
  })
}

/** Ordered candidates for the interpreter, before any probing. */
export function pythonCandidates(settings: {
  pythonPath: string
  venvDir: string
}, env: NodeJS.ProcessEnv = process.env, platform: NodeJS.Platform = process.platform): PythonCandidate[] {
  const list: PythonCandidate[] = []
  const explicit = settings.pythonPath.trim()
  if (explicit) list.push({ command: explicit, prefix: [], source: 'setting' })

  const fromEnv = (env.KAUTO_PYTHON ?? '').trim()
  if (fromEnv) list.push({ command: fromEnv, prefix: [], source: 'env' })

  const venv = venvPython(settings.venvDir, platform)
  if (existsSync(venv)) list.push({ command: venv, prefix: [], source: 'venv' })

  if (isWindows(platform)) {
    list.push({ command: 'py', prefix: ['-3'], source: 'path' })
  }
  list.push({ command: 'python3', prefix: [], source: 'path' })
  list.push({ command: 'python', prefix: [], source: 'path' })
  return list
}

/**
 * First candidate that answers with a new enough version.
 *
 * The version probe is the filter, so a stale `python` on PATH is skipped
 * rather than used and failing later inside the engine with a syntax error.
 */
export async function findPython(
  settings: { pythonPath: string; venvDir: string },
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): Promise<{ ok: true; candidate: PythonCandidate; version: string } | { ok: false; reason: string; hint: string; tried: string[] }> {
  const tried: string[] = []
  let lastReason = ''
  for (const candidate of pythonCandidates(settings, env, platform)) {
    const label = [candidate.command, ...candidate.prefix].join(' ')
    tried.push(label)
    const result = await probeVersion(candidate.command, candidate.prefix)
    if (result.ok) return { ok: true, candidate, version: result.version ?? 'unknown' }
    lastReason = result.reason ?? ''
  }
  return {
    ok: false,
    reason: lastReason || 'no Python interpreter found',
    hint: INSTALL_HINT,
    tried,
  }
}

// ── venv lifecycle ──────────────────────────────────────────────────────────

/** Marker recording what the venv was built from; lets a re-run skip the work. */
interface VenvStamp {
  engineVersion: string
  requirementsHash: string
  pythonVersion: string
  builtAt: string
}

export const STAMP_FILENAME = '.kauto-venv-stamp.json'

export function readStamp(venvDir: string): VenvStamp | undefined {
  const file = join(venvDir, STAMP_FILENAME)
  if (!existsSync(file)) return undefined
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8')) as Partial<VenvStamp>
    if (typeof parsed.requirementsHash !== 'string') return undefined
    return parsed as VenvStamp
  } catch {
    return undefined
  }
}

export function writeStamp(venvDir: string, stamp: VenvStamp): void {
  mkdirSync(venvDir, { recursive: true })
  writeFileSync(join(venvDir, STAMP_FILENAME), `${JSON.stringify(stamp, null, 2)}\n`, 'utf8')
}

export interface SpawnLine {
  stream: 'stdout' | 'stderr'
  text: string
}

/**
 * A tiny promise-wrapped spawn that streams lines.
 *
 * Deliberately not execa: this must run with the *user's* interpreter and may
 * legitimately outlive a short tool call (venv creation takes minutes on a
 * cold pip cache), so the caller keeps the child handle and forwards chunks to
 * the job log instead of buffering everything until exit.
 */
export function runStreaming(
  command: string,
  args: string[],
  options: {
    cwd?: string
    env?: NodeJS.ProcessEnv
    onLine?: (line: SpawnLine) => void
    timeoutMs?: number
    signal?: AbortSignal
  } = {},
): Promise<{ code: number | null; signal: NodeJS.Signals | null; output: string }> {
  return new Promise((resolve) => {
    let output = ''
    const emit = (stream: 'stdout' | 'stderr', chunk: Buffer | string): void => {
      const text = String(chunk)
      output += text
      if (output.length > 400_000) output = output.slice(-200_000)
      if (!options.onLine) return
      for (const line of text.split(/\r?\n/)) {
        if (line.trim() !== '' ) options.onLine({ stream, text: line })
      }
    }
    let child
    try {
      child = spawn(command, args, {
        cwd: options.cwd,
        env: options.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        signal: options.signal,
      })
    } catch (error) {
      resolve({ code: null, signal: null, output: `spawn failed: ${error instanceof Error ? error.message : String(error)}` })
      return
    }
    child.stdout?.on('data', (chunk: Buffer | string) => emit('stdout', chunk))
    child.stderr?.on('data', (chunk: Buffer | string) => emit('stderr', chunk))
    let timer: NodeJS.Timeout | undefined
    if (options.timeoutMs && options.timeoutMs > 0) {
      timer = setTimeout(() => { child?.kill('SIGTERM') }, options.timeoutMs)
    }
    child.on('error', (error) => {
      if (timer) clearTimeout(timer)
      resolve({ code: null, signal: null, output: `${output}\nspawn error: ${error.message}` })
    })
    child.on('close', (code, signal) => {
      if (timer) clearTimeout(timer)
      resolve({ code, signal, output })
    })
  })
}

/** Wire a runStreaming handle into a job log: returns the promise plus nothing else to hold. */
export function runStreamingInto(
  job: { push: (line: SpawnLine) => void },
  command: string,
  args: string[],
  options: Omit<Parameters<typeof runStreaming>[2] & object, 'onLine'> = {},
): ReturnType<typeof runStreaming> {
  return runStreaming(command, args, { ...options, onLine: (line) => job.push(line) })
}

export interface SetupStep {
  step: 'venv' | 'pip' | 'requirements' | 'verify'
  ok: boolean
  detail?: string
}

export interface SetupOutcome {
  ok: boolean
  python?: string
  version?: string
  steps: SetupStep[]
  /** Last lines of the failing step's output — enough to actually diagnose. */
  tail?: string
  error?: string
  /** Reused an existing venv instead of rebuilding. */
  reused?: boolean
}
