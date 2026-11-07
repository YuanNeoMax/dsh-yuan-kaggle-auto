import { describe, it, expect } from 'vitest'
import { isTrustedRequest, readBody } from '../src/routes.ts'

type Req = Parameters<typeof isTrustedRequest>[0]

const req = (cookie?: string): Req => ({
  method: 'POST',
  headers: cookie === undefined ? {} : { cookie },
})

describe('isTrustedRequest', () => {
  it('requires a dsh-auth cookie', () => {
    expect(isTrustedRequest(req())).toBe(false)
    expect(isTrustedRequest(req('session=abc'))).toBe(false)
    expect(isTrustedRequest(req('dsh-auth-123=xyz'))).toBe(true)
  })

  it('accepts the cookie among others and in any position', () => {
    expect(isTrustedRequest(req('a=1; dsh-auth-k=token; b=2'))).toBe(true)
    expect(isTrustedRequest(req('dsh-auth-k=token; a=1'))).toBe(true)
  })

  it('is not fooled by a lookalike value', () => {
    // The pattern requires the cookie NAME to start with dsh-auth-, not just
    // the string to appear somewhere.
    expect(isTrustedRequest(req('other=dsh-auth-x'))).toBe(false)
  })

  it('handles an array-valued cookie header', () => {
    const request: Req = { method: 'GET', headers: { cookie: ['a=1', 'dsh-auth-k=t'] } }
    expect(isTrustedRequest(request)).toBe(true)
  })
})

describe('readBody', () => {
  it('passes through an already-parsed body', async () => {
    const request = { headers: {}, body: { force: true } } as Req & { body: unknown }
    expect(await readBody(request)).toEqual({ force: true })
  })

  it('parses a streamed JSON body', async () => {
    const listeners: Record<string, Array<(...args: unknown[]) => void>> = {}
    const request = {
      headers: {},
      on(event: string, listener: (...args: unknown[]) => void) {
        (listeners[event] ??= []).push(listener)
      },
    } as unknown as Req
    const promise = readBody(request)
    listeners.data?.forEach((fn) => fn('{"version":"^0.9"}'))
    listeners.end?.forEach((fn) => fn())
    expect(await promise).toEqual({ version: '^0.9' })
  })

  it('falls back to an empty object for malformed JSON', async () => {
    const listeners: Record<string, Array<(...args: unknown[]) => void>> = {}
    const request = {
      headers: {},
      on(event: string, listener: (...args: unknown[]) => void) {
        (listeners[event] ??= []).push(listener)
      },
    } as unknown as Req
    const promise = readBody(request)
    listeners.data?.forEach((fn) => fn('not json'))
    listeners.end?.forEach((fn) => fn())
    expect(await promise).toEqual({})
  })

  it('resolves empty when there is no stream to read', async () => {
    expect(await readBody({ headers: {} } as Req)).toEqual({})
  })
})
