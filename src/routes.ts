/**
 * src/routes.ts — the HTTP surface behind the settings panel.
 *
 * Same guard as sibling plugins: the DSH web shell hands every browser session
 * a `dsh-auth-*` cookie, and a request without one is not from the app. That
 * keeps these routes from being a CSRF hole on a machine that exposes the port.
 *
 * Every handler answers JSON and never throws: a route that rejects an error
 * leaves the panel spinning with no explanation.
 */
import type { JobBoard } from './jobs.ts'
import { readJobSlice } from './jobs.ts'
import type { EngineStatus } from './setup.ts'
import type { InstallOutcome } from './installer.ts'

export const BASE = '/api/yuan-kaggle'
export const STATUS_ROUTE = `${BASE}/status`
export const SETUP_ROUTE = `${BASE}/setup`
export const DOCTOR_ROUTE = `${BASE}/doctor`
export const PROBE_LLM_ROUTE = `${BASE}/probe-llm`
export const SMOKE_ROUTE = `${BASE}/smoke`
export const INSTALL_EGO_ROUTE = `${BASE}/install-ego`
export const LEADERBOARD_ROUTE = `${BASE}/leaderboard`
export const INSTALL_SKILL_ROUTE = `${BASE}/install-skill`
export const JOBS_ROUTE = `${BASE}/jobs`

export interface RouteRequest {
  method?: string
  url?: string
  headers: Record<string, string | string[] | undefined>
  body?: unknown
  on?: (event: string, listener: (...args: unknown[]) => void) => void
}

export interface RouteResponse {
  statusCode: number
  setHeader(name: string, value: string): void
  end(body?: string): void
}

/**
 * The host's registrar takes an object, not `(path, handler)`:
 *   register({ path, handler })
 * Confirmed against dsh-ego-browser/lib/index.js:2489, which wraps the original
 * `register` as `rawRegister({ ...opts, handler })`.
 */
export interface RouteOptions {
  path: string
  handler: (req: RouteRequest, res: RouteResponse) => Promise<void> | void
}

export type RouteRegister = (options: RouteOptions) => void

/** What the routes need from the host half. */
export interface RouteBridge {
  settings(): { llmProvider: string; llmModel: string; skillBridgeDir: string; requireConfirm: boolean }
  status(): Promise<EngineStatus>
  setup(options?: { force?: boolean }): Promise<unknown>
  installEgo(options?: { version?: string }): Promise<InstallOutcome>
  /** Read-only leaderboard probe; returns the engine's JSON verbatim. */
  leaderboard(slug: string, top?: number): Promise<Record<string, unknown>>
  /** Runs the offline rehearsal; `onLine` receives progress text. */
  smoke(onLine?: (line: string) => void): Promise<{
    ok?: boolean
    steps?: Array<{ step: string; ok: boolean; seconds?: number; error?: string }>
    hint?: string
    not_covered?: string[]
  }>
  egoAvailable(): boolean
  jobs: JobBoard
}

function json(res: RouteResponse, status: number, body: unknown): void {
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(JSON.stringify(body))
}

/**
 * Trusted only when the request carries a DSH session cookie.
 *
 * Permissive about *which* cookie on purpose: the shell's cookie name has a
 * per-install suffix, so the `dsh-auth-` prefix is the stable contract.
 */
export function isTrustedRequest(req: RouteRequest): boolean {
  const raw = req.headers.cookie
  const cookie = Array.isArray(raw) ? raw.join('; ') : String(raw ?? '')
  return /(?:^|;\s*)dsh-auth-[^=]+=/.test(cookie)
}

/** Body as JSON, from whatever shape the host handed us. */
export function readBody(req: RouteRequest, timeoutMs = 5_000): Promise<Record<string, unknown>> {
  if (req.body !== undefined && req.body !== null && typeof req.body === 'object') {
    return Promise.resolve(req.body as Record<string, unknown>)
  }
  if (typeof req.on !== 'function') return Promise.resolve({})
  return new Promise((resolve) => {
    let text = ''
    let settled = false
    const done = (): void => {
      if (settled) return
      settled = true
      if (!text.trim()) {
        resolve({})
        return
      }
      try {
        const parsed = JSON.parse(text) as unknown
        resolve(parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {})
      } catch {
        resolve({})
      }
    }
    req.on?.('data', (chunk: unknown) => {
      text += String(chunk)
      if (text.length > 64_000) text = text.slice(0, 64_000)
    })
    req.on?.('end', done)
    req.on?.('error', done)
    setTimeout(done, timeoutMs)
  })
}

/**
 * Register every panel route. Returns a disposer.
 *
 * A host without a web server never reaches here — the caller checks for the
 * service first, so this is a pure web-mode addition.
 */
export function registerRoutes(
  register: RouteRegister,
  bridge: RouteBridge,
): () => void {
  const registered: string[] = []

  const guarded = (
    handler: (req: RouteRequest, res: RouteResponse) => Promise<void>,
  ): ((req: RouteRequest, res: RouteResponse) => Promise<void>) =>
    async (req, res) => {
      if (!isTrustedRequest(req)) {
        json(res, 401, { ok: false, error: 'unauthorized' })
        return
      }
      try {
        await handler(req, res)
      } catch (error) {
        json(res, 200, { ok: false, error: error instanceof Error ? error.message : String(error) })
      }
    }

  const onlyPost = (
    handler: (req: RouteRequest, res: RouteResponse) => Promise<void>,
  ): ((req: RouteRequest, res: RouteResponse) => Promise<void>) =>
    guarded(async (req, res) => {
      const method = (req.method ?? 'GET').toUpperCase()
      if (method !== 'POST') {
        json(res, 405, { ok: false, error: 'POST only' })
        return
      }
      await handler(req, res)
    })

  /** Run `task` after the response so the panel can start polling immediately. */
  const background = (
    kind: string,
    task: (job: { push: (line: string) => void; finish: (ok: boolean) => void }) => Promise<boolean>,
    res: RouteResponse,
  ): void => {
    const job = bridge.jobs.start(kind)
    json(res, 200, { ok: true, jobId: job.id, kind })
    void task(job)
      .then((ok) => job.finish(ok))
      .catch((error: unknown) => {
        job.push(error instanceof Error ? error.message : String(error))
        job.finish(false)
      })
  }

  const add = (path: string, handler: (req: RouteRequest, res: RouteResponse) => Promise<void> | void): void => {
    registered.push(path)
    register({ path, handler })
  }

  add(
    STATUS_ROUTE,
    guarded(async (_req, res) => {
      const status = await bridge.status()
      json(res, 200, { ok: true, ...status, ego: { available: bridge.egoAvailable() } })
    }),
  )

  add(
    SETUP_ROUTE,
    onlyPost(async (req, res) => {
      const body = await readBody(req)
      background(
        'setup',
        async (job) => {
          job.push('开始初始化引擎环境（venv + 依赖）')
          const outcome = await bridge.setup({ force: body.force === true })
          const record = outcome as { ok?: boolean; steps?: Array<{ step: string; ok: boolean; detail?: string }>; tail?: string }
          job.push(JSON.stringify(outcome, null, 2).split('\n').slice(0, 60).join('\n'))
          for (const step of record.steps ?? []) {
            job.push(`${step.ok ? 'OK  ' : 'FAIL'} ${step.step}: ${step.detail ?? ''}`)
          }
          if (record.tail) job.push(`--- 输出尾部 ---\n${record.tail}`)
          return Boolean(record.ok)
        },
        res,
      )
    }),
  )

  add(
    DOCTOR_ROUTE,
    onlyPost(async (_req, res) => {
      background(
        'doctor',
        async (job) => {
          const status = await bridge.status()
          for (const item of status.items) {
            job.push(`${item.level === 'ok' ? 'OK  ' : item.level === 'warn' ? 'WARN' : 'MISS'} ${item.label}: ${item.detail ?? ''}`)
            if (item.fix) job.push(`    → ${item.fix}`)
          }
          job.push(`ready=${status.ready}`)
          return status.ready
        },
        res,
      )
    }),
  )

  add(
    PROBE_LLM_ROUTE,
    onlyPost(async (_req, res) => {
      // Read-only: report the configured channel, never spend a token.
      const settings = bridge.settings()
      json(res, 200, {
        ok: true,
        provider: settings.llmProvider,
        model: settings.llmModel || '(auto)',
        note: '这一项只报告配置；真实连通性在第一次 iterate 时才会被验证。',
      })
    }),
  )

  add(
    SMOKE_ROUTE,
    onlyPost(async (_req, res) => {
      // A real rehearsal, not a status read.
      //
      // The previous version only called `bridge.status()` and printed the same
      // checklist the panel already shows — the button was named "smoke" but ran
      // no modelling step, so a user who clicked it still had no evidence the
      // pipeline works on this machine. The engine's `smoke` command now runs
      // prepare → baseline → write-sub → validate against a bundled fixture,
      // which exercises Python, the dependencies, the splitter, LightGBM and the
      // submission shape without touching Kaggle or spending a quota slot.
      background(
        'smoke',
        async (job) => {
          const status = await bridge.status()
          if (!status.ready) {
            job.push('引擎未就绪：先点「初始化环境」')
            for (const item of status.items) {
              job.push(`${item.label}: ${item.detail ?? item.level}`)
              if (item.fix) job.push(`    → ${item.fix}`)
            }
            return false
          }
          job.push('开始离线演练（prepare → baseline → write-sub → validate）')
          const result = await bridge.smoke((line) => job.push(line))
          for (const step of result.steps ?? []) {
            job.push(`${step.ok ? 'OK  ' : 'FAIL'} ${step.step}  ${step.seconds ?? '?'}s${step.error ? `  ${step.error}` : ''}`)
          }
          if (!result.ok && result.hint) job.push(`→ ${result.hint}`)
          if (result.ok) {
            job.push('')
            job.push('管线在本机可用。以下三项需要真凭据，由「体检」与第一次真实提交覆盖：')
            for (const item of result.not_covered ?? []) job.push(`  · ${item}`)
          }
          return Boolean(result.ok)
        },
        res,
      )
    }),
  )

  add(
    INSTALL_EGO_ROUTE,
    onlyPost(async (req, res) => {
      const body = await readBody(req)
      background(
        'install-ego',
        async (job) => {
          job.push('安装 dsh-ego-browser（可选组件）')
          const outcome = await bridge.installEgo({
            version: typeof body.version === 'string' && body.version.trim() ? body.version.trim() : undefined,
          })
          for (const step of outcome.steps) {
            job.push(`${step.ok ? 'OK  ' : 'FAIL'} ${step.step}: ${step.detail ?? ''}`)
          }
          if (outcome.error) job.push(`error: ${outcome.error}`)
          if (outcome.tail) job.push(`--- 输出尾部 ---\n${outcome.tail}`)
          return outcome.ok
        },
        res,
      )
    }),
  )

  add(
    INSTALL_SKILL_ROUTE,
    onlyPost(async (_req, res) => {
      // A git checkout into a user-chosen directory. Cloning 5 MB silently is
      // worse than handing the user the exact command, so this route reports
      // the plan instead of performing it.
      const settings = bridge.settings()
      const dir = settings.skillBridgeDir.trim()
      json(res, 200, {
        ok: true,
        configured: dir !== '',
        dir,
        command: dir
          ? `git clone --depth 1 https://github.com/shepsci/kaggle-skill "${dir}"`
          : '先在设置里填写「skill 桥接目录」，再执行返回的 git clone 命令',
      })
    }),
  )

  add(
    LEADERBOARD_ROUTE,
    onlyPost(async (req, res) => {
      // 只读查询：不启动 job，直接把结果带回来（排行榜是一次网络往返，
      // 秒级完成，套上轮询反而让面板显得迟钝）。
      const body = await readBody(req)
      const slug = String(body.slug ?? '').trim()
      if (!slug) {
        json(res, 200, { ok: false, error: '需要 slug（比赛标识，如 titanic）' })
        return
      }
      const result = await bridge.leaderboard(slug, Number(body.top ?? 10) || 10)
      json(res, 200, result)
    }),
  )

  add(
    JOBS_ROUTE,
    guarded(async (_req, res) => {
      json(res, 200, {
        ok: true,
        jobs: bridge.jobs.list().map((job) => ({
          id: job.id,
          kind: job.kind,
          done: job.done,
          ok: job.ok,
          lines: job.lines.length,
          startedAt: job.startedAt,
          finishedAt: job.finishedAt,
        })),
      })
    }),
  )

  // Parameterised job endpoints. The host registrar matches exact paths, so the
  // job id travels in the query string instead of a path segment — one route
  // shape the registrar definitely serves, and no path-template guessing.
  add(
    `${BASE}/job`,
    guarded(async (req, res) => {
      const query = new URLSearchParams((req.url ?? '').split('?')[1] ?? '')
      const id = query.get('id') ?? ''
      const job = bridge.jobs.get(id)
      if (!job) {
        json(res, 404, { ok: false, error: `no such job: ${id}` })
        return
      }
      const offset = Number(query.get('offset') ?? '0')
      json(res, 200, { ok: true, id, kind: job.kind, ...readJobSlice(job, offset) })
    }),
  )

  add(
    `${BASE}/job/cancel`,
    onlyPost(async (req, res) => {
      const query = new URLSearchParams((req.url ?? '').split('?')[1] ?? '')
      const body = await readBody(req)
      const id = String(body.id ?? query.get('id') ?? '')
      const cancelled = bridge.jobs.cancel(id)
      json(res, 200, { ok: true, cancelled, id })
    }),
  )

  return () => {
    // An unload drops the plugin fiber, which takes its routes with it; the
    // list is kept so a future registrar that returns disposers can use it.
    registered.length = 0
  }
}
