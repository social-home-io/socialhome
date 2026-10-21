/**
 * The pinned viewer (a static page no GFS serves) reads its whole boot
 * context from the link's fragment: ``#g=<gfs>&i=&h=&t=&k=``. Every field
 * is bounded and the GFS URL is refused unless it is plain https (or the
 * localhost dev / demo setup), so a crafted link can't point the viewer's
 * requests at a script URL, a credentialed URL or an http host.
 */
import { describe, expect, it } from 'vitest'
import {
  MAX_FIELD_LENGTH,
  gfsBaseUrl,
  pinnedBootFromHash,
  pinnedEndpoint,
  pinnedRequestInit,
} from './pinned_link'

const K = '0htX0WWeIJnofu_HTifPGX_9pz5oTvHMWAG9Ys-9JyQ'

function hash(params: Record<string, string>): string {
  return '#' + new URLSearchParams(params).toString()
}

describe('gfsBaseUrl', () => {
  it('accepts an https URL and drops a trailing slash', () => {
    expect(gfsBaseUrl('https://gfs.example.org/')).toBe('https://gfs.example.org')
    expect(gfsBaseUrl('https://gfs.example.org:8443/prefix/')).toBe(
      'https://gfs.example.org:8443/prefix',
    )
  })

  it('accepts http only for localhost and 127.0.0.1 (dev / demo)', () => {
    expect(gfsBaseUrl('http://localhost:18765')).toBe('http://localhost:18765')
    expect(gfsBaseUrl('http://127.0.0.1:18765')).toBe('http://127.0.0.1:18765')
    expect(gfsBaseUrl('http://gfs.example.org')).toBeNull()
    expect(gfsBaseUrl('http://127.0.0.1.evil.example')).toBeNull()
  })

  it('refuses script, data and relative URLs', () => {
    expect(gfsBaseUrl('javascript:alert(1)')).toBeNull()
    expect(gfsBaseUrl('data:text/html,hi')).toBeNull()
    expect(gfsBaseUrl('//gfs.example.org')).toBeNull()
    expect(gfsBaseUrl('/gfs')).toBeNull()
    expect(gfsBaseUrl('')).toBeNull()
  })

  it('refuses credentials, a fragment or a query', () => {
    expect(gfsBaseUrl('https://user:pw@gfs.example.org')).toBeNull()
    expect(gfsBaseUrl('https://user@gfs.example.org')).toBeNull()
    expect(gfsBaseUrl('https://gfs.example.org/#x')).toBeNull()
    expect(gfsBaseUrl('https://gfs.example.org/?a=b')).toBeNull()
  })

  it('refuses an overlong URL', () => {
    const long = 'https://gfs.example.org/' + 'a'.repeat(MAX_FIELD_LENGTH.g)
    expect(gfsBaseUrl(long)).toBeNull()
  })
})

describe('pinnedBootFromHash', () => {
  const ok = { g: 'https://gfs.example.org', i: 'inst-1', h: 'hl-1', t: 'tok_1-x', k: K }

  it('reads every field from the fragment', () => {
    expect(pinnedBootFromHash(hash(ok))).toEqual({
      gfs: 'https://gfs.example.org',
      instanceId: 'inst-1',
      highlightId: 'hl-1',
      token: 'tok_1-x',
    })
  })

  it('refuses a missing field', () => {
    for (const drop of ['g', 'i', 'h', 't'] as const) {
      const rest: Record<string, string> = { ...ok }
      delete rest[drop]
      expect(pinnedBootFromHash(hash(rest))).toBeNull()
    }
  })

  it('refuses an http GFS that is not localhost, and a javascript: one', () => {
    expect(pinnedBootFromHash(hash({ ...ok, g: 'http://gfs.example.org' }))).toBeNull()
    expect(pinnedBootFromHash(hash({ ...ok, g: 'javascript:alert(1)' }))).toBeNull()
  })

  it('refuses overlong ids and tokens', () => {
    expect(pinnedBootFromHash(hash({ ...ok, i: 'a'.repeat(MAX_FIELD_LENGTH.i + 1) }))).toBeNull()
    expect(pinnedBootFromHash(hash({ ...ok, h: 'a'.repeat(MAX_FIELD_LENGTH.h + 1) }))).toBeNull()
    expect(pinnedBootFromHash(hash({ ...ok, t: 'a'.repeat(MAX_FIELD_LENGTH.t + 1) }))).toBeNull()
  })

  it('refuses ids with characters outside the URL-safe set', () => {
    expect(pinnedBootFromHash(hash({ ...ok, i: '../admin' }))).toBeNull()
    expect(pinnedBootFromHash(hash({ ...ok, h: 'a b' }))).toBeNull()
    expect(pinnedBootFromHash(hash({ ...ok, t: 'x?y' }))).toBeNull()
  })

  it('refuses an overlong fragment before parsing it', () => {
    expect(pinnedBootFromHash(hash(ok) + '&pad=' + 'x'.repeat(4096))).toBeNull()
  })
})

describe('pinned requests', () => {
  it('are absolute on the GFS, carry no credentials and no referrer', () => {
    expect(pinnedEndpoint('https://gfs.example.org', '/gfs/highlights/ice-servers')).toBe(
      'https://gfs.example.org/gfs/highlights/ice-servers',
    )
    expect(pinnedRequestInit({ method: 'POST' })).toEqual({
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      redirect: 'error',
    })
  })
})

describe('gfsBaseUrl, review hardening', () => {
  it('refuses a host the household and the browser could read differently', () => {
    expect(gfsBaseUrl('https://gfs.example.org%2F@evil')).toBeNull()
    expect(gfsBaseUrl('https://gfs.example.org\\@evil.example')).toBeNull()
    expect(gfsBaseUrl('https://[::1]:8443')).toBeNull()
  })
})
