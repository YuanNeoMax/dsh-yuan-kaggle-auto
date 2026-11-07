import { describe, it, expect } from 'vitest'
import { DEFAULTS, resolveConfig } from '../src/config.ts'

describe('resolveConfig', () => {
  it('returns defaults for an empty or non-object input', () => {
    for (const input of [undefined, null, 42, 'x', []]) {
      const settings = resolveConfig(input)
      expect(settings).toEqual(DEFAULTS)
    }
  })

  it('unwraps volatile references', () => {
    // The settings service hands `.volatile()` fields back as `{ get() }`;
    // reading them as plain values would silently drop every user edit.
    const settings = resolveConfig({
      threads: { get: () => 4 },
      requireConfirm: { get: () => false },
      workRoot: { get: () => 'D:/work' },
    })
    expect(settings.threads).toBe(4)
    expect(settings.requireConfirm).toBe(false)
    expect(settings.workRoot).toBe('D:/work')
  })

  it('falls back per field instead of failing the whole entry', () => {
    const settings = resolveConfig({
      threads: 'many',            // wrong type
      maxRounds: -3,              // out of range
      llmProvider: 'nonsense',    // not in the union
      egoAssist: 1,               // truthy but not boolean
    })
    expect(settings.threads).toBe(DEFAULTS.threads)
    expect(settings.maxRounds).toBe(1)          // clamped to the minimum, not defaulted
    expect(settings.llmProvider).toBe(DEFAULTS.llmProvider)
    expect(settings.egoAssist).toBe(DEFAULTS.egoAssist)
  })

  it('clamps numeric fields into their declared range', () => {
    expect(resolveConfig({ threads: 999 }).threads).toBe(32)
    expect(resolveConfig({ threads: 0 }).threads).toBe(1)
    expect(resolveConfig({ maxRounds: 99 }).maxRounds).toBe(12)
    expect(resolveConfig({ threads: 3.7 }).threads).toBe(4)
  })

  it('trims strings and rejects empty overrides for defaults', () => {
    expect(resolveConfig({ workRoot: '  D:/w  ' }).workRoot).toBe('D:/w')
    // An empty egoVersion must not become '' — the installer needs a range.
    expect(resolveConfig({ egoVersion: '   ' }).egoVersion).toBe(DEFAULTS.egoVersion)
  })

  it('keeps the submission gate closed by default', () => {
    expect(DEFAULTS.requireConfirm).toBe(true)
    expect(DEFAULTS.autoSubmit).toBe(false)
  })
})
