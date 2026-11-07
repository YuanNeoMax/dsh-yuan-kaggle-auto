/**
 * src/runner.ts — invoke the Python engine and read its answer back.
 *
 * The engine is a CLI that prints JSON on stdout for machine-readable commands
 * and human text for others. There is no sentinel line (unlike ego-browser), so
 * parsing is a three-tier fallback: whole-stdout JSON, then the last balanced
 * JSON value anywhere in the tail, then a structured error carrying stderr.
 *
 * Every call goes through `ctx.subprocess.spawn` when the host provides it
 * (bounded buffers, grace periods, and teardown handled by the host), and
 * through a local spawn otherwise — a headless host without the subprocess
 * service must still be usable.
 */
import { spawn } from 'node:child_process'
import type { ResolvedPaths } from './paths.ts'
import { engineRootFor } from './paths.ts'
import type { Settings } from './config.ts'

export interface RunOptions {
  /** Hard kill after this many ms; 0 disables (long tasks like iterate). */
  timeoutMs?: number
  /** Called for each output line, so a job log can stream instead of blocking. */
  onLine?: (line: { stream: 'stdout' | 'stderr'; text: string }) => void
  /** Abort signal honoured by the streaming spawn. */
  signal?: AbortSignal
  /** Extra environment on top of the computed one. */
  env?: NodeJS.ProcessEnv
  /** Per-call buffer cap for the collected output; defaults to 8 MB. */
  maxBytes?: number
}

export interface RunResult {
  ok: boolean
  /** Parsed stdout when it was JSON, else undefined. */
  value?: unknown
  /** Raw stdout, capped. */
  stdout: string
  /** Raw stderr, capped. */
  stderr: string
  exitCode: number | null
  /** Present when !ok: the most useful sentence we can produce. */
  error?: string
}

const DEFAULT_MAX_BYTES = 8 * 1024 * 1024

/** Environment contract between the host shell and the engine. */
export function engineEnv(
  settings: Settings,
  paths: ResolvedPaths,
  base: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...base,
    PYTHONPATH: paths.engineDir,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    // The engine's writable root. `work/` `data/` `reports/` hang off it,
    // so pointing at `workRoot` itself would nest `work/work/`.
    KAUTO_ROOT: engineRootFor(paths.workRoot),
    KAUTO_THREADS: String(settings.threads),
  }
  if (paths.skillRoot) env.KAUTO_SKILL_ROOT = paths.skillRoot
  if (settings.uploadProxy.trim()) env.KAUTO_UPLOAD_PROXY = settings.uploadProxy.trim()
  if (settings.sandboxEnabled === false) env.KAUTO_NO_SANDBOX = '1'
  // Optional LLM overrides. Empty values must not clobber the host's own
  // detection, hence the conditional assignments rather than `?? ''`.
  if (settings.llmProvider !== 'auto') env.KAUTO_LLM_PROVIDER = settings.llmProvider
  if (settings.llmBaseUrl.trim()) env.KAUTO_LLM_BASE = settings.llmBaseUrl.trim()
  if (settings.llmModel.trim()) env.KAUTO_LLM_MODEL = settings.llmModel.trim()
  if (settings.llmApiKey.trim()) env.KAUTO_LLM_KEY = settings.llmApiKey.trim()
  return env
}

/**
 * Pull the last balanced JSON object/array out of arbitrary trailing text.
 *
 * `kauto` prints a JSON document then sometimes a human sentence after it, and
 * some commands print progress lines *before* the document. Scanning from the
 * end for a balanced span covers both without a sentinel.
 */
export function lastJsonValue(text: string): unknown {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  try {
    return JSON.parse(trimmed)
  } catch {
    // fall through to the scan
  }
  // Scan candidate starts from the end; the first one that balances wins.
  for (let start = trimmed.length - 1; start >= 0; start -= 1) {
    const ch = trimmed[start]
    if (ch !== '{' && ch !== '[') continue
    const end = matchBracket(trimmed, start)
    if (end === -1) continue
    try {
      return JSON.parse(trimmed.slice(start, end + 1))
    } catch {
      continue
    }
  }
  return undefined
}

/** Index of the matching close bracket, honouring strings and escapes; -1 if unbalanced. */
function matchBracket(text: string, start: number): number {
  const open = text[start]
  const close = open === '{' ? '}' : ']'
  let depth = 0
  let inString = false
  let escaped = false
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i]
    if (inString) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') { inString = true; continue }
    if (ch === open) depth += 1
    else if (ch === close) {
      depth -= 1
      if (depth === 0) return i
    }
  }
  return -1
}

/** The most actionable sentence available when a run fails. */
export function explainFailure(result: RunResult, command: string[]): string {
  const stderr = result.stderr.trim()
  const stdout = result.stdout.trim()
  if (stderr) {
    // Engines puts the real reason on stderr; the last few lines carry the traceback tail.
    const lines = stderr.split(/\r?\n/).filter((line) => line.trim() !== '')
    return `kauto ${command.join(' ')} 失败（exit ${result.exitCode ?? '?'}）：${lines.slice(-3).join(' | ').slice(0, 600)}`
  }
  if (stdout) {
    const lines = stdout.split(/\r?\n/).filter((line) => line.trim() !== '')
    return `kauto ${command.join(' ')} 未返回可解析结果（exit ${result.exitCode ?? '?'}）：${lines.slice(-3).join(' | ').slice(0, 600)}`
  }
  return `kauto ${command.join(' ')} 无输出（exit ${result.exitCode ?? '?'}）`
}

/** Cap a string from the head so the beginning (which names the cause) survives. */
function cap(text: string, limit: number): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}\n…[truncated ${text.length - limit} chars]`
}

export type SpawnLike = (options: {
  argv: string[]
  cwd: string
  env: NodeJS.ProcessEnv
  stdio: { stdout: { maxBytes: number }; stderr: { maxBytes: number } }
  graceMs?: number
}) => {
  done: Promise<{ exitCode: number | null }>
  collected: { stdout: { readFrom(offset: number): { text: string } }; stderr: { readFrom(offset: number): { text: string } } }
}

/**
 * One engine invocation.
 *
 * `readAll` mirrors the host's collected-output shape (`readFrom(0)`), so the
 * same code reads output whether the host or the local fallback produced it.
 */
export async function runEngine(
  command: string[],
  ctx: {
    python: string
    paths: ResolvedPaths
    settings: Settings
    /** Host subprocess service, when present. */
    subprocess?: { spawn: SpawnLike } | undefined
  },
  options: RunOptions = {},
): Promise<RunResult> {
  const argv = [ctx.python, '-m', 'kauto.cli', ...command]
  const env = { ...engineEnv(ctx.settings, ctx.paths), ...options.env }
  const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES
  // ## cwd must not be the work root
  //
  // `python -m <pkg>` puts the CURRENT DIRECTORY on sys.path[0], ahead of
  // PYTHONPATH. Running with `cwd = workRoot` means that if the work root
  // happens to sit inside another checkout that also has a `kauto/` package
  // (the common case: the user pointed `workRoot` at their own kaggle-auto
  // clone), the wrong engine loads and every call silently tests that copy.
  //
  // The engine reads its writable root from KAUTO_ROOT, not from cwd, so cwd
  // only has to be somewhere neutral: the engine directory itself.
  const cwd = ctx.paths.engineDir

  if (ctx.subprocess?.spawn) {
    try {
      const handle = ctx.subprocess.spawn({
        argv,
        cwd,
        env,
        stdio: { stdout: { maxBytes }, stderr: { maxBytes } },
        graceMs: options.timeoutMs ?? 0,
      })
      const outcome = await handle.done
      const stdout = cap(handle.collected.stdout.readFrom(0).text, maxBytes)
      const stderr = cap(handle.collected.stderr.readFrom(0).text, maxBytes)
      if (options.onLine) replayLines(stdout, stderr, options.onLine)
      return finish(command, stdout, stderr, outcome.exitCode ?? null)
    } catch {
      // Host subprocess refused (sandbox, policy, or a teardown race): fall
      // through to the local spawn so a single failed bridge does not kill the
      // whole feature.
    }
  }

  const { code, output } = await runLocal(argv, cwd, env, options, maxBytes)
  const split = splitStreams(command, output)
  if (options.onLine) replayLines(split.stdout, split.stderr, options.onLine)
  return finish(command, split.stdout, split.stderr, code)
}

/** Local fallback: one child, both descriptors merged with a stream prefix. */
function runLocal(
  argv: string[],
  cwd: string,
  env: NodeJS.ProcessEnv,
  options: RunOptions,
  maxBytes: number,
): Promise<{ code: number | null; output: string }> {
  return new Promise((resolve) => {
    let output = ''
    let killedForTimeout = false
    const push = (stream: 'stdout' | 'stderr', chunk: Buffer | string): void => {
      const text = String(chunk)
      output += stream === 'stdout' ? text : text.split(/\r?\n/).map((line) => (line ? `[stderr] ${line}\n` : '')).join('')
      if (output.length > maxBytes * 2) output = output.slice(-maxBytes)
    }
    let child
    try {
      child = spawn(argv[0] as string, argv.slice(1), {
        cwd,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        signal: options.signal,
      })
    } catch (error) {
      resolve({ code: null, output: `[stderr] spawn failed: ${error instanceof Error ? error.message : String(error)}` })
      return
    }
    child.stdout?.on('data', (chunk: Buffer | string) => push('stdout', chunk))
    child.stderr?.on('data', (chunk: Buffer | string) => push('stderr', chunk))
    let timer: NodeJS.Timeout | undefined
    if (options.timeoutMs && options.timeoutMs > 0) {
      timer = setTimeout(() => { killedForTimeout = true; child?.kill('SIGTERM') }, options.timeoutMs)
    }
    child.on('error', (error) => {
      if (timer) clearTimeout(timer)
      resolve({ code: null, output: `${output}\n[stderr] ${error.message}` })
    })
    child.on('close', (code) => {
      if (timer) clearTimeout(timer)
      if (killedForTimeout) output += `\n[stderr] timed out after ${options.timeoutMs} ms`
      resolve({ code, output })
    })
  })
}

function splitStreams(command: string[], merged: string): { stdout: string; stderr: string } {
  const stdoutLines: string[] = []
  const stderrLines: string[] = []
  for (const line of merged.split(/\r?\n/)) {
    if (line.startsWith('[stderr] ')) stderrLines.push(line.slice('[stderr] '.length))
    else stdoutLines.push(line)
  }
  return { stdout: stdoutLines.join('\n'), stderr: stderrLines.join('\n') }
}

function replayLines(
  stdout: string,
  stderr: string,
  onLine: (line: { stream: 'stdout' | 'stderr'; text: string }) => void,
): void {
  for (const text of stdout.split(/\r?\n/)) if (text.trim()) onLine({ stream: 'stdout', text })
  for (const text of stderr.split(/\r?\n/)) if (text.trim()) onLine({ stream: 'stderr', text })
}

function finish(command: string[], stdout: string, stderr: string, exitCode: number | null): RunResult {
  const value = lastJsonValue(stdout)
  const ok = exitCode === 0
  const result: RunResult = { ok, value, stdout, stderr, exitCode }
  if (!ok || (value === undefined && stdout.trim() === '' && stderr.trim() !== '')) {
    result.error = explainFailure(result, command)
  }
  return result
}
