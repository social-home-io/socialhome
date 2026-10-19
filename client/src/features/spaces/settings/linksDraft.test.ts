import { describe, expect, it, vi } from 'vitest'
import {
  diffLinks, draftOf, invalidLinks, newDraftKey, saveLinks, validLink,
  type DraftLink, type LinksApi, type StoredLink,
} from './linksDraft'

const stored: StoredLink[] = [
  { id: 'a', label: 'School', url: 'https://school.example', position: 0 },
  { id: 'b', label: 'Wiki', url: 'https://wiki.example', position: 1 },
  { id: 'c', label: 'Bins', url: 'https://bins.example', position: 2 },
]

const added = (label: string, url: string): DraftLink => ({ key: newDraftKey(), id: null, label, url })

/** An in-memory links API; ``fail`` names paths/methods that throw. */
function fakeApi(start: StoredLink[], fail: (method: string, path: string, body?: unknown) => boolean = () => false) {
  let rows = start.map(l => ({ ...l }))
  let n = 0
  const calls: string[] = []
  const api: LinksApi = {
    delete: vi.fn(async (path: string) => {
      calls.push(`DELETE ${path}`)
      if (fail('DELETE', path)) throw new Error('nope')
      rows = rows.filter(l => !path.endsWith(`/${l.id}`))
    }),
    patch: vi.fn(async (path: string, body: unknown) => {
      calls.push(`PATCH ${path} ${JSON.stringify(body)}`)
      if (fail('PATCH', path, body)) throw new Error('nope')
      rows = rows.map(l => (path.endsWith(`/${l.id}`) ? { ...l, ...(body as object) } : l))
    }),
    post: vi.fn(async (path: string, body: unknown) => {
      calls.push(`POST ${path} ${JSON.stringify(body)}`)
      if (fail('POST', path, body)) throw new Error('nope')
      n += 1
      const row = { id: `new${n}`, ...(body as { label: string; url: string; position: number }) }
      rows.push(row)
      return row
    }),
    get: vi.fn(async () => ({ links: [...rows].sort((x, y) => x.position - y.position) })),
  }
  return { api, calls, rows: () => rows }
}

describe('validLink', () => {
  it('needs a name and an http(s) address with a host', () => {
    expect(validLink('Wiki', 'https://wiki.example')).toBe('ok')
    expect(validLink(' ', 'https://wiki.example')).toBe('required')
    expect(validLink('Wiki', '')).toBe('required')
    expect(validLink('Wiki', 'javascript:alert(1)')).toBe('url')
    expect(validLink('Wiki', 'wiki.example')).toBe('url')
    expect(validLink('Wiki', 'https://user:pw@wiki.example')).toBe('url')
    expect(validLink('Wiki', 'https://wiki.example/a b')).toBe('url')
  })

  it('lists the rows Save cannot send', () => {
    const draft = [...draftOf(stored), added('', 'https://x.example')]
    expect(invalidLinks(draft).map(l => l.label)).toEqual([''])
  })
})

describe('diffLinks', () => {
  it('an untouched draft has nothing to save', () => {
    expect(diffLinks(stored, draftOf(stored)).count).toBe(0)
  })

  it('counts an add, a removal, an edit and a new order', () => {
    const draft = draftOf(stored)
    expect(diffLinks(stored, [...draft, added('Pool', 'https://pool.example')]).count).toBe(1)
    expect(diffLinks(stored, draft.filter(l => l.id !== 'b')).count).toBe(1)
    expect(diffLinks(stored, draft.map(l => (l.id === 'a' ? { ...l, label: 'School portal' } : l))).count).toBe(1)
    expect(diffLinks(stored, [draft[1], draft[0], draft[2]]).count).toBe(1)
  })

  it('a removal is not also counted as a move of the links after it', () => {
    const d = diffLinks(stored, draftOf(stored).filter(l => l.id !== 'a'))
    expect(d.count).toBe(1)
    // …but the remaining positions are renumbered on Save.
    expect(d.patches.map(p => [p.link.id, p.body])).toEqual([['b', { position: 0 }], ['c', { position: 1 }]])
  })

  it('an edit back to the stored text (or only spaces) is no change', () => {
    const draft = draftOf(stored).map(l => (l.id === 'a' ? { ...l, label: ' School ' } : l))
    expect(diffLinks(stored, draft).count).toBe(0)
  })

  it('never renumbers on its own: odd stored positions stay until the list changes', () => {
    const odd = stored.map((l, i) => ({ ...l, position: i * 10 }))
    expect(diffLinks(odd, draftOf(odd)).patches).toEqual([])
  })
})

describe('saveLinks', () => {
  it('applies deletes, edits / moves, then new links — one call at a time', async () => {
    const f = fakeApi(stored)
    const draft = draftOf(stored).filter(l => l.id !== 'c')
    draft.reverse() // Wiki, School
    draft[0] = { ...draft[0], label: 'Family wiki' }
    const pool = added('Pool', 'https://pool.example')
    draft.push(pool)
    const res = await saveLinks(f.api, 's1', stored, draft)
    expect(f.calls).toEqual([
      'DELETE /api/spaces/s1/links/c',
      'PATCH /api/spaces/s1/links/b {"label":"Family wiki","position":0}',
      'PATCH /api/spaces/s1/links/a {"position":1}',
      'POST /api/spaces/s1/links {"label":"Pool","url":"https://pool.example","position":2}',
    ])
    expect(res.failed).toEqual([])
    expect(res.stored.map(l => l.label)).toEqual(['Family wiki', 'School', 'Pool'])
    // The new row now carries its server id: nothing left to save.
    expect(res.draft.find(l => l.key === pool.key)?.id).toBe('new1')
    expect(diffLinks(res.stored, res.draft).count).toBe(0)
  })

  it('a failed call keeps only that change unsaved; the rest is saved', async () => {
    const f = fakeApi(stored, (m, path) => (m === 'PATCH' && path.endsWith('/a')) || m === 'DELETE')
    const draft = draftOf(stored)
      .filter(l => l.id !== 'c')
      .map(l => (l.id === 'a' ? { ...l, label: 'School portal' } : l.id === 'b' ? { ...l, label: 'Family wiki' } : l))
    const pool = added('Pool', 'https://pool.example')
    const res = await saveLinks(f.api, 's1', stored, [...draft, pool])
    expect(res.failed.map(x => x.label)).toEqual(['Bins', 'School portal'])
    // Wiki's rename and the new link landed…
    expect(f.rows().map(l => l.label)).toEqual(['School', 'Family wiki', 'Bins', 'Pool'])
    // …and the save bar still counts the two that didn't.
    expect(diffLinks(res.stored, res.draft).count).toBe(2)
    // Save again retries exactly those.
    f.calls.length = 0
    const again = await saveLinks(fakeApi(res.stored).api, 's1', res.stored, res.draft)
    expect(again.failed).toEqual([])
    expect(diffLinks(again.stored, again.draft).count).toBe(0)
  })

  it('when the re-read fails, a retry never repeats a call that worked', async () => {
    const f = fakeApi(stored)
    f.api.get = vi.fn(async () => { throw new Error('offline') })
    const pool = added('Pool', 'https://pool.example')
    const draft = [...draftOf(stored).filter(l => l.id !== 'a'), pool]
    const res = await saveLinks(f.api, 's1', stored, draft)
    expect(res.failed).toEqual([])
    expect(res.stored.map(l => l.id)).toEqual(['b', 'c', 'new1'])
    expect(diffLinks(res.stored, res.draft).count).toBe(0)
  })
})
