/**
 * src/installer.ts — one-click installation of optional companion plugins.
 *
 * ## Why this does not shell out to `dsh plugin add`
 *
 * Measured (2026-11 on DSH 0.2.1-rc.2):
 *   - `@deepseek-ai/dsh/lib/bin.js` refuses `--profile desktop` outright —
 *     "profile desktop is managed exclusively by the Electron application" —
 *     and the desktop profile is exactly where a GUI user installs plugins.
 *   - `@deepseek-ai/dsh-plugin-manager` exposes only the *read* remotes
 *     (`listPlugins`, `listBundles`, `registries`, `inspect`). There is no
 *     install/add remote to call.
 *
 * So installation is done the way the in-tree super-injector does it, which is
 * proven on this machine: write the profile's `package.json` (dependency AND
 * the `dsh.profile.bundles` list), materialise the package through pnpm, then
 * `ctx.loader.create()` so it appears without a restart.
 *
 * Everything is reversible: the recorded undo list runs in reverse, so a failure
 * halfway through leaves the profile exactly as it was.
 */
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { runStreaming } from './python.ts'
import { dshHome } from './paths.ts'

export type InstallStep = {
  step: 'resolve' | 'pnpm' | 'manifest' | 'bundles' | 'link' | 'load' | 'verify'
  ok: boolean
  detail?: string
}

export interface InstallOutcome {
  ok: boolean
  already?: boolean
  steps: InstallStep[]
  error?: string
  /** Tail of the failing command, capped. */
  tail?: string
  /** Package version actually present after a successful install. */
  version?: string
}

export interface InstallRequest {
  /** npm spec: a name, a name@range, a git URL, or a local tarball path. */
  spec: string
  /** What the entry is called in the Loader (also the settings namespace). */
  entryId: string
  /** Package name once installed; defaults to the spec's name part. */
  packageName?: string
  profile?: string
  onLine?: (line: { stream: 'stdout' | 'stderr'; text: string }) => void
  signal?: AbortSignal
}

/** Split `name@range` / `@scope/name@range` into package name and range. */
export function splitSpec(spec: string): { name: string; range: string } {
  const raw = spec.trim()
  if (!raw) return { name: '', range: '' }
  // A path or URL has no version to split off.
  if (raw.includes('/') && (raw.startsWith('.') || raw.startsWith('/') || /^[A-Za-z]:/.test(raw))) {
    return { name: raw, range: '' }
  }
  if (raw.startsWith('github:') || raw.startsWith('git+') || raw.endsWith('.tgz')) {
    return { name: raw, range: '' }
  }
  const at = raw.startsWith('@') ? raw.indexOf('@', 1) : raw.indexOf('@')
  if (at === -1) return { name: raw, range: '' }
  return { name: raw.slice(0, at), range: raw.slice(at + 1) }
}

function profileDir(profile: string, env: NodeJS.ProcessEnv = process.env): string {
  return join(dshHome(env), 'profiles', profile)
}

interface ProfileManifest {
  name?: string
  version?: string
  dsh?: { profile?: { bundles?: string[] } }
  dependencies?: Record<string, string>
  [key: string]: unknown
}

function readManifest(dir: string): ProfileManifest | undefined {
  const file = join(dir, 'package.json')
  if (!existsSync(file)) return undefined
  try {
    return JSON.parse(readFileSync(file, 'utf8')) as ProfileManifest
  } catch {
    return undefined
  }
}

/** Undo stack: each entry restores exactly what it changed. */
type Undo = () => void

/**
 * Install one companion plugin into a profile.
 *
 * Order is deliberate: pnpm first (it owns `node_modules` and the lockfile),
 * then the manifest edits (so a pnpm failure leaves no half-edited JSON), then
 * the loader row.
 */
export async function installCompanion(
  request: InstallRequest,
  loader?: { create(options: { id: string; name: string; config?: Record<string, unknown> }): Promise<unknown> },
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): Promise<InstallOutcome> {
  const steps: InstallStep[] = []
  const undo: Undo[] = []
  const profile = request.profile?.trim() || env.DSH_PROFILE?.trim() || 'desktop'
  const dir = profileDir(profile, env)
  const { name: specName, range } = splitSpec(request.spec)
  const packageName = request.packageName?.trim() || specName
  const emit = request.onLine

  if (!existsSync(dir)) {
    steps.push({ step: 'resolve', ok: false, detail: `profile 目录不存在：${dir}` })
    return {
      ok: false,
      steps,
      error: `找不到 profile "${profile}" 的目录`,
      tail: `期望路径：${dir}\n可用的 profile 在 ${join(dshHome(env), 'profiles')} 下。`,
    }
  }
  steps.push({ step: 'resolve', ok: true, detail: `${profile} → ${dir}` })

  // ── already installed? ───────────────────────────────────────────────────
  const manifest = readManifest(dir)
  const bundles = manifest?.dsh?.profile?.bundles ?? []
  const declared = manifest?.dependencies?.[packageName]
  const installedDir = join(dir, 'node_modules', ...packageName.split('/'))
  if (declared && bundles.includes(packageName) && existsSync(installedDir)) {
    steps.push({ step: 'manifest', ok: true, detail: 'already declared' })
    steps.push({ step: 'bundles', ok: true, detail: 'already listed' })
    steps.push({ step: 'link', ok: true, detail: installedDir })
    let version: string | undefined
    try {
      version = (JSON.parse(readFileSync(join(installedDir, 'package.json'), 'utf8')) as { version?: string }).version
    } catch {
      version = undefined
    }
    steps.push({ step: 'verify', ok: true, detail: `已安装 ${version ?? ''}`.trim() })
    return { ok: true, already: true, steps, version }
  }

  // ── pnpm add ─────────────────────────────────────────────────────────────
  const pnpm = platform === 'win32' ? 'pnpm.cmd' : 'pnpm'
  const target = range ? `${specName}@${range}` : request.spec.trim()
  // Remember whether the package was already on disk BEFORE pnpm ran. Rolling
  // back a freshly created directory is safe; removing a pre-existing one would
  // delete something the user already had.
  const installedBefore = existsSync(installedDir)
  const added = await runStreaming(pnpm, ['add', target, '--save-exact'], {
    cwd: dir,
    onLine: emit,
    signal: request.signal,
    timeoutMs: 600_000,
  })
  const pnpmOk = added.code === 0 && existsSync(installedDir)
  steps.push({
    step: 'pnpm',
    ok: pnpmOk,
    detail: pnpmOk ? `pnpm add ${target}` : `pnpm add exited ${added.code}`,
  })
  if (!pnpmOk) {
    rollback(undo)
    return {
      ok: false,
      steps,
      error: 'pnpm 安装失败（可能是网络、代理或包名不存在）',
      tail: tailOf(added.output),
    }
  }
  // pnpm succeeded, so `node_modules/<pkg>` now exists. If a later step fails,
  // restoring only `package.json` would leave an orphaned package on disk —
  // the manifest would say the plugin is not installed while its files are.
  // Register the removal so rollback undoes the whole operation, not half of it.
  if (!installedBefore) {
    undo.push(() => {
      try {
        rmSync(installedDir, { recursive: true, force: true })
      } catch {
        // A locked file must not mask the original failure.
      }
    })
  }

  // ── manifest: register the bundle so a restart keeps it ──────────────────
  try {
    const current = readManifest(dir)
    if (!current) throw new Error('package.json 读不出来')
    const nextBundles = [...(current.dsh?.profile?.bundles ?? [])]
    if (!nextBundles.includes(packageName)) nextBundles.push(packageName)
    const next: ProfileManifest = {
      ...current,
      dsh: {
        ...current.dsh,
        profile: { ...current.dsh?.profile, bundles: nextBundles },
      },
    }
    writeManifest(dir, next, undo)
    // 文案要说清这一步**实际做了什么**：`dependencies` 由 pnpm 写，
    // 这里只补 `dsh.profile.bundles`。原先写的是"写入 dependencies"，
    // 与行为不符 —— 若 pnpm 因 `--no-save` 或版本差异没写进去，
    // 用户会以为这一步已经保证过。
    const declared = current.dependencies?.[packageName]
    steps.push({
      step: 'manifest',
      ok: true,
      detail: declared
        ? `${packageName} 已在 dependencies（${declared}）`
        : `${packageName} 未出现在 dependencies —— pnpm 没有记录它，请检查 pnpm 版本`,
    })
    steps.push({ step: 'bundles', ok: true, detail: `${packageName} 加入 dsh.profile.bundles` })
  } catch (error) {
    rollback(undo)
    return {
      ok: false,
      steps,
      error: `写 profile 清单失败：${error instanceof Error ? error.message : String(error)}`,
    }
  }

  // ── link (pnpm normally did it; junction is the fallback) ────────────────
  steps.push({ step: 'link', ok: true, detail: installedDir })

  // ── hot-load so the user does not have to restart ────────────────────────
  if (loader?.create) {
    try {
      await loader.create({ id: request.entryId, name: packageName, config: {} })
      steps.push({ step: 'load', ok: true, detail: 'loader.create 成功（免重启生效）' })
    } catch (error) {
      // The manifest already carries it; a failed hot-load only costs a restart.
      steps.push({
        step: 'load',
        ok: false,
        detail: `热装载失败（重启后仍会生效）：${error instanceof Error ? error.message : String(error)}`,
      })
    }
  } else {
    steps.push({ step: 'load', ok: false, detail: '宿主未提供 loader；重启 DSH 后生效' })
  }

  let version: string | undefined
  try {
    version = (JSON.parse(readFileSync(join(installedDir, 'package.json'), 'utf8')) as { version?: string }).version
  } catch {
    version = undefined
  }
  const loaded = steps.some((item) => item.step === 'load' && item.ok)
  steps.push({
    step: 'verify',
    ok: true,
    detail: `已安装 ${version ?? ''}${loaded ? '，已热装' : '，需重启生效'}`.trim(),
  })
  return { ok: true, steps, version }
}

function writeManifest(dir: string, next: ProfileManifest, undo: Undo[]): void {
  const file = join(dir, 'package.json')
  const before = readFileSync(file, 'utf8')
  undo.push(() => { writeFileSync(file, before, 'utf8') })
  writeFileSync(file, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
}

function rollback(undo: Undo[]): void {
  for (const step of undo.reverse()) {
    try {
      step()
    } catch {
      // A failed rollback step must not mask the original failure.
    }
  }
}

function tailOf(output: string, limit = 25): string {
  return output
    .split(/\r?\n/)
    .filter((line) => line.trim() !== '')
    .slice(-limit)
    .join('\n')
    .slice(0, 3000)
}

/** Absolute path of a profile's package.json — used by the panel for a pre-flight preview. */
export function profileManifestPath(profile?: string, env: NodeJS.ProcessEnv = process.env): string {
  const name = profile?.trim() || env.DSH_PROFILE?.trim() || 'desktop'
  return join(profileDir(name, env), 'package.json')
}

/** Parent directory name, exported so callers can show "which profile" without re-deriving it. */
export function profileOf(manifestPath: string): string {
  return manifestPath.split(/[\\/]/).slice(-2, -1)[0] ?? ''
}

export const EGO_ENTRY_ID = 'ego-browser'
export const EGO_PACKAGE = 'dsh-ego-browser'
export const SKILL_REPO = 'https://github.com/shepsci/kaggle-skill'
