import { describe, it, expect } from 'vitest'
import { lastJsonValue } from '../src/runner.ts'

describe('lastJsonValue', () => {
  it('parses a clean JSON document', () => {
    expect(lastJsonValue('{"ok":true,"n":1}')).toEqual({ ok: true, n: 1 })
    expect(lastJsonValue('[1,2,3]')).toEqual([1, 2, 3])
  })

  it('ignores progress lines printed before the document', () => {
    // The engine prints human lines first on several commands; the document is
    // the answer, so parsing must not be anchored to the string's start.
    const stdout = [
      '[kauto] 全量重训：42000 行 × 784 特征 × 10 类（LightGBM 参数按规模自适应）',
      'some warning about a column',
      '{"submission":"x.csv","rows":28000}',
    ].join('\n')
    expect(lastJsonValue(stdout)).toEqual({ submission: 'x.csv', rows: 28000 })
  })

  it('ignores a human sentence printed after the document', () => {
    const stdout = '{"cv_mean":0.95}\nEvery check passed. The shape is right.'
    expect(lastJsonValue(stdout)).toEqual({ cv_mean: 0.95 })
  })

  it('is not confused by braces inside strings', () => {
    const stdout = 'note before\n{"error":"unbalanced } brace","ok":false}'
    expect(lastJsonValue(stdout)).toEqual({ error: 'unbalanced } brace', ok: false })
  })

  it('is not confused by escaped quotes', () => {
    const stdout = '{"text":"he said \\"hi {there}\\"","ok":true}'
    expect(lastJsonValue(stdout)).toEqual({ text: 'he said "hi {there}"', ok: true })
  })

  it('takes the LAST balanced document when several are present', () => {
    const stdout = '{"stage":"baseline"}\nthen\n{"stage":"final","score":0.8}'
    expect(lastJsonValue(stdout)).toEqual({ stage: 'final', score: 0.8 })
  })

  it('returns undefined for text with no JSON', () => {
    expect(lastJsonValue('')).toBeUndefined()
    expect(lastJsonValue('   ')).toBeUndefined()
    expect(lastJsonValue('no json at all here')).toBeUndefined()
    expect(lastJsonValue('{ unbalanced')).toBeUndefined()
  })
})
