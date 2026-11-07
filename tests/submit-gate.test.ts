import { describe, it, expect } from 'vitest'
import { DEFAULTS, resolveConfig } from '../src/config.ts'
import { submitGate } from '../src/tools.ts'

/** Convenience: settings with one override, fully resolved. */
const withSettings = (patch: Record<string, unknown>) => resolveConfig({ ...DEFAULTS, ...patch })

describe('submitGate', () => {
  it('withholds --yes when requireConfirm is on and confirm is absent', () => {
    const gate = submitGate(withSettings({ requireConfirm: true }), {})
    expect(gate.willSubmit).toBe(false)
    expect(gate.argv).toEqual([])
    expect(gate.withheldReason).toMatch(/确认/)
  })

  it('withholds --yes when confirm is not literally true', () => {
    for (const value of [false, 'true', 1, null, undefined]) {
      const gate = submitGate(withSettings({ requireConfirm: true }), { confirm: value })
      expect(gate.willSubmit).toBe(false)
      expect(gate.argv).toEqual([])
    }
  })

  it('appends --yes only when both gates pass', () => {
    const gate = submitGate(withSettings({ requireConfirm: true }), { confirm: true })
    expect(gate.willSubmit).toBe(true)
    expect(gate.argv).toEqual(['--yes'])
  })

  it('autoSubmit never relaxes the confirm requirement', () => {
    // The setting means "the agent may decide to submit", not "confirmation is
    // optional" — a plugin that quietly spend a daily slot would be a defect.
    const gate = submitGate(withSettings({ requireConfirm: true, autoSubmit: true }), {})
    expect(gate.willSubmit).toBe(false)
  })

  it('still withholds when the guard is off but confirm is absent', () => {
    // requireConfirm=false says "I have seen the file, go ahead" — but a bare
    // call has not said even that, so the slot stays unspent.
    const gate = submitGate(withSettings({ requireConfirm: false }), {})
    expect(gate.willSubmit).toBe(false)
    expect(gate.withheldReason).toMatch(/confirm/)
  })

  it('submits when the guard is off and confirm is given', () => {
    const gate = submitGate(withSettings({ requireConfirm: false }), { confirm: true })
    expect(gate.willSubmit).toBe(true)
    expect(gate.argv).toEqual(['--yes'])
  })
})
