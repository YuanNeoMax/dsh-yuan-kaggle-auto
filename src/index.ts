/**
 * src/index.ts — the host half: config, tools, routes, and the two installers.
 *
 * Nothing in here implements Kaggle or modelling semantics. It resolves settings
 * into paths, finds a Python, hands argv to the engine, and reports what came
 * back. That split is what keeps the 66-engine-test suite meaningful after the
 * plugin has been wrapped around it.
 */
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type {} from '@deepseek-ai/dsh-tools'
import { Config, DEFAULTS, ENTRY_ID, resolveConfig, type Settings } from './config.ts'
import { engineRootFor, resolvePaths, ENGINE_DIR, type ResolvedPaths } from './paths.ts'
import { findPython } from './python.ts'
import { venvPython } from './paths.ts'
import { installEngine, readStatus, type EngineStatus } from './setup.ts'
import { runEngine } from './runner.ts'
import { installCompanion, EGO_ENTRY_ID, EGO_PACKAGE, type InstallOutcome } from './installer.ts'
import { createJobBoard } from './jobs.ts'
import { registerRoutes, type RouteRegister } from './routes.ts'
import { registerTools, type ToolDeps } from './tools.ts'
import { promptText } from './prompt.ts'

export { Config, DEFAULTS, ENTRY_ID, resolveConfig }
export type { Settings }

export const name = 'dsh-yuan-kaggle-auto'
export const inject = ['tools']

/** Optional services are read through ctx.get, never required. */
interface HostServices {
  subprocess?: { spawn: ToolsSpawn }
  loader?: { create(options: { id: string; name: string; config?: Record<string, unknown> }): Promise<unknown> }
  webServer?: { register(route: { path: string; handler: RouteHandler }): void }
  systemPrompt?: unknown
}

type ToolsSpawn = NonNullable<ToolDeps['subprocess']>
type RouteHandler = (req: RouteRequest, res: RouteResponse) => Promise<void> | void

interface RouteRequest {
  method?: string
  url?: string
  headers: Record<string, string | string[] | undefined>
}

interface RouteResponse {
  statusCode: number
  setHeader(name: string, value: string): void
  end(body?: string): void
}

/** Serialise per-key so two calls do not write the same work/<slug> tree. */
function createLock(): (key: string) => Promise<() => void> {
  const chains = new Map<string, Promise<void>>()
  return async (key: string) => {
    const previous = chains.get(key) ?? Promise.resolve()
    let release!: () => void
    const current = new Promise<void>((resolve) => { release = resolve })
    chains.set(key, previous.then(() => current))
    await previous
    return () => {
      release()
      if (chains.get(key) === current) chains.delete(key)
    }
  }
}

export interface HostBridge {
  settings(): Settings
  paths(): ResolvedPaths
  status(): Promise<EngineStatus>
  setup(options?: { force?: boolean; onLine?: (line: { stream: string; text: string }) => void }): Promise<unknown>
  installEgo(options?: { version?: string }): Promise<InstallOutcome>
  /** Read-only leaderboard probe for the panel; resolves the engine's JSON. */
  leaderboard(slug: string, top?: number): Promise<Record<string, unknown>>
  /** Offline rehearsal: real pipeline against a bundled fixture, no quota spent. */
  smoke(onLine?: (line: string) => void): Promise<{
    ok?: boolean
    steps?: Array<{ step: string; ok: boolean; seconds?: number; error?: string }>
    hint?: string
    not_covered?: string[]
  }>
  egoAvailable(): boolean
  jobs: ReturnType<typeof createJobBoard>
}

/**
 * Apply: register tools, routes and the optional prompt contribution.
 *
 * Every registration is wrapped in `ctx.effect` where the service offers one,
 * so an unload unwinds routes and timers rather than leaking them across a
 * plugin reload (measured failure mode in sibling plugins).
 */
export function apply(ctx: Context, config: Record<string, unknown> = {}): void {
  const bridge = createBridge(ctx as unknown as HostServices & Context, config as unknown as Settings)
  const registrar = (ctx as unknown as { tools: { register(tool: unknown): void } }).tools
  registerTools((tool) => registrar.register(tool), {
    settings: bridge.settings,
    paths: bridge.paths,
    python: async () => {
      const known = bridge.lastPython
      if (known) return { ok: true as const, python: known }
      const settings = bridge.settings()
      const paths = bridge.paths()
      const found = await findPython(settings, process.env)
      if (!found.ok) return { ok: false as const, reason: found.reason, hint: found.hint }
      // Prefer the venv once it exists: it is the one with pandas and lightgbm.
      const venv = venvPython(paths.venvDir)
      const python = existsSync(venv) ? venv : found.candidate.command
      bridge.lastPython = python
      return { ok: true as const, python }
    },
    subprocess: (ctx as unknown as HostServices).subprocess as ToolsSpawn | undefined,
    egoAvailable: bridge.egoAvailable,
    setup: (force?: boolean) => bridge.setup({ force }),
    acquire: createLock(),
  })

  const services = ctx as unknown as HostServices
  const server = services.webServer
  if (server && typeof server.register === 'function') {
    // The host registrar takes `{ path, handler }`, not `(path, handler)` —
    // see RouteRegister in routes.ts for the reference.
    const register: RouteRegister = (options) => { server.register?.(options) }
    const dispose = registerRoutes(register, bridge)
    ;(ctx as unknown as { effect?: (fn: () => () => void) => void }).effect?.(() => dispose)
  }

  const prompt = promptText(bridge.settings())
  const systemPrompt = services.systemPrompt as { addSection?(text: string): () => void } | undefined
  if (prompt && typeof systemPrompt?.addSection === 'function') {
    const off = systemPrompt.addSection(prompt)
    ;(ctx as unknown as { effect?: (fn: () => () => void) => void }).effect?.(() => off)
  }
}

/** Everything the tools, routes and panel read from, in one place. */
function createBridge(ctx: HostServices & Context, raw: Settings): HostBridge & { lastPython?: string } {
  // The Loader owns the live config; `resolveConfig` unwraps volatile refs.
  const source = (): Record<string, unknown> => {
    const entry = (ctx as unknown as { fiber?: { entry?: { options?: { config?: unknown } } } })
      .fiber?.entry?.options?.config
    return (entry ?? raw) as Record<string, unknown>
  }
  const settings = (): Settings => resolveConfig(source())
  const paths = (): ResolvedPaths => resolvePaths(settings())
  const jobs = createJobBoard()

  const bridge: HostBridge & { lastPython?: string } = {
    settings,
    paths,
    jobs,
    status: async () => {
      const egoTools = (ctx as unknown as { get?(name: string): unknown }).get?.('tools') as
        | { has?(name: string): boolean }
        | undefined
      const egoAvailable = Boolean(egoTools?.has?.('ego_navigate'))
      return readStatus(settings(), paths(), { ego: { available: egoAvailable } })
    },
    setup: async (options = {}) => {
      const job = jobs.start('setup')
      try {
        const outcome = await installEngine(settings(), paths(), {
          force: options.force,
          onLine: (line) => job.push(line.text),
        })
        job.finish(outcome.ok)
        if (outcome.ok) bridge.lastPython = venvPython(paths().venvDir)
        return outcome
      } catch (error) {
        job.finish(false)
        throw error
      }
    },
    installEgo: async (options = {}) => {
      const job = jobs.start('install-ego')
      try {
        const version = options.version?.trim() || settings().egoVersion
        const outcome = await installCompanion(
          { spec: `${EGO_PACKAGE}@${version}`, entryId: EGO_ENTRY_ID, packageName: EGO_PACKAGE },
          (ctx as unknown as HostServices).loader,
          undefined,
          process.platform,
        )
        job.finish(outcome.ok)
        return outcome
      } catch (error) {
        job.finish(false)
        throw error
      }
    },
    leaderboard: async (slug: string, top = 10) => {
      // Read-only, one network round trip: no job, no lock. A panel that has to
      // poll for a leaderboard read feels broken even when it is not.
      const found = await findPython(settings(), process.env)
      if (!found.ok) {
        return { ok: false, error: found.reason, hint: found.hint, needsSetup: true }
      }
      const venv = venvPython(paths().venvDir)
      const python = existsSync(venv) ? venv : found.candidate.command
      const run = await runEngine(['leaderboard', slug, '--top', String(Math.max(1, top))], {
        python,
        paths: paths(),
        settings: settings(),
        subprocess: (ctx as unknown as HostServices).subprocess as ToolsSpawn | undefined,
      }, { timeoutMs: 120_000 })
      const value = run.value
      if (run.ok && value !== null && typeof value === 'object' && !Array.isArray(value)) {
        return { ok: true, ...(value as Record<string, unknown>) }
      }
      return {
        ok: run.ok,
        error: run.error,
        stdout: run.stdout.trim().slice(-1200) || undefined,
        stderr: run.stderr.trim().slice(-800) || undefined,
      }
    },
    smoke: async (onLine?: (line: string) => void) => {
      // Reuses `runEngine` so the rehearsal travels the same path a real run
      // does (same interpreter, same env contract, same cwd rule). A smoke test
      // that spawns differently from production would prove nothing about it.
      const found = await findPython(settings(), process.env)
      if (!found.ok) {
        return { ok: false, hint: found.reason, steps: [] }
      }
      const venv = venvPython(paths().venvDir)
      const python = existsSync(venv) ? venv : found.candidate.command
      const run = await runEngine(['smoke'], {
        python,
        paths: paths(),
        settings: settings(),
        subprocess: (ctx as unknown as HostServices).subprocess as ToolsSpawn | undefined,
      }, {
        // The rehearsal trains LightGBM three times over a 1200-row fixture;
        // measured ~5 s, so ten minutes is a generous ceiling for a slow machine.
        timeoutMs: 10 * 60 * 1000,
        onLine: onLine ? (line) => { if (line.text.trim()) onLine(line.text) } : undefined,
      })
      const value = run.value
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        return value as {
          ok?: boolean
          steps?: Array<{ step: string; ok: boolean; seconds?: number; error?: string }>
          hint?: string
          not_covered?: string[]
        }
      }
      return { ok: false, hint: run.error ?? 'smoke 未返回可解析结果', steps: [] }
    },
    egoAvailable: () => {
      const registry = (ctx as unknown as { get?(name: string): unknown }).get?.('tools') as
        | { has?(name: string): boolean }
        | undefined
      return Boolean(registry?.has?.('ego_navigate'))
    },
  }
  return bridge
}

export const ConfigSchema = Config as unknown as z<Record<string, unknown>>

/** Where the engine lives, exposed for the panel and for tests. */
export const engineDirectory = ENGINE_DIR

/** Root the engine will treat as writable, for the panel's "working directory" row. */
export function engineWritableRoot(workRoot: string): string {
  return engineRootFor(workRoot)
}

/** Convenience for the panel: is the shipped engine present at all? */
export function engineSourcesPresent(): boolean {
  return existsSync(join(ENGINE_DIR, 'kauto', 'cli.py'))
}
