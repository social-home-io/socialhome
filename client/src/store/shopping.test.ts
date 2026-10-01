/**
 * Tests for the shopping store's catalogue reconciliation.
 *
 * Store names are unique case-insensitively server-side (migration
 * 0048), so a rename can now MERGE two catalogue rows and the server
 * may answer with a spelling that differs from the one the caller
 * typed. These tests pin the local signals to the same end state the
 * server reports — both for the tab that initiated the change and for
 * a sibling tab that only sees the WS frame.
 *
 * ``@/api`` is mocked through ONE factory (two competing factories in
 * a single file previously flaked — see bd4c73c); ``@/ws`` is mocked
 * so we can drive synthetic frames at the registered callbacks.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    put: (...args: unknown[]) => apiPut(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

import {
  items,
  stores,
  createStore,
  renameStore,
  deleteStore,
  sameName,
  wireShoppingWs,
  loadShopping,
  ensureShopping,
  shoppingLoaded,
  deleteItems,
  updateItem,
  toggleItem,
  reorderStores,
  resetShopping,
} from './shopping'
import { pendingDeletes, undoableDelete, resetPendingDeletes } from '@/utils/undoableDelete'
import type { ShoppingItem } from '@/types'

function item(id: string, text: string, store: string | null): ShoppingItem {
  return {
    id,
    text,
    completed: false,
    created_by: 'u1',
    created_at: '2026-05-02T08:00:00Z',
    store,
  }
}

// ``wireShoppingWs`` is idempotent per module instance — wire once and
// reuse the captured handlers across every case.
wireShoppingWs()

beforeEach(() => {
  items.value = []
  stores.value = []
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiPut.mockReset()
  apiDelete.mockReset()
})

describe('shopping store', () => {
  it('starts with empty items', () => {
    expect(items.value).toEqual([])
  })
})

describe('createStore', () => {
  it('appends the store the server reports', async () => {
    apiPost.mockResolvedValue({ name: 'Bakery', sort_order: 0 })
    const created = await createStore('Bakery')
    expect(created).toEqual({ name: 'Bakery', sort_order: 0 })
    expect(stores.value).toEqual([{ name: 'Bakery', sort_order: 0 }])
    expect(apiPost).toHaveBeenCalledWith('/api/shopping/stores', { name: 'Bakery' })
  })

  it('does not duplicate a store that already exists in another casing', async () => {
    stores.value = [{ name: 'Bakery', sort_order: 0 }]
    // Idempotent server side — the existing row comes back verbatim.
    apiPost.mockResolvedValue({ name: 'Bakery', sort_order: 0 })
    const created = await createStore('bakery')
    expect(created.name).toBe('Bakery')
    expect(stores.value).toEqual([{ name: 'Bakery', sort_order: 0 }])
  })
})

describe('renameStore', () => {
  it('merges the old row onto the survivor, keeping the survivor sort_order', async () => {
    stores.value = [
      { name: 'Aldi', sort_order: 0 },
      { name: 'Migros', sort_order: 1 },
    ]
    items.value = [item('i1', 'milk', 'Aldi'), item('i2', 'bread', 'Migros')]
    apiPatch.mockResolvedValue({
      old_name: 'Aldi',
      new_name: 'Migros',
      merged: true,
      moved_items: 1,
    })

    const result = await renameStore('Aldi', 'Migros')

    expect(result).toEqual({
      old_name: 'Aldi',
      new_name: 'Migros',
      merged: true,
      moved_items: 1,
    })
    expect(stores.value).toEqual([{ name: 'Migros', sort_order: 1 }])
    expect(items.value.map(i => i.store)).toEqual(['Migros', 'Migros'])
  })

  it("reconciles to the server's casing when it differs from what was typed", async () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }, { name: 'Migros', sort_order: 1 }]
    items.value = [item('i1', 'milk', 'Aldi')]
    // Caller typed "migros"; the survivor's spelling is "Migros".
    apiPatch.mockResolvedValue({
      old_name: 'Aldi',
      new_name: 'Migros',
      merged: true,
      moved_items: 1,
    })

    await renameStore('Aldi', 'migros')

    expect(stores.value).toEqual([{ name: 'Migros', sort_order: 1 }])
    expect(items.value[0].store).toBe('Migros')
  })

  it('treats a pure case change as real work, not a no-op', async () => {
    stores.value = [{ name: 'migros', sort_order: 0 }]
    items.value = [item('i1', 'milk', 'migros')]
    apiPatch.mockResolvedValue({
      old_name: 'migros',
      new_name: 'Migros',
      merged: false,
      moved_items: 0,
    })

    const result = await renameStore('migros', 'Migros')

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/shopping/stores/migros',
      { name: 'Migros' },
    )
    expect(result.new_name).toBe('Migros')
    expect(stores.value).toEqual([{ name: 'Migros', sort_order: 0 }])
    expect(items.value[0].store).toBe('Migros')
  })

  it('is a no-op on an exact match', async () => {
    stores.value = [{ name: 'Migros', sort_order: 0 }]
    const result = await renameStore('Migros', ' Migros ')
    expect(apiPatch).not.toHaveBeenCalled()
    expect(result).toEqual({
      old_name: 'Migros',
      new_name: 'Migros',
      merged: false,
      moved_items: 0,
    })
  })

  it('rolls both signals back when the request throws', async () => {
    const before = [
      { name: 'Aldi', sort_order: 0 },
      { name: 'Migros', sort_order: 1 },
    ]
    stores.value = before
    items.value = [item('i1', 'milk', 'Aldi')]
    apiPatch.mockRejectedValue(new Error('boom'))

    await expect(renameStore('Aldi', 'Migros')).rejects.toThrow('boom')

    expect(stores.value).toEqual(before)
    expect(items.value[0].store).toBe('Aldi')
  })
})

describe('deleteStore', () => {
  it('clears items whose casing diverged from the catalogue row', async () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }]
    items.value = [item('i1', 'milk', 'aldi'), item('i2', 'bread', 'Migros')]
    apiDelete.mockResolvedValue({ name: 'aldi', cleared: 1 })

    await deleteStore('aldi')

    expect(stores.value).toEqual([])
    expect(items.value.map(i => i.store)).toEqual([null, 'Migros'])
  })
})

describe('shopping_list.store_renamed frame (sibling tab)', () => {
  it('collapses the duplicate instead of leaving two identical rows', () => {
    stores.value = [
      { name: 'Aldi', sort_order: 0 },
      { name: 'Migros', sort_order: 1 },
    ]
    items.value = [item('i1', 'milk', 'aldi'), item('i2', 'bread', 'Migros')]

    handlers['shopping_list.store_renamed']({
      data: { old_name: 'Aldi', new_name: 'Migros' },
    })

    expect(stores.value).toEqual([{ name: 'Migros', sort_order: 1 }])
    expect(items.value.map(i => i.store)).toEqual(['Migros', 'Migros'])
  })

  it('renames in place when the target does not exist yet', () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }]
    items.value = [item('i1', 'milk', 'Aldi')]

    handlers['shopping_list.store_renamed']({
      data: { old_name: 'aldi', new_name: 'Lidl' },
    })

    expect(stores.value).toEqual([{ name: 'Lidl', sort_order: 0 }])
    expect(items.value[0].store).toBe('Lidl')
  })
})

describe('sameName', () => {
  it('folds ASCII case, exactly like SQLite NOCASE', () => {
    expect(sameName('Migros', 'migros')).toBe(true)
    expect(sameName('MIGROS', 'migros')).toBe(true)
  })

  it('does NOT fold non-ASCII — the server keeps those as distinct stores', () => {
    // SQLite NOCASE (and migration 0048) are ASCII-only, so "Müller"
    // and "MÜLLER" are two legitimate rows. Folding them here would
    // collapse both catalogue entries into one name on a merge and
    // show items under a store the server never moved them to.
    // Verified against SQLite: 'Müller' = 'MÜLLER' COLLATE NOCASE is
    // FALSE (the Ü is untouched), while 'Müller' = 'müller' is TRUE
    // because only the ASCII M differs. The fold has to agree on both.
    expect(sameName('Müller', 'MÜLLER')).toBe(false)
    expect(sameName('Müller', 'müller')).toBe(true)
    expect(sameName('ÜBER', 'über')).toBe(false)
  })
})

describe('loadShopping', () => {
  function deferredGets() {
    const resolvers: Array<() => void> = []
    apiGet.mockImplementation((url: string) => new Promise((res) => {
      resolvers.push(() => res(url.includes('/stores') ? [] : [item('i1', 'Milk', null)]))
    }))
    return () => resolvers.splice(0).forEach(r => r())
  }

  beforeEach(() => {
    shoppingLoaded.value = false
  })

  it('dedupes concurrent calls onto one in-flight request pair', async () => {
    const flush = deferredGets()
    const a = loadShopping()
    const b = loadShopping()
    expect(a).toBe(b)
    expect(apiGet).toHaveBeenCalledTimes(2) // items + stores, once
    flush()
    await a
    expect(items.value.map(i => i.id)).toEqual(['i1'])
    expect(shoppingLoaded.value).toBe(true)
  })

  it('fetches again once the previous load has settled', async () => {
    const flush = deferredGets()
    const first = loadShopping()
    flush()
    await first
    const second = loadShopping()
    expect(second).not.toBe(first)
    flush()
    await second
    expect(apiGet).toHaveBeenCalledTimes(4)
  })

  it('a failed load rejects, leaves loaded false, and does not wedge later loads', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    apiGet.mockResolvedValue([])
    await expect(loadShopping()).rejects.toThrow('offline')
    expect(shoppingLoaded.value).toBe(false)
    await loadShopping()
    expect(shoppingLoaded.value).toBe(true)
  })

  it('ensureShopping only fetches when nothing is loaded yet', async () => {
    apiGet.mockResolvedValue([])
    await ensureShopping()
    expect(apiGet).toHaveBeenCalledTimes(2)
    await ensureShopping()
    expect(apiGet).toHaveBeenCalledTimes(2)
  })
})


describe('deleteItems', () => {
  it('deletes each id, in batches of at most 10, and drops them locally', async () => {
    items.value = Array.from({ length: 12 }, (_, i) => item(`d${i}`, `x${i}`, null))
    let inFlight = 0
    let peak = 0
    apiDelete.mockImplementation(async () => {
      inFlight++; peak = Math.max(peak, inFlight)
      await Promise.resolve()
      inFlight--
    })
    await deleteItems(items.value.map(i => i.id))
    expect(apiDelete).toHaveBeenCalledTimes(12)
    expect(peak).toBeLessThanOrEqual(10)
    expect(items.value).toEqual([])
  })

  it('treats 404 as gone, keeps the failures and rejects', async () => {
    items.value = [item('a', 'A', null), item('b', 'B', null), item('c', 'C', null)]
    apiDelete.mockImplementation(async (url: string) => {
      if (url.endsWith('/b')) throw Object.assign(new Error('gone'), { status: 404 })
      if (url.endsWith('/c')) throw Object.assign(new Error('boom'), { status: 500 })
    })
    await expect(deleteItems(['a', 'b', 'c'])).rejects.toThrow('boom')
    expect(items.value.map(i => i.id)).toEqual(['c'])
  })
})

describe('row-scoped rollback (concurrent commits)', () => {
  it('a failed toggle restores only its own row — an item deleted meanwhile stays deleted', async () => {
    items.value = [item('a', 'A', null), item('b', 'B', null)]
    let fail!: (e: unknown) => void
    apiPatch.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    apiDelete.mockResolvedValue(undefined)
    const toggling = toggleItem('a', true)
    await deleteItems(['b'])
    fail(new Error('boom'))
    await expect(toggling).rejects.toThrow('boom')
    expect(items.value.map(i => [i.id, i.completed])).toEqual([['a', false]])
  })

  it('a failed update restores only the patched fields of that row', async () => {
    items.value = [item('a', 'A', 'Aldi'), item('b', 'B', null)]
    let fail!: (e: unknown) => void
    apiPatch.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    const updating = updateItem('a', { text: 'A2' })
    // Meanwhile a WS frame moves "a" to another store and "b" goes away.
    items.value = items.value
      .filter(i => i.id !== 'b')
      .map(i => (i.id === 'a' ? { ...i, store: 'Migros' } : i))
    fail(new Error('boom'))
    await expect(updating).rejects.toThrow('boom')
    expect(items.value).toEqual([{ ...item('a', 'A', 'Migros') }])
  })

  it('a failed update does not resurrect a row deleted meanwhile', async () => {
    items.value = [item('a', 'A', null)]
    let fail!: (e: unknown) => void
    apiPatch.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    const updating = updateItem('a', { text: 'A2' })
    items.value = []
    fail(new Error('boom'))
    await expect(updating).rejects.toThrow('boom')
    expect(items.value).toEqual([])
  })

  it('a failed reorder restores the old order but keeps a store added meanwhile', async () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }, { name: 'Migros', sort_order: 1 }]
    let fail!: (e: unknown) => void
    apiPut.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    const moving = reorderStores(['Migros', 'Aldi'])
    stores.value = [...stores.value, { name: 'Coop', sort_order: 2 }]
    fail(new Error('boom'))
    await expect(moving).rejects.toThrow('boom')
    expect(stores.value.map(s => s.name)).toEqual(['Aldi', 'Migros', 'Coop'])
  })

  it('a failed store delete restores that store and only the items it cleared', async () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }, { name: 'Migros', sort_order: 1 }]
    items.value = [item('a', 'A', 'Aldi'), item('b', 'B', 'Aldi')]
    let fail!: (e: unknown) => void
    apiDelete.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    const removing = deleteStore('Aldi')
    // Meanwhile "b" is re-assigned by hand and Migros is deleted elsewhere.
    items.value = items.value.map(i => (i.id === 'b' ? { ...i, store: 'Coop' } : i))
    stores.value = stores.value.filter(s => s.name !== 'Migros')
    fail(new Error('boom'))
    await expect(removing).rejects.toThrow('boom')
    expect(stores.value.map(s => s.name)).toEqual(['Aldi'])
    expect(items.value.map(i => i.store)).toEqual(['Aldi', 'Coop'])
  })

  it('a failed store rename puts back only what it moved', async () => {
    stores.value = [{ name: 'Aldi', sort_order: 0 }]
    items.value = [item('a', 'A', 'Aldi')]
    let fail!: (e: unknown) => void
    apiPatch.mockImplementation(() => new Promise((_, rej) => { fail = rej }))
    const renaming = renameStore('Aldi', 'Lidl')
    items.value = [...items.value, item('n', 'New', null)]
    fail(new Error('boom'))
    await expect(renaming).rejects.toThrow('boom')
    expect(stores.value.map(s => s.name)).toEqual(['Aldi'])
    expect(items.value.map(i => [i.id, i.store])).toEqual([['a', 'Aldi'], ['n', null]])
  })
})

describe('deleteItems — keepalive flush', () => {
  it('with keepalive sends every DELETE at once (no batching) with keepalive', async () => {
    items.value = Array.from({ length: 12 }, (_, i) => item(`k${i}`, `x${i}`, null))
    let inFlight = 0
    let peak = 0
    apiDelete.mockImplementation(async () => {
      inFlight++; peak = Math.max(peak, inFlight)
      await Promise.resolve()
      inFlight--
    })
    await deleteItems(items.value.map(i => i.id), { keepalive: true })
    expect(peak).toBe(12)
    expect(apiDelete.mock.calls.every(c => c[1]?.keepalive === true)).toBe(true)
  })
})

describe('loadShopping — force + late responses', () => {
  beforeEach(() => { shoppingLoaded.value = false; resetPendingDeletes() })

  it('force starts a fresh request even while one is in flight, and the newest wins', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    apiGet.mockImplementation((url: string) => new Promise((res) => {
      resolvers.push(() => res(url.includes('/stores') ? [] : [item(`gen${resolvers.length}`, 'x', null)]))
    }))
    const first = loadShopping()
    const second = loadShopping({ force: true })
    expect(second).not.toBe(first)
    expect(apiGet).toHaveBeenCalledTimes(4)
    // Newer answers first, the old one lands late — it must not win.
    resolvers[2](undefined); resolvers[3](undefined)
    await second
    const fresh = items.value.map(i => i.id)
    resolvers[0](undefined); resolvers[1](undefined)
    await first
    expect(items.value.map(i => i.id)).toEqual(fresh)
  })

  it('a late response cannot resurrect rows committed or pending meanwhile', async () => {
    let answer!: () => void
    apiGet.mockImplementation((url: string) => new Promise((res) => {
      if (url.includes('/stores')) return res([])
      answer = () => res([item('x', 'X', null), item('y', 'Y', null), item('z', 'Z', null)])
    }))
    items.value = [item('x', 'X', null), item('y', 'Y', null), item('z', 'Z', null)]
    const loading = loadShopping()
    apiDelete.mockResolvedValue(undefined)
    await deleteItems(['x'])
    undoableDelete({ ids: ['y'], message: 'D', commit: async () => {} })
    answer()
    await loading
    expect(items.value.map(i => i.id)).toEqual(['z'])
    expect(pendingDeletes.value.has('y')).toBe(true)
  })
})

describe('resetShopping (logout)', () => {
  it('clears items, stores, the loaded flag and pending deletes', () => {
    items.value = [item('a', 'A', null)]
    stores.value = [{ name: 'Aldi', sort_order: 0 }]
    shoppingLoaded.value = true
    undoableDelete({ ids: ['a'], message: 'D', commit: async () => {} })
    resetShopping()
    expect(items.value).toEqual([])
    expect(stores.value).toEqual([])
    expect(shoppingLoaded.value).toBe(false)
    expect(pendingDeletes.value.size).toBe(0)
  })
})
