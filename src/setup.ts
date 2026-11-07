/**
 * src/setup.ts — build and maintain the engine venv, and report readiness.
 *
 * Two jobs, deliberately separate:
 *   - `installEngine` does the slow, state-changing work once (venv + pip);
 *   - `readStatus` answers "is this usable right now" cheaply enough to power
 *     a panel that refreshes on every open.
 *
 * The stamp file makes install idempotent: a second click with an unchanged
 * requirements file and engine version does nothing at all, which matters
 * because a cold `pip install lightgbm` is minutes of the user's life.
 */
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Settings } from './config.ts'
import type { ResolvedPaths } from './paths.ts'
import { isWindows, venvKaggle, venvPython } from './paths.ts'
import { findPython, readStamp, runStreaming, writeStamp, type SetupStep } from './python.ts'

/** Bumped whenever the engine layout changes in a way a venv must notice. */
export const ENGINE_CONTRACT_VERSION = '1'

export interface InstallOptions {
  onLine?: (line: { stream: 'stdout' | 'stderr'; text: string }) => void
  signal?: AbortSignal
  /** Rebuild even when the stamp matches. */
  force?: boolean
}

export interface InstallResult {
  ok: boolean
  reused: boolean
  python?: string
  pythonVersion?: string
  steps: SetupStep[]
  error?: string
  /** Tail of the failing step's output, capped — enough to diagnose pip. */
  tail?: string
}

function requirementsPath(paths: ResolvedPaths): string {
  return join(paths.engineDir, 'requirements.lock.txt')
}

function requirementsHash(paths: ResolvedPaths): string {
  const file = requirementsPath(paths)
  if (!existsSync(file)) return 'missing'
  return createHash('sha256').update(readFileSync(file, 'utf8')).digest('hex').slice(0, 16)
}

/** pip on Windows needs an explicit proxy reset when `` blocked PyPI before. */
function pipArgs(paths: ResolvedPaths, settings: Settings): string[] {
  const args = ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input']
  if (isWindows()) {
    // Measured on this machine: a system proxy makes pip fail its TLS handshake
    // to PyPI. An empty --proxy is the root fix; it overrides the registry value,
    // which HTTP_PROXY/HTTPS_PROXY alone do not.
    if (!settings.uploadProxy.trim()) args.push('--proxy', '')
  } else if (settings.uploadProxy.trim()) {
    args.push('--proxy', settings.uploadProxy.trim())
  }
  args.push('-r', requirementsPath(paths))
  return args
}

/**
 * Create or refresh the venv. Idempotent: stamp match ⇒ no-op.
 *
 * Step order matters and is preserved in the result so the panel can show
 * exactly which stage failed — "setup failed" alone sends people nowhere.
 */
export async function installEngine(
  settings: Settings,
  paths: ResolvedPaths,
  options: InstallOptions = {},
): Promise<InstallResult> {
  const steps: SetupStep[] = []
  const emit = options.onLine

  // ── 1. a usable interpreter to build the venv from ────────────────────────
  const found = await findPython(settings, process.env)
  if (!found.ok) {
    steps.push({ step: 'venv', ok: false, detail: found.reason })
    return { ok: false, reused: false, steps, error: found.reason, tail: found.hint }
  }

  const targetPython = venvPython(paths.venvDir)
  const stamp = readStamp(paths.venvDir)
  const wantHash = requirementsHash(paths)
  const venvUsable = existsSync(targetPython)

  // ── 2. stamp fast-path ────────────────────────────────────────────────────
  if (
    venvUsable &&
    !options.force &&
    stamp &&
    stamp.requirementsHash === wantHash &&
    stamp.engineVersion === ENGINE_CONTRACT_VERSION &&
    stamp.pythonVersion === (found.version ?? 'unknown')
  ) {
    steps.push({ step: 'venv', ok: true, detail: 'reused existing venv (stamp matched)' })
    return { ok: true, reused: true, python: targetPython, pythonVersion: found.version, steps }
  }

  // ── 3. create the venv ────────────────────────────────────────────────────
  if (!venvUsable) {
    mkdirSync(paths.venvDir, { recursive: true })
    const created = await runStreaming(
      found.candidate.command,
      [...found.candidate.prefix, '-m', 'venv', paths.venvDir],
      { onLine: emit, signal: options.signal, timeoutMs: 180_000 },
    )
    if (created.code !== 0 || !existsSync(targetPython)) {
      steps.push({ step: 'venv', ok: false, detail: `python -m venv exited ${created.code}` })
      return {
        ok: false,
        reused: false,
        steps,
        python: targetPython,
        pythonVersion: found.version,
        error: '创建虚拟环境失败',
        tail: tailOf(created.output),
      }
    }
    steps.push({ step: 'venv', ok: true, detail: `venv created at ${paths.venvDir}` })
  } else {
    steps.push({ step: 'venv', ok: true, detail: 'existing venv reused (dependencies will be refreshed)' })
  }

  // ── 4. upgrade pip, then install requirements ─────────────────────────────
  const pipUp = await runStreaming(targetPython, ['-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check', '--no-input', ...(isWindows() ? ['--proxy', ''] : [])], {
    onLine: emit,
    signal: options.signal,
    timeoutMs: 240_000,
  })
  steps.push({
    step: 'pip',
    ok: pipUp.code === 0,
    detail: pipUp.code === 0 ? 'pip upgraded' : `pip upgrade exited ${pipUp.code}`,
  })

  const installed = await runStreaming(targetPython, pipArgs(paths, settings), {
    onLine: emit,
    signal: options.signal,
    timeoutMs: 900_000,
  })
  if (installed.code !== 0) {
    steps.push({ step: 'requirements', ok: false, detail: `pip install exited ${installed.code}` })
    return {
      ok: false,
      reused: false,
      steps,
      python: targetPython,
      pythonVersion: found.version,
      error: '安装引擎依赖失败（见日志尾部）',
      tail: tailOf(installed.output),
    }
  }
  steps.push({ step: 'requirements', ok: true, detail: 'engine dependencies installed' })

  // ── 5. import smoke: catches a broken wheel that pip still "succeeded" on ──
  const smoke = await runStreaming(
    targetPython,
    ['-c', 'import pandas, numpy, sklearn, lightgbm; print("engine-ok")'],
    { env: { ...process.env, PYTHONPATH: paths.engineDir }, onLine: emit, signal: options.signal, timeoutMs: 180_000 },
  )
  const smokeOk = smoke.code === 0 && smoke.output.includes('engine-ok')
  steps.push({
    step: 'verify',
    ok: smokeOk,
    detail: smokeOk ? 'import smoke passed' : `import smoke exited ${smoke.code}`,
  })
  if (!smokeOk) {
    return {
      ok: false,
      reused: false,
      steps,
      python: targetPython,
      pythonVersion: found.version,
      error: '依赖装上了但导入失败（可能是平台 wheel 问题）',
      tail: tailOf(smoke.output),
    }
  }

  writeStamp(paths.venvDir, {
    engineVersion: ENGINE_CONTRACT_VERSION,
    requirementsHash: wantHash,
    pythonVersion: found.version ?? 'unknown',
    builtAt: new Date().toISOString(),
  })

  return { ok: true, reused: false, python: targetPython, pythonVersion: found.version, steps }
}

function tailOf(output: string, limit = 30): string {
  const lines = output.split(/\r?\n/).filter((line) => line.trim() !== '')
  return lines.slice(-limit).join('\n').slice(0, 4000)
}

// ── status ───────────────────────────────────────────────────────────────────

export type StatusLevel = 'ok' | 'warn' | 'missing'

export interface StatusItem {
  /** Stable id for the panel; not localised. */
  id: string
  /** Short label. */
  label: string
  level: StatusLevel
  /** One line of fact (version, path, count). */
  detail?: string
  /** What to do when level !== 'ok'. */
  fix?: string
}

export interface EngineStatus {
  ready: boolean
  items: StatusItem[]
  paths: { venvDir: string; workRoot: string; engineDir: string }
}

/**
 * Cheap readiness probe. Never installs, never spawns a long child.
 *
 * The panel calls this on every open, so each check is a file existence test
 * or a bounded subprocess. The one subprocess (`--version`) has an 8 s ceiling.
 */
export async function readStatus(
  settings: Settings,
  paths: ResolvedPaths,
  probes: {
    /** Optional ego-browser availability, supplied by the host. */
    ego?: { available: boolean; detail?: string }
    /** Optional host LLM description, so "no key" is not reported wrongly. */
    llm?: { available: boolean; detail?: string }
  } = {},
): Promise<EngineStatus> {
  const items: StatusItem[] = []

  // Python for building the venv (only meaningful before setup).
  const found = await findPython(settings, process.env)
  items.push(
    found.ok
      ? {
          id: 'python',
          label: 'Python',
          level: 'ok',
          detail: `${found.version}（${found.candidate.source}）`,
        }
      : {
          id: 'python',
          label: 'Python',
          level: 'missing',
          detail: found.reason,
          fix: found.hint,
        },
  )

  const targetPython = venvPython(paths.venvDir)
  const venvExists = existsSync(targetPython)
  const stamp = venvExists ? readStamp(paths.venvDir) : undefined
  const wantHash = requirementsHash(paths)
  const stampFresh = Boolean(stamp && stamp.requirementsHash === wantHash && stamp.engineVersion === ENGINE_CONTRACT_VERSION)

  items.push(
    venvExists
      ? {
          id: 'venv',
          label: '引擎环境',
          level: stampFresh ? 'ok' : 'warn',
          detail: venvExists && stampFresh ? `${paths.venvDir}` : `${paths.venvDir}（依赖可能需要刷新）`,
          fix: stampFresh ? undefined : '点「初始化环境」刷新依赖',
        }
      : {
          id: 'venv',
          label: '引擎环境',
          level: 'missing',
          detail: '尚未初始化',
          fix: '点「初始化环境」',
        },
  )

  // Dependencies: file-only check, no import.
  const depProbe = venvExists ? existsSync(join(paths.venvDir, isWindows() ? 'Scripts' : 'bin', isWindows() ? 'lightgbm.exe' : 'lightgbm')) : false
  const sitePackages = join(paths.venvDir, isWindows() ? 'Lib' : join('lib', 'python3')) 
  const lightgbmInstalled = venvExists
    ? existsSync(join(paths.venvDir, isWindows() ? 'Lib' : 'lib')) &&
      findAnyDir(paths.venvDir, 'lightgbm', 3)
    : false
  items.push({
    id: 'deps',
    label: '依赖',
    level: lightgbmInstalled ? 'ok' : venvExists ? 'warn' : 'missing',
    detail: lightgbmInstalled
      ? 'pandas / numpy / scikit-learn / lightgbm 已安装'
      : venvExists
        ? '未检测到 lightgbm'
        : '尚未安装',
    fix: lightgbmInstalled ? undefined : '点「初始化环境」',
  })
  void depProbe
  void sitePackages

  // Kaggle CLI: the engine's own venv copy first, then the user's pin.
  const kaggleCli = settings.kaggleCliPath.trim() || venvKaggle(paths.venvDir)
  const kaggleCliPresent = existsSync(kaggleCli)
  items.push({
    id: 'kaggle-cli',
    label: 'Kaggle CLI',
    level: kaggleCliPresent ? 'ok' : 'missing',
    detail: kaggleCliPresent ? kaggleCli : '未找到',
    fix: kaggleCliPresent ? undefined : '初始化环境会一并安装，或在设置里指定路径',
  })

  // Kaggle credentials: file-only probe, values never read.
  const credDir = join(process.env.DSH_HOME?.trim() || join(homeDir(), '.dsh'), '..')
  const kaggleCreds = kaggleCredentialSummary()
  items.push({
    id: 'kaggle-creds',
    label: 'Kaggle 凭据',
    level: kaggleCreds.present ? 'ok' : 'missing',
    detail: kaggleCreds.detail,
    fix: kaggleCreds.present ? undefined : '在 Kaggle 账号设置里生成 API Token，或执行 kaggle auth login',
  })
  void credDir

  items.push({
    id: 'llm',
    label: 'LLM 通道',
    level: probes.llm?.available ? 'ok' : 'warn',
    detail: probes.llm?.detail ?? '未探测',
    fix: probes.llm?.available
      ? undefined
      : '迭代功能需要 LLM；baseline / write-sub / submit 不需要，可先跑这些',
  })

  items.push({
    id: 'ego',
    label: 'ego 浏览器',
    level: probes.ego?.available ? 'ok' : 'warn',
    detail: probes.ego?.available ? (probes.ego.detail ?? '可用') : '未安装（可选）',
    fix: probes.ego?.available ? undefined : '可选：点「安装 ego」让 agent 在必须点按钮时自动接管',
  })

  const skillPresent = Boolean(settings.skillBridgeDir.trim() && existsSync(join(paths.skillRoot, 'skills', 'kaggle', 'scripts', 'kaggle_skill.py')))
  items.push({
    id: 'skill-bridge',
    label: 'skill 桥接',
    level: skillPresent ? 'ok' : 'ok',
    detail: skillPresent ? paths.skillRoot : '未安装（可选，不影响核心流程）',
  })

  const ready = Boolean(venvExists && stampFresh && lightgbmInstalled && kaggleCliPresent)
  return {
    ready,
    items,
    paths: { venvDir: paths.venvDir, workRoot: paths.workRoot, engineDir: paths.engineDir },
  }
}

/** Depth-limited search for a package directory inside site-packages. */
function findAnyDir(root: string, name: string, depth: number): boolean {
  if (depth < 0) return false
  const candidates = [
    join(root, isWindows() ? 'Lib' : '', 'site-packages', name),
    join(root, 'lib', 'python3', 'site-packages', name),
  ]
  if (candidates.some((path) => existsSync(path))) return true
  // Look under any python3.x directory without enumerating the whole tree.
  for (const version of ['python3.10', 'python3.11', 'python3.12', 'python3.13', 'python3.14']) {
    if (existsSync(join(root, 'lib', version, 'site-packages', name))) return true
  }
  return false
}

function homeDir(): string {
  return process.env.USERPROFILE?.trim() || process.env.HOME?.trim() || '.'
}

/** Present + which mechanism, never the secret itself. */
function kaggleCredentialSummary(): { present: boolean; detail: string } {
  const home = process.env.USERPROFILE?.trim() || process.env.HOME?.trim() || ''
  const candidates: Array<[string, string]> = [
    [join(home, '.kaggle', 'credentials.json'), 'OAuth（credentials.json）'],
    [join(home, '.kaggle', 'access_token'), 'access_token 文件'],
    [join(home, '.kaggle', 'kaggle.json'), 'kaggle.json'],
  ]
  for (const [path, label] of candidates) {
    if (existsSync(path)) return { present: true, detail: label }
  }
  if (process.env.KAGGLE_USERNAME && process.env.KAGGLE_KEY) {
    return { present: true, detail: '环境变量 KAGGLE_USERNAME / KAGGLE_KEY' }
  }
  return { present: false, detail: '未找到' }
}
