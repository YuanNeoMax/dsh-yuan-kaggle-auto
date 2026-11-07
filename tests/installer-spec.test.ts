import { describe, it, expect } from 'vitest'
import { splitSpec } from '../src/installer.ts'

describe('splitSpec', () => {
  it('splits a bare name', () => {
    expect(splitSpec('dsh-ego-browser')).toEqual({ name: 'dsh-ego-browser', range: '' })
  })

  it('splits name@range', () => {
    expect(splitSpec('dsh-ego-browser@^0.9')).toEqual({ name: 'dsh-ego-browser', range: '^0.9' })
    expect(splitSpec('dsh-ego-browser@0.9.7')).toEqual({ name: 'dsh-ego-browser', range: '0.9.7' })
  })

  it('handles a scoped name without mistaking the scope @ for a version', () => {
    // '@copylee/dsh-computer-use' has an @ at index 0 that is NOT a separator;
    // the first implementation split there and produced name '' + range 'copylee/...'.
    expect(splitSpec('@copylee/dsh-computer-use')).toEqual({ name: '@copylee/dsh-computer-use', range: '' })
    expect(splitSpec('@copylee/dsh-computer-use@0.8.1')).toEqual({ name: '@copylee/dsh-computer-use', range: '0.8.1' })
  })

  it('leaves local paths and tarballs intact', () => {
    expect(splitSpec('./dsh-ego-browser-0.9.7.tgz')).toEqual({ name: './dsh-ego-browser-0.9.7.tgz', range: '' })
    expect(splitSpec('C:\\pkgs\\ego.tgz')).toEqual({ name: 'C:\\pkgs\\ego.tgz', range: '' })
    expect(splitSpec('/opt/pkgs/ego.tgz')).toEqual({ name: '/opt/pkgs/ego.tgz', range: '' })
  })

  it('leaves git specs intact', () => {
    expect(splitSpec('github:Fisfzy/dsh-ego-browser')).toEqual({ name: 'github:Fisfzy/dsh-ego-browser', range: '' })
    expect(splitSpec('git+https://github.com/Fisfzy/dsh-ego-browser.git')).toEqual({
      name: 'git+https://github.com/Fisfzy/dsh-ego-browser.git',
      range: '',
    })
  })

  it('returns empty parts for blank input', () => {
    expect(splitSpec('')).toEqual({ name: '', range: '' })
    expect(splitSpec('   ')).toEqual({ name: '', range: '' })
  })
})
