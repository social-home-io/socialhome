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
  wireShoppingWs,
} from './shopping'
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
