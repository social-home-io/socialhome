import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { ShoppingItem, ShoppingStore } from '@/types'

export const items = signal<ShoppingItem[]>([])
export const stores = signal<ShoppingStore[]>([])

/** What ``PATCH /api/shopping/stores/{name}`` answers. A rename onto a
 *  name another store already holds is a MERGE, not a conflict — the
 *  old store's items fold onto the survivor and ``new_name`` carries
 *  the spelling that SURVIVED (the target's existing casing, not the
 *  casing the caller typed). */
export interface StoreRenameResult {
  old_name: string
  new_name: string
  merged: boolean
  moved_items: number
}

/** Case-insensitive store-name compare, folding **exactly** what the
 *  server folds.
 *
 *  Store names are unique case-insensitively server-side, but SQLite's
 *  ``NOCASE`` (and its ``lower()``, and migration 0048's repair) fold
 *  **ASCII only** — ``"Müller"`` and ``"MÜLLER"`` are two legitimate,
 *  distinct stores. JS ``toLowerCase()`` folds the full Unicode range
 *  and would call them equal, which is NOT a harmless over-eagerness:
 *  :func:`applyStoreRename` would collapse both catalogue rows into
 *  one name, leaving two entries with an identical ``key`` and items
 *  shown under a store the server never moved them to. So fold ASCII
 *  and nothing else — all four layers (migration, index, repo,
 *  client) then draw the line in the same place. */
export function sameName(
  a: string | null | undefined,
  b: string | null | undefined,
): boolean {
  if (!a || !b) return false
  return asciiFold(a) === asciiFold(b)
}

/** ASCII-only lowercase — the JS equivalent of SQLite ``NOCASE``. */
function asciiFold(value: string): string {
  return value.replace(/[A-Z]/g, (c) => c.toLowerCase())
}

/** Apply a (possibly merging) store rename to the local signals.
 *
 *  Shared by the tab that issued the PATCH and by the sibling tab that
 *  only sees the ``store_renamed`` WS frame, so both converge on the
 *  same state: exactly one catalogue row named ``newName`` carrying the
 *  SURVIVOR's ``sort_order``, and every item that pointed at either
 *  spelling now pointing at ``newName``. Idempotent — re-applying with
 *  a different casing just reconciles the spelling. */
function _applyStoreRename(oldName: string, newName: string): void {
  if (sameName(oldName, newName)) {
    // Pure case change — the server renames the row in place, so there
    // is nothing to collapse, only a spelling to adopt.
    stores.value = stores.value.map(s =>
      sameName(s.name, oldName) ? { ...s, name: newName } : s,
    )
  } else {
    const survivor = stores.value.find(s => sameName(s.name, newName))
    if (survivor) {
      // Merge: the target row wins (its ``sort_order`` is where the
      // household dragged it); the old row disappears.
      stores.value = stores.value
        .filter(s => !sameName(s.name, oldName))
        .map(s => (sameName(s.name, newName) ? { ...s, name: newName } : s))
    } else {
      stores.value = stores.value.map(s =>
        sameName(s.name, oldName) ? { ...s, name: newName } : s,
      )
    }
  }
  items.value = items.value.map(i =>
    sameName(i.store, oldName) || sameName(i.store, newName)
      ? { ...i, store: newName }
      : i,
  )
}

export async function loadShopping() {
  // Include completed so the "Re-add recent" suggestion chips have
  // something to show. The component sorts by `completed` for render.
  // Stores are fetched in parallel — the grouped view needs them
  // before the first paint to render section headers in trip order.
  const [itemsResp, storesResp] = await Promise.all([
    api.get('/api/shopping?include_completed=true'),
    api.get('/api/shopping/stores'),
  ])
  items.value = itemsResp as ShoppingItem[]
  stores.value = storesResp as ShoppingStore[]
}

export async function addItem(text: string, store: string | null = null) {
  // Optimistic by way of WS fan-out: the POST response also comes back
  // via shopping_list.item_added for every other device in the
  // household. The caller need not append locally — the WS listener
  // upserts the item by id (idempotent).
  const body: Record<string, unknown> = { text }
  if (store) body.store = store
  const item = await api.post('/api/shopping', body)
  _upsert(item as ShoppingItem)
  // The server auto-upserts a catalogue row on first sighting; pull
  // the fresh list so the new section appears immediately on the page
  // that added it (the WS fan-out doesn't carry a "new store" frame —
  // adding a new store is unambiguous from ``item.store``).
  if (store && !stores.value.some(s => sameName(s.name, store))) {
    await reloadStores()
  }
}

export async function updateItem(
  id: string,
  patch: { text?: string; store?: string | null },
) {
  const prev = items.value
  // Optimistic patch — reconcile from WS event / error.
  items.value = items.value.map((i) =>
    i.id === id ? { ...i, ...patch } : i,
  )
  try {
    const fresh = await api.patch(`/api/shopping/${id}`, patch)
    _upsert(fresh as ShoppingItem)
    if (patch.store && !stores.value.some(s => sameName(s.name, patch.store))) {
      await reloadStores()
    }
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function toggleItem(id: string, nextCompleted: boolean) {
  const prev = items.value
  // Optimistic local update — reconcile from WS event / error.
  items.value = items.value.map((i) =>
    i.id === id ? { ...i, completed: nextCompleted } : i,
  )
  try {
    await api.patch(
      `/api/shopping/${id}/${nextCompleted ? 'complete' : 'uncomplete'}`,
    )
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function deleteItem(id: string) {
  const prev = items.value
  items.value = items.value.filter((i) => i.id !== id)
  try {
    await api.delete(`/api/shopping/${id}`)
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function clearCompleted() {
  const prev = items.value
  items.value = items.value.filter((i) => !i.completed)
  try {
    await api.post('/api/shopping/clear-completed', {})
  } catch (err) {
    items.value = prev
    throw err
  }
}

export async function createStore(name: string): Promise<ShoppingStore> {
  const trimmed = name.trim()
  if (!trimmed) {
    throw new Error('store name must be non-empty')
  }
  // The endpoint is idempotent case-insensitively: an existing store
  // comes back unchanged (same casing, same ``sort_order``) rather
  // than conflicting, so reconcile by name instead of appending
  // blindly — otherwise "bakery" would fork a second "Bakery" row.
  const store = (await api.post('/api/shopping/stores', {
    name: trimmed,
  })) as ShoppingStore
  const existing = stores.value.findIndex(s => sameName(s.name, store.name))
  if (existing >= 0) {
    stores.value = stores.value.map((s, i) => (i === existing ? store : s))
  } else {
    stores.value = [...stores.value, store]
  }
  return store
}

export async function renameStore(
  oldName: string,
  newName: string,
): Promise<StoreRenameResult> {
  const trimmedOld = oldName.trim()
  const trimmedNew = newName.trim()
  if (!trimmedOld || !trimmedNew) {
    throw new Error('store names must be non-empty')
  }
  // Only an EXACT match is a no-op: a pure case change ("migros" →
  // "Migros") is real work, since the server renames the row in place.
  if (trimmedOld === trimmedNew) {
    return {
      old_name: trimmedOld,
      new_name: trimmedNew,
      merged: false,
      moved_items: 0,
    }
  }
  const prevStores = stores.value
  const prevItems = items.value
  // Optimistic — rename (and, when the target already exists, merge
  // onto) the catalogue row plus every item that referenced either
  // spelling. The WS frame the server sends back is then a no-op.
  _applyStoreRename(trimmedOld, trimmedNew)
  try {
    const result = (await api.patch(
      `/api/shopping/stores/${encodeURIComponent(trimmedOld)}`,
      { name: trimmedNew },
    )) as StoreRenameResult
    // The survivor's spelling may differ from what the caller typed
    // (renaming "Aldi" → "migros" when "Migros" exists keeps
    // "Migros"). Re-apply with the server's names so the catalogue and
    // every affected item carry the canonical casing.
    _applyStoreRename(result.old_name, result.new_name)
    return result
  } catch (err) {
    stores.value = prevStores
    items.value = prevItems
    throw err
  }
}

export async function deleteStore(name: string): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed) return
  const prevStores = stores.value
  const prevItems = items.value
  // Optimistic — drop the catalogue row + clear ``store`` on every
  // item that referenced it (rows fall into the "No store" bucket).
  stores.value = prevStores.filter(s => !sameName(s.name, trimmed))
  items.value = prevItems.map(i =>
    sameName(i.store, trimmed) ? { ...i, store: null } : i,
  )
  try {
    await api.delete(`/api/shopping/stores/${encodeURIComponent(trimmed)}`)
  } catch (err) {
    stores.value = prevStores
    items.value = prevItems
    throw err
  }
}

export async function reorderStores(orderedNames: string[]) {
  const prev = stores.value
  // Optimistic: shuffle the local store list so the section headers
  // animate to their new positions immediately. The server returns
  // the canonical post-reorder list which we then replace verbatim.
  const byName = new Map(prev.map(s => [s.name, s]))
  const optimistic: ShoppingStore[] = []
  orderedNames.forEach((name, i) => {
    const existing = byName.get(name)
    if (existing) optimistic.push({ name, sort_order: i })
  })
  // Carry over any rows the caller forgot — they shift past the end,
  // mirroring the server-side rule.
  prev.forEach((s) => {
    if (!orderedNames.includes(s.name)) {
      optimistic.push({ name: s.name, sort_order: optimistic.length })
    }
  })
  stores.value = optimistic
  try {
    const fresh = (await api.put('/api/shopping/stores/order', {
      order: orderedNames,
    })) as ShoppingStore[]
    stores.value = fresh
  } catch (err) {
    stores.value = prev
    throw err
  }
}

async function reloadStores() {
  stores.value = (await api.get('/api/shopping/stores')) as ShoppingStore[]
}

function _upsert(item: ShoppingItem) {
  const existing = items.value.findIndex((i) => i.id === item.id)
  if (existing >= 0) {
    items.value = items.value.map((i) =>
      i.id === item.id ? { ...i, ...item } : i,
    )
  } else {
    items.value = [...items.value, item]
  }
}

// ─── WS event handlers (§23.120.3, local household only) ────────────────

let _wired = false

/** Wire the shopping_list.* events into the local store so other
 *  clients' changes appear without a manual refresh. Idempotent. */
export function wireShoppingWs() {
  if (_wired) return
  _wired = true
  ws.on('shopping_list.item_added', (e) => {
    const item = e.data as unknown as ShoppingItem
    _upsert(item)
    // A new store name from a sibling tab → refresh the catalogue
    // (the server-side ``touch_store`` already wrote the row; we
    // just don't have it yet on the read side).
    if (item.store && !stores.value.some(s => sameName(s.name, item.store))) {
      void reloadStores()
    }
  })
  ws.on('shopping_list.item_updated', (e) => {
    const patch = e.data as unknown as Partial<ShoppingItem> & { id: string }
    items.value = items.value.map((i) =>
      i.id === patch.id ? { ...i, ...patch } : i,
    )
    if (patch.store && !stores.value.some(s => sameName(s.name, patch.store))) {
      void reloadStores()
    }
  })
  ws.on('shopping_list.item_removed', (e) => {
    const id = (e.data as { id: string }).id
    items.value = items.value.filter((i) => i.id !== id)
  })
  ws.on('shopping_list.cleared', () => {
    items.value = items.value.filter((i) => !i.completed)
  })
  ws.on('shopping_list.stores_reordered', (e) => {
    const order = (e.data as { order: string[] }).order
    // ``order`` is the canonical post-reorder name sequence — the
    // server only carries the new index, not any per-store metadata,
    // so we just rebuild the local catalogue from it.
    stores.value = order.map((name, i) => ({ name, sort_order: i }))
  })
  ws.on('shopping_list.store_renamed', (e) => {
    const { old_name, new_name } = e.data as {
      old_name: string
      new_name: string
    }
    if (!old_name || !new_name || old_name === new_name) return
    // May be a merge — ``new_name`` can already be in the catalogue.
    // Collapse to one row rather than leaving two identical entries.
    _applyStoreRename(old_name, new_name)
  })
  ws.on('shopping_list.store_deleted', (e) => {
    const { name } = e.data as { name: string }
    if (!name) return
    stores.value = stores.value.filter(s => !sameName(s.name, name))
    items.value = items.value.map(i =>
      sameName(i.store, name) ? { ...i, store: null } : i,
    )
  })
}
