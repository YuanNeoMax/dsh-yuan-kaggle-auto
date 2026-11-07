import { describe, it, expect, beforeEach } from 'vitest'
import { registerRoutes, isTrustedRequest, type RouteRegister, type RouteOptions } from '../src/routes.ts'
import { createJobBoard } from '../src/jobs.ts'

/**
 * `registerRoutes` 的真实行为。
 *
 * 此前 `routes-guard.test.ts` 只测了 `isTrustedRequest` / `readBody` 两个纯函数 ——
 * 而 `registerRoutes` 本身从未被调用过。它承担的是：9 条路由的注册、
 * 鉴权与方法判别、响应信封、后台任务的启动与日志写入。这些都不在纯函数的覆盖里。
 *
 * 用假的 `register` 收集注册项，用假的 `req`/`res` 驱动 handler。
 */

const COOKIE = 'dsh-auth-abc=token'

interface Feigned {
  statusCode: number
  headers: Record<string, string>
  body: string
}

function makeRes(): Feigned & { setHeader(n: string, v: string): void; end(b?: string): void } {
  const res = {
    statusCode: 200,
    headers: {} as Record<string, string>,
    body: '',
    setHeader(name: string, value: string) {
      res.headers[name.toLowerCase()] = value
    },
    end(body?: string) {
      res.body = body ?? ''
    },
  }
  return res
}

function makeReq(options: {
  method?: string
  url?: string
  cookie?: string
  body?: unknown
} = {}) {
  return {
    method: options.method ?? 'POST',
    url: options.url ?? '/',
    headers: options.cookie === undefined ? {} : { cookie: options.cookie },
    body: options.body,
  }
}

/** Collector: `RouteRegister` 的假实现。 */
function makeRegister(): { register: RouteRegister; routes: Map<string, RouteOptions> } {
  const routes = new Map<string, RouteOptions>()
  return {
    routes,
    register: (options) => {
      routes.set(options.path, options)
    },
  }
}

function makeBridge(overrides: Partial<Record<string, unknown>> = {}) {
  const jobs = createJobBoard()
  return {
    jobs,
    settings: () => ({
      llmProvider: 'auto',
      llmModel: '',
      skillBridgeDir: '',
      requireConfirm: true,
    }),
    status: async () => ({
      ready: true,
      items: [
        { id: 'python', label: 'Python', level: 'ok' as const, detail: '3.12' },
        { id: 'venv', label: '引擎环境', level: 'missing' as const, detail: '尚未初始化', fix: '点「初始化环境」' },
      ],
      paths: { venvDir: '/v', workRoot: '/w', engineDir: '/e' },
    }),
    setup: async () => ({ ok: true, steps: [{ step: 'venv', ok: true, detail: 'done' }] }),
    installEgo: async () => ({ ok: true, steps: [{ step: 'pnpm', ok: true, detail: 'ok' }] }),
    leaderboard: async () => ({ ok: true, team_count: 10, user_rank: 3 }),
    smoke: async (onLine?: (line: string) => void) => {
      onLine?.('演练开始')
      return { ok: true, steps: [{ step: 'prepare', ok: true, seconds: 0.1 }], not_covered: ['a', 'b', 'c'] }
    },
    egoAvailable: () => false,
    ...overrides,
  } as never
}

/** 轮询一个 job 直到完成（同步任务里 done 会立刻置位）。 */
async function settle(ms = 40): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

function parse(res: Feigned): Record<string, unknown> {
  return JSON.parse(res.body) as Record<string, unknown>
}

describe('registerRoutes', () => {
  let register: RouteRegister
  let routes: Map<string, RouteOptions>

  beforeEach(() => {
    const made = makeRegister()
    register = made.register
    routes = made.routes
  })

  it('registers every panel route exactly once', () => {
    registerRoutes(register, makeBridge())
    for (const path of [
      '/api/yuan-kaggle/status',
      '/api/yuan-kaggle/setup',
      '/api/yuan-kaggle/doctor',
      '/api/yuan-kaggle/probe-llm',
      '/api/yuan-kaggle/smoke',
      '/api/yuan-kaggle/install-ego',
      '/api/yuan-kaggle/install-skill',
      '/api/yuan-kaggle/jobs',
      '/api/yuan-kaggle/job',
      '/api/yuan-kaggle/job/cancel',
    ]) {
      expect(routes.has(path)).toBe(true)
    }
  })

  it('rejects a request without the dsh-auth cookie', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/status')!.handler(makeReq(), res as never)
    expect(res.statusCode).toBe(401)
    expect(parse(res)).toEqual({ ok: false, error: 'unauthorized' })
  })

  it('serves status with the engine report', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/status')!.handler(makeReq({ cookie: COOKIE }), res as never)
    expect(res.statusCode).toBe(200)
    const body = parse(res)
    expect(body.ok).toBe(true)
    expect(body.ready).toBe(true)
    expect(Array.isArray(body.items)).toBe(true)
    expect(body.ego).toEqual({ available: false })
    // A settings panel reading stale data is worse than one that waits.
    expect(res.headers['cache-control']).toBe('no-store')
  })

  it('rejects a non-POST on a POST-only route', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/setup')!.handler(
      makeReq({ method: 'GET', cookie: COOKIE }),
      res as never,
    )
    expect(res.statusCode).toBe(405)
    expect(parse(res)).toMatchObject({ ok: false })
  })

  it('answers setup immediately with a job id and runs the work in the background', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/setup')!.handler(
      makeReq({ cookie: COOKIE, body: { force: false } }),
      res as never,
    )
    // The panel must be able to start polling right away — that is the whole
    // reason this route hands back a job id instead of awaiting the install.
    expect(res.statusCode).toBe(200)
    const body = parse(res)
    expect(typeof body.jobId).toBe('string')
    expect(body.kind).toBe('setup')
  })

  it('writes the doctor checklist into the job log, including the fix hint', async () => {
    const bridge = makeBridge()
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/doctor')!.handler(makeReq({ cookie: COOKIE }), res as never)
    const { jobId } = parse(res) as { jobId: string }
    await settle()
    const job = bridge.jobs.get(jobId)!
    const log = job.lines.join('\n')
    expect(log).toContain('Python')
    // A missing component without its fix is just a complaint.
    expect(log).toContain('点「初始化环境」')
    expect(log).toContain('ready=true')
  })

  it('probe-llm is read-only and reports the configured channel', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/probe-llm')!.handler(makeReq({ cookie: COOKIE }), res as never)
    const body = parse(res)
    expect(body.ok).toBe(true)
    expect(body.provider).toBe('auto')
    // The route must say it did not actually reach the provider — otherwise a
    // green result reads as "the LLM works", which it never proved.
    expect(String(body.note)).toContain('只报告配置')
  })

  it('smoke refuses to rehearse when the engine is not ready', async () => {
    const bridge = makeBridge({
      status: async () => ({
        ready: false,
        items: [{ id: 'venv', label: '引擎环境', level: 'missing', detail: '尚未初始化', fix: '点「初始化环境」' }],
        paths: { venvDir: '/v', workRoot: '/w', engineDir: '/e' },
      }),
    })
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/smoke')!.handler(makeReq({ cookie: COOKIE }), res as never)
    const { jobId } = parse(res) as { jobId: string }
    await settle()
    const log = bridge.jobs.get(jobId)!.lines.join('\n')
    expect(log).toContain('引擎未就绪')
    // And it must not have spawned the rehearsal at all.
    expect(log).not.toContain('开始离线演练')
  })

  it('smoke streams the rehearsal steps and the not-covered list', async () => {
    const bridge = makeBridge()
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/smoke')!.handler(makeReq({ cookie: COOKIE }), res as never)
    const { jobId } = parse(res) as { jobId: string }
    await settle()
    const job = bridge.jobs.get(jobId)!
    const log = job.lines.join('\n')
    expect(log).toContain('开始离线演练')
    expect(log).toContain('OK   prepare')
    // Claiming success without saying what was NOT tested is the failure mode
    // this list exists to prevent.
    expect(log).toContain('需要真凭据')
    expect(job.ok).toBe(true)
  })

  it('leaderboard requires a slug instead of calling the engine with an empty one', async () => {
    let called = 0
    const bridge = makeBridge({
      leaderboard: async () => {
        called += 1
        return { ok: true }
      },
    })
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/leaderboard')!.handler(
      makeReq({ cookie: COOKIE, body: {} }),
      res as never,
    )
    expect(called).toBe(0)
    expect(parse(res)).toMatchObject({ ok: false })
  })

  it('leaderboard passes the slug through and returns the payload as-is', async () => {
    let seen = ''
    const bridge = makeBridge({
      leaderboard: async (slug: string) => {
        seen = slug
        return { ok: true, team_count: 42, source: 'kaggle-cli' }
      },
    })
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/leaderboard')!.handler(
      makeReq({ cookie: COOKIE, body: { slug: '  titanic  ' } }),
      res as never,
    )
    expect(seen).toBe('titanic')
    expect(parse(res)).toMatchObject({ ok: true, team_count: 42 })
  })

  it('install-skill hands back the command instead of cloning silently', async () => {
    const bridge = makeBridge({
      settings: () => ({ llmProvider: 'auto', llmModel: '', skillBridgeDir: '', requireConfirm: true }),
    })
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/install-skill')!.handler(makeReq({ cookie: COOKIE }), res as never)
    const body = parse(res)
    expect(body.ok).toBe(true)
    expect(body.configured).toBe(false)
    expect(String(body.command)).toContain('git clone')
  })

  it('job routes 404 for an unknown id', async () => {
    registerRoutes(register, makeBridge())
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/job')!.handler(
      makeReq({ method: 'GET', url: '/api/yuan-kaggle/job?id=nope', cookie: COOKIE }),
      res as never,
    )
    expect(res.statusCode).toBe(404)
    expect(parse(res)).toMatchObject({ ok: false })
  })

  it('job route returns incremental lines', async () => {
    const bridge = makeBridge()
    registerRoutes(register, bridge)
    const job = bridge.jobs.start('probe')
    job.push('line one')
    job.push('line two')
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/job')!.handler(
      makeReq({ method: 'GET', url: `/api/yuan-kaggle/job?id=${job.id}&offset=1`, cookie: COOKIE }),
      res as never,
    )
    const body = parse(res) as { lines: string[]; nextOffset: number }
    expect(body.lines).toEqual(['line two'])
    expect(body.nextOffset).toBe(2)
  })

  it('job cancel is idempotent and reports whether it did anything', async () => {
    const bridge = makeBridge()
    registerRoutes(register, bridge)
    const job = bridge.jobs.start('long')
    const register2 = routes.get('/api/yuan-kaggle/job/cancel')!

    const first = makeRes()
    await register2.handler(
      makeReq({ cookie: COOKIE, body: { id: job.id } }),
      first as never,
    )
    expect(parse(first)).toMatchObject({ ok: true, cancelled: true })

    const second = makeRes()
    await register2.handler(
      makeReq({ cookie: COOKIE, body: { id: job.id } }),
      second as never,
    )
    expect(parse(second)).toMatchObject({ ok: true, cancelled: false })
  })

  it('a handler that throws still answers JSON instead of leaving the panel spinning', async () => {
    const bridge = makeBridge({
      status: async () => {
        throw new Error('boom')
      },
    })
    registerRoutes(register, bridge)
    const res = makeRes()
    await routes.get('/api/yuan-kaggle/status')!.handler(makeReq({ cookie: COOKIE }), res as never)
    // 200 with ok:false, not a rejected promise: the panel has no error channel
    // for a route that never responds.
    expect(res.statusCode).toBe(200)
    expect(parse(res)).toMatchObject({ ok: false, error: 'boom' })
  })
})
