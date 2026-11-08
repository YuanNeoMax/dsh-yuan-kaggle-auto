import { describe, it, expect } from 'vitest'
import { fromRun } from '../src/tools.ts'
import type { RunResult } from '../src/runner.ts'

/**
 * `fromRun` 是引擎输出到工具结果的唯一通道。
 *
 * ## 修的是什么
 *
 * 引擎在**失败路径上同样打印 JSON**：`kauto run` 某一步失败时会输出
 * `{"steps": [{"step": "prepare", "ok": false, "error": "..."}]}`。
 * 实测（用一个不存在的 slug 跑 `run`）确认过这一点。
 *
 * 而早先 `fromRun` 在 `ok: false` 时只截 stderr 尾部，把整个 JSON 丢掉。
 * 模型于是只看到一段人读的文本，拿不到"哪一步失败、为什么"这个唯一的
 * 可分支信息 —— 而那正是它决定下一步做什么的依据。
 *
 * 这类损失很隐蔽：工具确实返回了、`ok: false` 也是对的，
 * 只是**把有用的结构换成了没用的文本**。
 */

const base: RunResult = {
  ok: false,
  stdout: '',
  stderr: '',
  exitCode: 2,
}

describe('fromRun', () => {
  it('passes a structured payload through on success', () => {
    const result = fromRun({
      ...base,
      ok: true,
      exitCode: 0,
      value: { cv_mean: 0.81, rows: 418 },
    })
    expect(result.ok).toBe(true)
    expect(result.cv_mean).toBe(0.81)
    expect(result.rows).toBe(418)
  })

  it('keeps engine structure on failure instead of collapsing to text', () => {
    // 这是本轮修的核心：失败时引擎的 `steps` 必须到达模型。
    const result = fromRun({
      ...base,
      value: {
        steps: [
          { step: 'prepare', ok: false, error: '这场比赛的规则还没接受' },
        ],
      },
      error: 'kauto run 失败（exit 2）：…',
    })
    expect(result.ok).toBe(false)
    const steps = result.steps as Array<{ step: string; ok: boolean; error?: string }>
    expect(Array.isArray(steps)).toBe(true)
    expect(steps[0].step).toBe('prepare')
    expect(steps[0].ok).toBe(false)
    expect(String(steps[0].error)).toContain('规则还没接受')
  })

  it('keeps both the error line and the structure', () => {
    const result = fromRun({
      ...base,
      value: { steps: [{ step: 'baseline', ok: false }] },
      error: 'kauto run 失败（exit 2）：lightgbm 未安装',
      stderr: 'ModuleNotFoundError: lightgbm',
    })
    // 两者都要：一句话给模型快速判断，结构给模型精确分支。
    expect(String(result.error)).toContain('lightgbm')
    expect(Array.isArray(result.steps)).toBe(true)
    expect(String(result.stderr)).toContain('ModuleNotFoundError')
  })

  it('reports the exit code so callers can branch on known codes', () => {
    // 提交门用 exit 4、形状校验用 exit 3 —— 工具层需要这个数字。
    const result = fromRun({ ...base, exitCode: 4, error: '比赛已关闭' })
    expect(result.exitCode).toBe(4)
  })

  it('handles a failure with no parsed value at all', () => {
    const result = fromRun({
      ...base,
      stderr: 'Traceback (most recent call last): ...',
      error: 'kauto doctor 失败（exit 2）：boom',
    })
    expect(result.ok).toBe(false)
    expect(String(result.error)).toContain('boom')
    expect(String(result.stderr)).toContain('Traceback')
    expect(result.steps).toBeUndefined()
  })

  it('does not let stderr override an engine-provided field', () => {
    // 顺序很重要：结构在前、文本在后。反过来的话，引擎明确给出的
    // `error` 会被一段 stderr 摘要覆盖掉。
    const result = fromRun({
      ...base,
      value: { error: '结构化错误：规则未接受' },
      stderr: 'some noise',
      error: 'fallback',
    })
    expect(String(result.error)).toBe('结构化错误：规则未接受')
  })

  it('attaches caller extras last so they can override', () => {
    const result = fromRun(
      { ...base, ok: true, exitCode: 0, value: { rounds: 'engine' } },
      { rounds: 'caller', note: 'x' },
    )
    expect(result.rounds).toBe('caller')
    expect(result.note).toBe('x')
  })

  it('falls back to raw stdout when nothing parses', () => {
    const result = fromRun({
      ...base,
      ok: true,
      exitCode: 0,
      stdout: 'human readable only',
      value: undefined,
    })
    expect(result.ok).toBe(true)
    expect(String(result.stdout)).toBe('human readable only')
  })

  it('ignores an array value (the engine never returns a bare array as a result)', () => {
    // `list --format json` 返回数组，但那个工具走的是别的分支；
    // 这里只保证数组不会被当成对象展开成 `{0: ..., 1: ...}`。
    const result = fromRun({ ...base, ok: true, exitCode: 0, value: [1, 2, 3] })
    expect(result.ok).toBe(true)
    expect(result.value).toEqual([1, 2, 3])
    expect(result['0']).toBeUndefined()
  })
})
