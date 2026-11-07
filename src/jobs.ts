/**
 * src/jobs.ts — bounded, cancellable job logs behind one-click actions.
 *
 * A panel click starts a multi-minute operation (venv creation, pip, a full
 * `kaggle_run`). The HTTP request that triggered it must return immediately,
 * and the panel then polls a job id. This is the smallest structure that makes
 * that true: a ring buffer of lines plus a done flag, with an auto-expiry so a
 * forgotten job does not pin memory for the life of the process.
 */

export interface Job {
  id: string
  kind: string
  lines: string[]
  done: boolean
  ok: boolean
  startedAt: number
  finishedAt?: number
  /** Called when a job is cancelled; implementations kill their child. */
  cancel?: () => void
}

const MAX_LINES = 2000
const MAX_LINE = 4_000
/** Finished jobs stay readable for this long, then are reaped. */
const LINGER_MS = 15 * 60 * 1000

export interface JobBoard {
  start(kind: string, cancel?: () => void): JobWithSink
  get(id: string): Job | undefined
  list(): Job[]
  cancel(id: string): boolean
  /** Kill every running job; called from host teardown. */
  cancelAll(): void
}

export interface JobWithSink extends Job {
  push(line: string): void
  finish(ok: boolean): void
}

let counter = 0

export function createJobBoard(now: () => number = Date.now): JobBoard {
  const jobs = new Map<string, JobWithSink>()

  const reap = (): void => {
    const at = now()
    for (const [id, job] of jobs) {
      if (job.done && job.finishedAt !== undefined && at - job.finishedAt > LINGER_MS) jobs.delete(id)
    }
  }

  const board: JobBoard = {
    start(kind, cancel) {
      counter += 1
      const job: JobWithSink = {
        id: `job-${counter}`,
        kind,
        lines: [],
        done: false,
        ok: false,
        startedAt: now(),
        cancel,
        push(line: string) {
          if (job.done) return
          const text = line.length > MAX_LINE ? `${line.slice(0, MAX_LINE)}…` : line
          job.lines.push(text)
          // Ring buffer: drop from the head so the newest lines always survive.
          if (job.lines.length > MAX_LINES) job.lines.splice(0, job.lines.length - MAX_LINES)
        },
        finish(ok: boolean) {
          if (job.done) return
          job.done = true
          job.ok = ok
          job.finishedAt = now()
        },
      }
      jobs.set(job.id, job)
      reap()
      return job
    },
    get(id) {
      return jobs.get(id)
    },
    list() {
      reap()
      return [...jobs.values()]
    },
    cancel(id) {
      const job = jobs.get(id)
      if (!job || job.done) return false
      try {
        job.cancel?.()
      } catch {
        // A cancel hook that throws must not leave the job marked running.
      }
      job.push('[kauto] 已请求取消')
      job.finish(false)
      return true
    },
    cancelAll() {
      for (const job of jobs.values()) {
        if (job.done) continue
        try {
          job.cancel?.()
        } catch {
          // ignore
        }
        job.finish(false)
      }
    },
  }
  return board
}

/** Paths the panel polls; kept here so routes and the client agree. */
export const JOB_ROUTES = {
  list: '/api/yuan-kaggle/jobs',
  one: (id: string) => `/api/yuan-kaggle/job/${encodeURIComponent(id)}`,
  cancel: (id: string) => `/api/yuan-kaggle/job/${encodeURIComponent(id)}/cancel`,
} as const

/**
 * Incremental read: only hand back lines the caller has not seen.
 *
 * The completion flag is `succeeded`, not `ok` — the HTTP envelope already uses
 * `ok` for "the request was served", and collapsing the two makes a job that
 * ran and failed indistinguishable from a route that errored.
 */
export function readJobSlice(job: Job, offset: number): { lines: string[]; nextOffset: number; done: boolean; succeeded: boolean } {
  const start = Math.max(0, Math.min(job.lines.length, Math.floor(offset) || 0))
  return {
    lines: job.lines.slice(start),
    nextOffset: job.lines.length,
    done: job.done,
    succeeded: job.ok,
  }
}
