import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { ShoppingItem, ShoppingStore } from '@/types'
import { pendingDeletes, resetPendingDeletes } from '@/utils/undoableDelete'

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

/** ``true`` once a load has succeeded — the page can render the
 *  signals straight away on the next visit and revalidate behind. */
export const shoppingLoaded = signal(false)

let _inflight: Promise<void> | null = null
/** Bumped per load; only the newest load may write the signals. */
let _loadGen = 0

/** Ids this tab deleted recently, with when. A list response that was
 *  already on its way when the DELETE landed would otherwise put the
 *  row back. Kept for a minute — far longer than any request. */
const _recentlyGone = new Map<string, number>()
const RECENTLY_GONE_MS = 60_000

function _markGone(ids: Iterable<string>) {
  const now = Date.now()
  for (const id of ids) _recentlyGone.set(id, now)
}

function _isRecentlyGone(id: string): boolean {
  const at = _recentlyGone.get(id)
  if (at === undefined) return false
  if (Date.now() - at > RECENTLY_GONE_MS) {
    _recentlyGone.delete(id)
    return false
  }
  return true
}

/** Fetch items + stores. Concurrent callers (the Organize hub's count
 *  chip and the Shopping tab mounting together) share ONE in-flight
 *  request pair instead of fetching twice; ``force`` (a reconnect
 *  revalidation) starts a fresh pair anyway, and only the newest load
 *  writes the signals. Rows deleted meanwhile — committed by this tab,
 *  or hidden behind an Undo toast — are filtered out of the answer
 *  (an Undo re-inserts its own rows). Rejects on failure so the
 *  caller can show an error + Retry. */
export function loadShopping({ force = false }: { force?: boolean } = {}): Promise<void> {
  if (_inflight && !force) return _inflight
  const gen = ++_loadGen
  const run: Promise<void> = (async () => {
    // Include completed so the "Re-add recent" suggestion chips have
    // something to show. Stores are fetched in parallel — the grouped
    // view needs them before the first paint (section order).
    const [itemsResp, storesResp] = await Promise.all([
      api.get('/api/shopping?include_completed=true'),
      api.get('/api/shopping/stores'),
    ])
    if (gen !== _loadGen) return // a newer load owns the signals
    const hidden = pendingDeletes.value
    items.value = (itemsResp as ShoppingItem[])
      .filter(i => !hidden.has(i.id) && !_isRecentlyGone(i.id))
    stores.value = storesResp as ShoppingStore[]
    shoppingLoaded.value = true
  })().finally(() => {
    if (_inflight === run) _inflight = null
  })
  _inflight = run
  return run
}

/** Load unless already loaded (or loading) — for count badges. */
export function ensureShopping(): Promise<void> {
  return shoppingLoaded.value ? Promise.resolve() : loadShopping()
}

/** Logout: forget this household's list. */
export function resetShopping(): void {
  _loadGen++
  _inflight = null
  _recentlyGone.clear()
  items.value = []
  stores.value = []
  shoppingLoaded.value = false
  resetPendingDeletes()
}

/** Put ``rows`` back that aren't in ``items`` (Undo of a hidden delete
 *  whose rows a refetch filtered out meanwhile). */
export function reinsertItems(rows: readonly ShoppingItem[]): void {
  const have = new Set(items.value.map(i => i.id))
  const missing = rows.filter(r => !have.has(r.id))
  if (missing.length) items.value = [...items.value, ...missing]
}

export async function addItem(text: string, store: string | null = null) {
  // The POST response also comes back via shopping_list.item_added for
  // every other device in the household; the WS listener upserts by id
  // (idempotent).
  const body: Record<string, unknown> = { text }
  if (store) body.store = store
  const item = await api.post('/api/shopping', body)
  _upsert(item as ShoppingItem)
  // The server auto-upserts a catalogue row on first sighting; pull
  // the fresh list so the new section appears immediately on the page
  // that added it.
  if (store && !stores.value.some(s => sameName(s.name, store))) {
    await reloadStores()
  }
}

/** Undo an optimistic field change on ONE row: put back the fields
 *  we changed, only where they still hold our optimistic value, and
 *  never re-create a row that was removed meanwhile. Rollbacks never
 *  assign an older ``items`` array — that would resurrect or revert
 *  rows other commits changed while this request was in flight. */
function _rollbackFields(
  id: string,
  before: Partial<ShoppingItem>,
  optimistic: Partial<ShoppingItem>,
) {
  items.value = items.value.map((i) => {
    if (i.id !== id) return i
    const next = { ...i }
    for (const k of Object.keys(optimistic) as (keyof ShoppingItem)[]) {
      if (i[k] === optimistic[k]) (next as Record<string, unknown>)[k] = before[k]
    }
    return next
  })
}

export async function updateItem(
  id: string,
  patch: { text?: string; store?: string | null },
) {
  const row = items.value.find(i => i.id === id)
  const before: Record<string, unknown> = {}
  for (const k of Object.keys(patch) as (keyof typeof patch)[]) before[k] = row?.[k]
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
    if (row) _rollbackFields(id, before as Partial<ShoppingItem>, patch as Partial<ShoppingItem>)
    throw err
  }
}

export async function toggleItem(id: string, nextCompleted: boolean) {
  const row = items.value.find(i => i.id === id)
  items.value = items.value.map((i) =>
    i.id === id ? { ...i, completed: nextCompleted } : i,
  )
  try {
    await api.patch(
      `/api/shopping/${id}/${nextCompleted ? 'complete' : 'uncomplete'}`,
    )
  } catch (err) {
    if (row) _rollbackFields(id, { completed: row.completed }, { completed: nextCompleted })
    throw err
  }
}

/** At most this many DELETEs in flight at once. */
const DELETE_BATCH = 10

/** Delete exactly ``ids`` — one ``DELETE`` each.
 *
 *  The Undo commit for a single delete and for "Clear all": unlike the
 *  atomic ``POST /clear-completed`` it never touches an item ticked
 *  off *after* the user pressed Clear all. Normally at most 10 run at
 *  once; with ``keepalive`` (leaving the page) all go out together,
 *  there may be no "later". A 404 counts as gone. Gone ids drop out of
 *  ``items`` (and stay out of late list responses); any other failure
 *  leaves that item alone and the promise rejects with the first
 *  error. */
export async function deleteItems(
  ids: readonly string[],
  { keepalive = false }: { keepalive?: boolean } = {},
): Promise<void> {
  const gone = new Set<string>()
  let firstError: unknown = null
  const size = keepalive ? Math.max(ids.length, 1) : DELETE_BATCH
  for (let i = 0; i < ids.length; i += size) {
    const batch = ids.slice(i, i + size)
    const results = await Promise.allSettled(
      batch.map(id => keepalive
        ? api.delete(`/api/shopping/${id}`, { keepalive: true })
        : api.delete(`/api/shopping/${id}`)),
    )
    results.forEach((r, j) => {
      const notFound = r.status === 'rejected'
        && (r.reason as { status?: unknown } | null)?.status === 404
      if (r.status === 'fulfilled' || notFound) gone.add(batch[j])
      else if (firstError === null) firstError = r.reason
    })
  }
  _markGone(gone)
  items.value = items.value.filter(i => !gone.has(i.id))
  if (firstError !== null) throw firstError
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

/** Re-insert catalogue rows that are missing now, at their old index. */
function _restoreStoreRows(rows: { row: ShoppingStore; index: number }[]) {
  let next = stores.value
  for (const { row, index } of rows) {
    if (next.some(s => sameName(s.name, row.name))) continue
    next = [...next.slice(0, index), row, ...next.slice(index)]
  }
  stores.value = next
}

/** Put item stores back for the rows WE moved, where they still hold
 *  the value we set (a later hand-edit wins). */
function _restoreItemStores(moved: Map<string, string | null>, setTo: (id: string) => string | null) {
  items.value = items.value.map(i =>
    moved.has(i.id) && (i.store ?? null) === setTo(i.id)
      ? { ...i, store: moved.get(i.id) ?? null }
      : i,
  )
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
  // Remember exactly what the optimistic step changes, so a failure
  // can put back just that.
  const oldIndex = stores.value.findIndex(s => sameName(s.name, trimmedOld))
  const oldRow = oldIndex >= 0 ? stores.value[oldIndex] : null
  const targetExisted = stores.value.find(s => sameName(s.name, trimmedNew)) ?? null
  const moved = new Map<string, string | null>()
  for (const i of items.value) {
    if (sameName(i.store, trimmedOld) || sameName(i.store, trimmedNew)) {
      moved.set(i.id, i.store ?? null)
    }
  }
  _applyStoreRename(trimmedOld, trimmedNew)
  const optimisticName = stores.value.find(s => sameName(s.name, trimmedNew))?.name ?? trimmedNew
  try {
    const result = (await api.patch(
      `/api/shopping/stores/${encodeURIComponent(trimmedOld)}`,
      { name: trimmedNew },
    )) as StoreRenameResult
    // The survivor's spelling may differ from what the caller typed.
    _applyStoreRename(result.old_name, result.new_name)
    return result
  } catch (err) {
    // Catalogue: drop the row the rename created (unless it existed
    // before), put the old row back; then only the items we moved.
    if (!sameName(trimmedOld, trimmedNew)) {
      if (!targetExisted) {
        stores.value = stores.value.filter(s => s.name !== optimisticName)
      } else {
        stores.value = stores.value.map(s =>
          s.name === optimisticName ? { ...s, name: targetExisted.name } : s)
      }
      if (oldRow) _restoreStoreRows([{ row: oldRow, index: oldIndex }])
    } else {
      stores.value = stores.value.map(s =>
        s.name === optimisticName && oldRow ? { ...s, name: oldRow.name } : s)
    }
    _restoreItemStores(moved, () => optimisticName)
    throw err
  }
}

export async function deleteStore(name: string): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed) return
  const index = stores.value.findIndex(s => sameName(s.name, trimmed))
  const row = index >= 0 ? stores.value[index] : null
  const cleared = new Map<string, string | null>()
  for (const i of items.value) {
    if (sameName(i.store, trimmed)) cleared.set(i.id, i.store ?? null)
  }
  // Optimistic — drop the catalogue row + clear ``store`` on every
  // item that referenced it (rows fall into the "No store" bucket).
  stores.value = stores.value.filter(s => !sameName(s.name, trimmed))
  items.value = items.value.map(i =>
    cleared.has(i.id) ? { ...i, store: null } : i,
  )
  try {
    await api.delete(`/api/shopping/stores/${encodeURIComponent(trimmed)}`)
  } catch (err) {
    if (row) _restoreStoreRows([{ row, index }])
    _restoreItemStores(cleared, () => null)
    throw err
  }
}

export async function reorderStores(orderedNames: string[]) {
  const prevOrder = stores.value.map(s => s.name)
  // Optimistic: shuffle the local store list so the section headers
  // move immediately. The server returns the canonical post-reorder
  // list which we then replace verbatim.
  const byName = new Map(stores.value.map(s => [s.name, s]))
  const optimistic: ShoppingStore[] = []
  orderedNames.forEach((name) => {
    if (byName.has(name)) optimistic.push({ name, sort_order: optimistic.length })
  })
  // Carry over any rows the caller forgot — they shift past the end,
  // mirroring the server-side rule.
  stores.value.forEach((s) => {
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
    // Old order for the stores that still exist; stores added
    // meanwhile keep their place at the end.
    const now = new Set(stores.value.map(s => s.name))
    const names = [
      ...prevOrder.filter(n => now.has(n)),
      ...stores.value.map(s => s.name).filter(n => !prevOrder.includes(n)),
    ]
    stores.value = names.map((name, i) => ({ name, sort_order: i }))
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
