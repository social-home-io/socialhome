/**
 * A GFS's public page opens in the viewer from a fragment the GFS's 302
 * wrote: ``#v=<view>&g=<gfs>&<id>``. Anything off — another view, a stray
 * or repeated field, an http GFS, an id outside the URL-safe alphabet —
 * is refused before anything is fetched.
 */
import { describe, expect, it } from 'vitest'
import { MAX_ID_LENGTH, isPageHash, pageBootFromHash } from './page_link'

const G = 'https://gfs.example.org'

function hash(params: [string, string][]): string {
  return '#' + new URLSearchParams(params).toString()
}

describe('pageBootFromHash', () => {
  it('reads each view', () => {
    expect(pageBootFromHash(hash([['v', 'space'], ['g', G], ['s', 'sp-1']]))).toEqual({
      view: 'space', gfs: G, spaceId: 'sp-1',
    })
    expect(pageBootFromHash(hash([['v', 'join'], ['g', `${G}/`], ['t', 'tok_A-b.c~']]))).toEqual({
      view: 'join', gfs: G, token: 'tok_A-b.c~',
    })
    expect(pageBootFromHash(hash([['v', 'person'], ['g', 'http://127.0.0.1:18765'], ['u', 'u1']]))).toEqual({
      view: 'person', gfs: 'http://127.0.0.1:18765', userId: 'u1',
    })
  })

  it.each([
    ['unknown view', [['v', 'admin'], ['g', G], ['s', 'x']]],
    ['wrong id field', [['v', 'space'], ['g', G], ['t', 'x']]],
    ['extra field', [['v', 'space'], ['g', G], ['s', 'x'], ['k', 'y']]],
    ['repeated field', [['v', 'space'], ['g', G], ['s', 'x'], ['s', 'y']]],
    ['missing gfs', [['v', 'space'], ['s', 'x'], ['t', 'y']]],
    ['http gfs', [['v', 'space'], ['g', 'http://gfs.example.org'], ['s', 'x']]],
    ['script gfs', [['v', 'space'], ['g', 'javascript:alert(1)'], ['s', 'x']]],
    ['gfs with credentials', [['v', 'space'], ['g', 'https://a:b@gfs.example.org'], ['s', 'x']]],
    ['slash in id', [['v', 'space'], ['g', G], ['s', 'a/b']]],
    ['dot-dot id', [['v', 'space'], ['g', G], ['s', '..']]],
    ['empty id', [['v', 'person'], ['g', G], ['u', '']]],
    ['markup id', [['v', 'person'], ['g', G], ['u', '<img>']]],
    ['proto', [['v', '__proto__'], ['g', G], ['s', 'x']]],
  ] as [string, [string, string][]][])('refuses %s', (_name, params) => {
    expect(pageBootFromHash(hash(params))).toBeNull()
  })

  it('bounds each id and the whole fragment', () => {
    const at = (n: number) => hash([['v', 'join'], ['g', G], ['t', 'a'.repeat(n)]])
    expect(pageBootFromHash(at(MAX_ID_LENGTH.t))).not.toBeNull()
    expect(pageBootFromHash(at(MAX_ID_LENGTH.t + 1))).toBeNull()
    expect(pageBootFromHash('#v=space&g=' + encodeURIComponent(G) + '&s=x' + '&'.repeat(1100))).toBeNull()
  })
})

describe('isPageHash', () => {
  it('tells a page link from a highlight or moment link', () => {
    expect(isPageHash('#v=space&g=x&s=y')).toBe(true)
    expect(isPageHash('#g=x&i=a&h=b&t=c&k=d')).toBe(false)
    expect(isPageHash('#g=x&a=1&m=2&b=3&k=4')).toBe(false)
  })
})
