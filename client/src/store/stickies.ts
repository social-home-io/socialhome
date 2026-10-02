/**
 * Stickies store — sticky notes (§19), one store per scope.
 *
 * ``createStickyStore(spaceId)`` builds the store for the household
 * (``null`` → ``/api/stickies``) or one space
 * (``/api/spaces/{id}/stickies``). ``householdStickyStore`` is the
 * household's; ``spaceStickyStore(id)`` makes a space's on first use
 * and keeps it, so a space board never shares a signal with the
 * household board (or another space). Modelled on ``store/tasks.ts``.
 *
 * - **Loads** are deduped: a call while a request is in flight shares
 *   it, and a board fetched a moment ago (``FRESH_MS``) is not fetched
 *   again — the Organize hub's count chip and the Stickies tab mounting
 *   together fetch once. ``force`` skips both; ``ensure`` loads only
 *   when never loaded.
 * - **Edits and moves** are optimistic. A failure rolls back only the
 *   fields of that one row that still hold our optimistic value, so it
 *   never reverts another row or a newer WS update, then revalidates.
 *   While a move is in progress (``holdPosition``) server positions for
 *   that note are ignored. A move is applied
 *   locally while dragging (``moveLocal``) and sent once on release
 *   (``commitMove``) with the position the drag started from.
 * - **Deletes** go through ``undoableDelete``: hidden at once (``visible``
 *   filters ``pendingDeletes``), the DELETE is sent when the Undo toast
 *   expires; a 404 counts as gone. Ids deleted here stay out of late WS
 *   echoes and list responses for a minute.
 * - **WS frames** route by ``space_id``: none → the household store, a
 *   space id → that space's store if one was made. A reconnect
 *   revalidates every store that had loaded.
 * - **Held for review** (a space's stickies are "Reviewed", §4.3): a
 *   ``202 {queued: true}`` saved nothing — an edit is rolled back, a
 *   delete leaves the note on the board, a create resolves ``null``; the
 *   "Submitted for review" toast shows (``utils/contentWrite``).
 */
import { computed, signal, type ReadonlySignal, type Signal } from '@preact/signals'
import { api } from '@/api'
import { connectionState, ws } from '@/ws'
import { t } from '@/i18n/i18n'
import { pendingDeletes, undoableDelete } from '@/utils/undoableDelete'
import { announceQueued, isQueuedWrite } from '@/utils/contentWrite'

export interface StickyRow {
  id:         string
  author:     string
  content:    string
  color:      string
  position_x: number
  position_y: number
  created_at: string
  updated_at: string
  space_id:   string | null
}

/** Fields a PATCH may change. */
export type StickyPatch = Partial<Pick<StickyRow, 'content' | 'color' | 'position_x' | 'position_y'>>

export interface StickyDraft {
  content: string
  color: string
  position_x: number
  position_y: number
}

export interface LoadOpts {
  /** Fetch even when a request is in flight or just finished. */
  force?: boolean
}

export interface StickyStore {
  spaceId: string | null
  /** Every known sticky of this scope (including pending deletes). */
  rows: Signal<StickyRow[]>
  /** The board has loaded at least once. */
  loaded: Signal<boolean>
  /** ``rows`` minus stickies hidden behind an Undo toast. */
  visible: ReadonlySignal<StickyRow[]>

  load(opts?: LoadOpts): Promise<void>
  ensure(): Promise<void>
  /** Reload if loaded before (after a reconnect). */
  revalidate(): Promise<void>

  find(id: string): StickyRow | undefined
  /** ``null`` when the new note is held for review. */
  create(draft: StickyDraft): Promise<StickyRow | null>
  /** Optimistic PATCH. ``before`` overrides the rollback values (a
   *  drag already moved the row locally). */
  patch(id: string, patch: StickyPatch, before?: StickyPatch): Promise<StickyRow | null>
  /** Move a sticky on screen only (while dragging / arrow-keying). */
  moveLocal(id: string, x: number, y: number): void
  /** Send the current position; on failure put it back to ``from``. */
  commitMove(id: string, from: { x: number; y: number }): Promise<void>
  /** A drag / keyboard move of ``id`` is in progress: until released,
   *  positions from WS frames and loads are ignored for it. */
  holdPosition(id: string): void
  releasePosition(id: string): void
  remove(sticky: StickyRow, cb?: { onUndone?: () => void }): void

  onUpsert(frame: Partial<StickyRow> & { id: string }, create: boolean): void
  onDeleted(id: string): void
  reset(): void
}

/** A fetch this recent is reused instead of repeated. */
const FRESH_MS = 2_000
/** How long a deleted id stays out of late responses / WS echoes. */
const RECENTLY_GONE_MS = 60_000

const ROW_FIELDS: ReadonlyArray<keyof StickyRow> = [
  'id', 'author', 'content', 'color', 'position_x', 'position_y',
  'created_at', 'updated_at', 'space_id',
]

/** Keep only sticky fields (a WS frame also carries ``type``). */
function pick(frame: Partial<StickyRow>): Partial<StickyRow> {
  const out: Record<string, unknown> = {}
  for (const k of ROW_FIELDS) if (frame[k] !== undefined) out[k] = frame[k]
  return out as Partial<StickyRow>
}

function isNotFound(err: unknown): boolean {
  return (err as { status?: unknown } | null)?.status === 404
}

/** A store for one scope: ``null`` = the household, else a space id. */
export function createStickyStore(spaceId: string | null): StickyStore {
  const base = spaceId === null
    ? '/api/stickies'
    : `/api/spaces/${encodeURIComponent(spaceId)}/stickies`
  const itemPath = (id: string) => `${base}/${encodeURIComponent(id)}`

  const rows = signal<StickyRow[]>([])
  const loaded = signal(false)
  const visible = computed(() => {
    const hidden = pendingDeletes.value
    return rows.value.filter(r => !hidden.has(r.id))
  })

  /** Bumped by ``reset`` so a request in flight then never writes. */
  let epoch = 0
  let inflight: Promise<void> | null = null
  let fetchedAt = 0
  let gen = 0
  const recentlyGone = new Map<string, number>()
  const patchSeq = new Map<string, number>()
  /** Ids being moved here right now (ref-counted). A WS frame or a load
   *  landing mid-drag carries the OLD server position (or another
   *  member's); applying it would yank the note from under the pointer
   *  and the release would then commit a jump. Other fields still apply. */
  const held = new Map<string, number>()

  function withoutHeldPosition<T extends Partial<StickyRow>>(r: T & { id: string }): T {
    if (!held.has(r.id)) return r
    const rest = { ...r }
    delete rest.position_x
    delete rest.position_y
    return rest
  }

  function isGone(id: string): boolean {
    const at = recentlyGone.get(id)
    if (at === undefined) return false
    if (Date.now() - at > RECENTLY_GONE_MS) {
      recentlyGone.delete(id)
      return false
    }
    return true
  }

  function find(id: string): StickyRow | undefined {
    return rows.value.find(r => r.id === id)
  }

  function mapRow(id: string, fn: (r: StickyRow) => StickyRow) {
    if (!find(id)) return
    rows.value = rows.value.map(r => r.id === id ? fn(r) : r)
  }

  /** Merge a server row / frame in. Only ``create`` may add a new row —
   *  a partial update frame for an unknown id is not a whole sticky. */
  function upsert(frame: Partial<StickyRow> & { id: string }, create: boolean) {
    if (!frame?.id || isGone(frame.id)) return
    const fields = pick(find(frame.id) ? withoutHeldPosition(frame) : frame)
    if (find(frame.id)) {
      mapRow(frame.id, r => ({ ...r, ...fields }))
    } else if (create) {
      rows.value = [...rows.value, {
        author: '', content: '', color: '#FFF9B1', position_x: 0, position_y: 0,
        created_at: '', updated_at: '', space_id: spaceId,
        ...fields,
      } as StickyRow]
    }
  }

  function load({ force = false }: LoadOpts = {}): Promise<void> {
    if (!force && inflight) return inflight
    if (!force && loaded.value && Date.now() - fetchedAt < FRESH_MS) return Promise.resolve()
    const mine = ++gen
    const myEpoch = epoch
    const run: Promise<void> = (async () => {
      const list = await api.get(base) as StickyRow[]
      if (myEpoch !== epoch || mine !== gen) return
      rows.value = list.filter(r => !isGone(r.id)).map((r) => {
        const local = held.has(r.id) ? find(r.id) : undefined
        return local ? { ...r, position_x: local.position_x, position_y: local.position_y } : r
      })
      loaded.value = true
      fetchedAt = Date.now()
    })().finally(() => {
      if (inflight === run) inflight = null
    })
    inflight = run
    return run
  }

  function ensure(): Promise<void> {
    if (loaded.value) return Promise.resolve()
    return inflight ?? load()
  }

  async function revalidate(): Promise<void> {
    if (!loaded.value) return
    await load({ force: true }).catch(() => { /* the board keeps what it has */ })
  }

  async function create(draft: StickyDraft): Promise<StickyRow | null> {
    const res = await api.post(base, draft) as unknown
    if (isQueuedWrite(res)) {
      announceQueued({ spaceId })
      return null
    }
    const row = res as StickyRow
    upsert(row, true)
    return row
  }

  /** Put back the fields we changed on ONE row, only where they still
   *  hold our optimistic value. Never re-creates a removed row. */
  function rollback(id: string, before: StickyPatch, optimistic: StickyPatch) {
    mapRow(id, (r) => {
      const next = { ...r }
      for (const k of Object.keys(optimistic) as (keyof StickyPatch)[]) {
        if (r[k] === optimistic[k] && before[k] !== undefined) {
          (next as Record<string, unknown>)[k] = before[k]
        }
      }
      return next
    })
  }

  async function patch(
    id: string, change: StickyPatch, beforeOverride?: StickyPatch,
  ): Promise<StickyRow | null> {
    const row = find(id)
    if (!row) return null
    const before: StickyPatch = {}
    for (const k of Object.keys(change) as (keyof StickyPatch)[]) {
      (before as Record<string, unknown>)[k] = beforeOverride?.[k] ?? row[k]
    }
    mapRow(id, r => ({ ...r, ...change }))
    const seq = (patchSeq.get(id) ?? 0) + 1
    patchSeq.set(id, seq)
    try {
      const res = await api.patch(itemPath(id), change) as unknown
      if (isQueuedWrite(res)) {
        // Someone else's note in a "Reviewed" space: put it back.
        rollback(id, before, change)
        announceQueued({ spaceId })
        return null
      }
      const fresh = res as StickyRow
      if (fresh?.id && patchSeq.get(id) === seq) upsert(fresh, false)
      return fresh
    } catch (err) {
      rollback(id, before, change)
      // Two overlapping failed PATCHes can each "restore" the other's
      // optimistic value; refetch so the board ends on the server's.
      void revalidate()
      throw err
    }
  }

  function holdPosition(id: string): void {
    held.set(id, (held.get(id) ?? 0) + 1)
  }

  function releasePosition(id: string): void {
    const n = (held.get(id) ?? 0) - 1
    if (n > 0) held.set(id, n)
    else held.delete(id)
  }

  function moveLocal(id: string, x: number, y: number): void {
    mapRow(id, r => ({ ...r, position_x: x, position_y: y }))
  }

  async function commitMove(id: string, from: { x: number; y: number }): Promise<void> {
    const row = find(id)
    if (!row || (row.position_x === from.x && row.position_y === from.y)) return
    await patch(
      id,
      { position_x: row.position_x, position_y: row.position_y },
      { position_x: from.x, position_y: from.y },
    )
  }

  function onDeleted(id: string) {
    recentlyGone.set(id, Date.now())
    if (find(id)) rows.value = rows.value.filter(r => r.id !== id)
  }

  function remove(sticky: StickyRow, cb: { onUndone?: () => void } = {}): void {
    const snippet = sticky.content.trim().replace(/\s+/g, ' ')
    undoableDelete({
      ids: [sticky.id],
      message: t('stickies.deleted', {
        text: snippet.length > 40 ? `${snippet.slice(0, 40)}…` : snippet,
      }),
      commit: async ({ keepalive }) => {
        try {
          const res = await (keepalive
            ? api.delete<unknown>(itemPath(sticky.id), { keepalive: true })
            : api.delete<unknown>(itemPath(sticky.id)))
          // Held for review: the note stays until a moderator approves.
          if (isQueuedWrite(res)) {
            announceQueued({ spaceId })
            return
          }
        } catch (err) {
          if (!isNotFound(err)) throw err
        }
        onDeleted(sticky.id)
      },
      onUndone: cb.onUndone,
    })
  }

  function reset() {
    epoch++
    inflight = null
    fetchedAt = 0
    recentlyGone.clear()
    patchSeq.clear()
    held.clear()
    rows.value = []
    loaded.value = false
  }

  return {
    spaceId, rows, loaded, visible,
    load, ensure, revalidate,
    find, create, patch, moveLocal, commitMove, holdPosition, releasePosition, remove,
    onUpsert: upsert, onDeleted, reset,
  }
}

/** The household's sticky board. */
export const householdStickyStore = createStickyStore(null)

const spaceStores = new Map<string, StickyStore>()

/** One space's sticky board (made on first use, then kept). */
export function spaceStickyStore(spaceId: string): StickyStore {
  let s = spaceStores.get(spaceId)
  if (!s) {
    s = createStickyStore(spaceId)
    spaceStores.set(spaceId, s)
  }
  return s
}

/** The store for a scope: ``null`` = household. */
export function stickyStoreFor(spaceId: string | null): StickyStore {
  return spaceId === null ? householdStickyStore : spaceStickyStore(spaceId)
}

/** The Organize hub's "Stickies · N": household notes, minus pending
 *  deletes; ``null`` until the household board loaded. */
export const householdStickyCount = computed(() =>
  householdStickyStore.loaded.value ? householdStickyStore.visible.value.length : null)

/** Logout: forget every scope's stickies. */
export function resetStickies(): void {
  householdStickyStore.reset()
  for (const s of spaceStores.values()) s.reset()
  spaceStores.clear()
}

// ─── WebSocket ─────────────────────────────────────────────────────

let _wired = false

/** Route ``sticky.*`` frames to their scope's store. Idempotent. */
export function wireStickiesWs(): void {
  if (_wired) return
  _wired = true
  type Frame = Partial<StickyRow> & { id?: string; space_id?: string | null }
  const target = (d: Frame): StickyStore | undefined =>
    d.space_id == null ? householdStickyStore : spaceStores.get(d.space_id)

  ws.on('sticky.created', (e) => {
    const d = e.data as Frame
    if (d.id) target(d)?.onUpsert(d as Frame & { id: string }, true)
  })
  ws.on('sticky.updated', (e) => {
    const d = e.data as Frame
    if (d.id) target(d)?.onUpsert(d as Frame & { id: string }, false)
  })
  ws.on('sticky.deleted', (e) => {
    const d = e.data as Frame
    if (d.id) target(d)?.onDeleted(d.id)
  })

  let prev = connectionState.value
  connectionState.subscribe((next) => {
    if (prev === 'reconnecting' && next === 'open') {
      void householdStickyStore.revalidate()
      for (const s of spaceStores.values()) void s.revalidate()
    }
    prev = next
  })
}
