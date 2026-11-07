import { describe, it, expect } from 'vitest'
import { createJobBoard, readJobSlice } from '../src/jobs.ts'

describe('job board', () => {
  it('accumulates lines and reports completion', () => {
    const board = createJobBoard()
    const job = board.start('setup')
    job.push('one')
    job.push('two')
    expect(job.lines).toEqual(['one', 'two'])
    expect(job.done).toBe(false)
    job.finish(true)
    expect(job.done).toBe(true)
    expect(job.ok).toBe(true)
  })

  it('ignores pushes after completion', () => {
    const board = createJobBoard()
    const job = board.start('x')
    job.finish(true)
    job.push('late')
    expect(job.lines).toEqual([])
  })

  it('is idempotent on finish', () => {
    const board = createJobBoard()
    const job = board.start('x')
    job.finish(true)
    job.finish(false)
    expect(job.ok).toBe(true)
  })

  it('keeps the newest lines in a ring buffer', () => {
    const board = createJobBoard()
    const job = board.start('noisy')
    for (let i = 0; i < 2_500; i += 1) job.push(`line-${i}`)
    // The head is dropped, so the tail (which names the failure) always survives.
    expect(job.lines.length).toBe(2_000)
    expect(job.lines.at(-1)).toBe('line-2499')
    expect(job.lines[0]).toBe('line-500')
  })

  it('hands back only unseen lines', () => {
    const board = createJobBoard()
    const job = board.start('x')
    job.push('a')
    job.push('b')
    const first = readJobSlice(job, 0)
    expect(first.lines).toEqual(['a', 'b'])
    expect(first.nextOffset).toBe(2)
    job.push('c')
    const second = readJobSlice(job, first.nextOffset)
    expect(second.lines).toEqual(['c'])
  })

  it('reports success separately from request success', () => {
    const board = createJobBoard()
    const job = board.start('x')
    job.finish(false)
    const slice = readJobSlice(job, 0)
    // `succeeded` is the job's outcome; the HTTP envelope owns `ok`.
    expect(slice.succeeded).toBe(false)
    expect(slice.done).toBe(true)
  })

  it('cancels a running job through its hook', () => {
    let cancelled = false
    const board = createJobBoard()
    const job = board.start('long', () => { cancelled = true })
    expect(board.cancel(job.id)).toBe(true)
    expect(cancelled).toBe(true)
    expect(job.done).toBe(true)
    // Cancelling twice is not an error, just a no-op.
    expect(board.cancel(job.id)).toBe(false)
  })

  it('survives a cancel hook that throws', () => {
    const board = createJobBoard()
    const job = board.start('long', () => { throw new Error('kill failed') })
    expect(board.cancel(job.id)).toBe(true)
    expect(job.done).toBe(true)
  })

  it('cancels everything on teardown', () => {
    const board = createJobBoard()
    const a = board.start('a')
    const b = board.start('b')
    board.cancelAll()
    expect(a.done && b.done).toBe(true)
  })

  it('reaps finished jobs after the linger window', () => {
    let clock = 0
    const board = createJobBoard(() => clock)
    const job = board.start('x')
    job.finish(true)
    clock += 16 * 60 * 1000
    board.list()
    expect(board.get(job.id)).toBeUndefined()
  })
})
