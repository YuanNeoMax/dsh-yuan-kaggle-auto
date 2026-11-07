import { describe, it, expect } from 'vitest'
import { DEFAULTS, resolveConfig } from '../src/config.ts'
import { planRounds } from '../src/tools.ts'

/**
 * `rounds` 的夹紧必须**可见**。
 *
 * `maxRounds` 这个配置存在是对的：否则模型可以要求跑 50 轮，白烧一小时本地算力。
 * 错的是夹紧是**静默**的 —— 引擎跑了 4 轮、调用方要求 10 轮，
 * 而返回里没有一个字提到这件事。
 *
 * 一个以为自己迭代了 10 次的模型，会基于它从未收集过的证据下结论。
 * 所以 `planRounds` 返回 `effective` 和一句点名两个数字的 `note`。
 */

const settings = (patch: Record<string, unknown> = {}) => resolveConfig({ ...DEFAULTS, ...patch })

describe('planRounds', () => {
  it('passes a request through when it fits', () => {
    const plan = planRounds(settings({ maxRounds: 6 }), { rounds: 3 }, 1)
    expect(plan.effective).toBe(3)
    expect(plan.note).toBeUndefined()
  })

  it('clamps above the ceiling and says so', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 10 }, 1)
    expect(plan.effective).toBe(4)
    expect(plan.requested).toBe(10)
    expect(plan.note).toBeDefined()
    // The note must name BOTH numbers — "clamped" alone does not tell the caller
    // how much of what it asked for actually happened.
    expect(plan.note).toContain('10')
    expect(plan.note).toContain('4')
  })

  it('tells the caller how to raise the ceiling', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 10 }, 1)
    expect(plan.note).toMatch(/设置|上限/)
  })

  it('clamps below the floor and says so', () => {
    // `iterate` needs at least one round; zero there is a no-op call.
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 0 }, 1)
    expect(plan.effective).toBe(1)
    expect(plan.requested).toBe(0)
    expect(plan.note).toBeDefined()
  })

  it('allows 0 rounds for run (baseline-only is legitimate)', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 0 }, 0)
    expect(plan.effective).toBe(0)
    expect(plan.note).toBeUndefined()
  })

  it('falls back to the ceiling when rounds is absent or not a number', () => {
    for (const value of [undefined, null, 'three', NaN, Infinity]) {
      const plan = planRounds(settings({ maxRounds: 5 }), { rounds: value }, 1)
      expect(plan.effective).toBe(5)
      expect(plan.note).toBeUndefined()
    }
  })

  it('rounds a fractional request before comparing', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 3.6 }, 1)
    expect(plan.effective).toBe(4)
    expect(plan.note).toBeUndefined()
  })

  it('clamps a fractional request that exceeds the ceiling', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 4.7 }, 1)
    expect(plan.effective).toBe(4)
    expect(plan.requested).toBe(5)
    expect(plan.note).toBeDefined()
  })

  it('never exceeds the ceiling even if config is nonsense', () => {
    // resolveConfig already clamps maxRounds to 1..12; this guards the helper
    // itself so a direct call with an odd Settings object cannot overshoot.
    const odd = { ...settings(), maxRounds: 0 } as ReturnType<typeof settings>
    const plan = planRounds(odd, { rounds: 3 }, 1)
    expect(plan.effective).toBeLessThanOrEqual(1)
  })

  it('reports the ceiling itself when the caller asks for exactly it', () => {
    const plan = planRounds(settings({ maxRounds: 4 }), { rounds: 4 }, 1)
    expect(plan.effective).toBe(4)
    expect(plan.requested).toBe(4)
    expect(plan.note).toBeUndefined()
  })
})
