import { describe, expect, it } from 'vitest'
import { isMomentHash, momentBootFromHash } from './moment_link'

const K = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8'
const B = 'a'.repeat(64)
const ok = `#g=${encodeURIComponent('https://gfs.example.org')}&a=u-noor&m=m-1&b=${B}&k=${K}`

describe('momentBootFromHash', () => {
  it('reads a well-formed moment link', () => {
    const boot = momentBootFromHash(ok)
    expect(boot).not.toBeNull()
    expect(boot!.gfs).toBe('https://gfs.example.org')
    expect([boot!.author, boot!.momentId, boot!.shareId]).toEqual(['u-noor', 'm-1', B])
    expect(boot!.itemKey.length).toBe(32)
    expect(isMomentHash(ok)).toBe(true)
    expect(isMomentHash('#g=x&i=y&h=z&t=t&k=k')).toBe(false)
  })

  it.each([
    ['plain-http GFS', ok.replace('https', 'http')],
    ['missing key', ok.replace(`&k=${K}`, '')],
    ['short key', ok.replace(K, K.slice(1))],
    ['bad blob id', ok.replace(B, 'z'.repeat(64))],
    ['missing moment', ok.replace('&m=m-1', '')],
    ['unsafe author', ok.replace('a=u-noor', 'a=u%3Cscript%3E')],
    ['overlong', ok + '&x=' + 'y'.repeat(3000)],
  ])('refuses a link with %s', (_label, hash) => {
    expect(momentBootFromHash(hash)).toBeNull()
  })
})
