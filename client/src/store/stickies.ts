/**
 * Stickies store — household + space-scoped sticky notes (§19).
 *
 * Canonical row shape matches the backend (``content`` + ``position_x``
 * / ``position_y``), and the WS handlers now actually merge server
 * frames into the signal — prior to §SX1 the backend didn't publish
 * anything and this store was a placeholder.
 */
import { computed, signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'

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

/** All known stickies for the current scope. The page component sets
 * this from the REST list, WS handlers merge live updates in.   */
export const stickies = signal<StickyRow[]>([])

/** Scope of the sticky board currently mounted: a ``space_id`` for a
 *  space board, or ``null`` for the household board. The ``stickies``
 *  signal feeds BOTH boards (``StickyBoardPage``), so WS handlers below
 *  short-circuit when an inbound frame's scope doesn't match — without
 *  this gate, a sticky created on a space board leaks into the
 *  household board (and vice versa). The page owns this signal: set on
 *  load, reset to ``null`` on unmount. Mirrors calendar.ts's
 *  ``activeCalendarScope``. */
export const activeStickyScope = signal<string | null>(null)

/** True when an inbound sticky frame's scope (``space_id``, ``null`` =
 *  household) matches the currently-mounted board. */
function _scopedToActive(spaceId: string | null | undefined): boolean {
  return (spaceId ?? null) === activeStickyScope.value
}

// ─── Household count (the Organize hub's "Stickies · N" chip) ─────────
//
// ``stickies`` may hold a SPACE board's notes (the last board opened),
// so the hub can't count it. The household's ids are kept apart here:
// loaded from ``/api/stickies`` (the household board loads through
// ``loadHouseholdStickies`` too, so the two share one request) and kept
// current by household-scoped WS frames and local adds / deletes,
// whatever board is mounted. The per-scope sticky cache that replaces
// this is a follow-up.

/** Household sticky ids; ``null`` until loaded. */
const householdIds = signal<ReadonlySet<string> | null>(null)
let _householdInflight: Promise<StickyRow[]> | null = null
let _householdGen = 0

/** How many household stickies there are; ``null`` while unknown. */
export const householdStickyCount = computed(() => householdIds.value?.size ?? null)

/** GET the household board (shared while in flight) and note its ids. */
export function loadHouseholdStickies(): Promise<StickyRow[]> {
  if (_householdInflight) return _householdInflight
  const gen = _householdGen
  const run = (async () => {
    const rows = await api.get('/api/stickies') as StickyRow[]
    if (gen === _householdGen) householdIds.value = new Set(rows.map(r => r.id))
    return rows
  })().finally(() => {
    if (_householdInflight === run) _householdInflight = null
  })
  _householdInflight = run
  return run
}

/** Load the household count unless it is known (or loading). */
export async function ensureHouseholdStickies(): Promise<void> {
  if (householdIds.value !== null) return
  await loadHouseholdStickies()
}

/** A household sticky was added (``true``) or deleted here. */
export function trackHouseholdSticky(id: string, present: boolean): void {
  const ids = householdIds.value
  if (ids === null || ids.has(id) === present) return
  const next = new Set(ids)
  if (present) next.add(id)
  else next.delete(id)
  householdIds.value = next
}

/** Logout: forget the count. */
export function resetHouseholdStickies(): void {
  _householdGen++
  _householdInflight = null
  householdIds.value = null
}

export function wireStickiesWs(): void {
  ws.on('sticky.created', (e) => {
    const s = e.data as unknown as StickyRow
    if ((s.space_id ?? null) === null) trackHouseholdSticky(s.id, true)
    if (!_scopedToActive(s.space_id)) return
    if (!stickies.value.some((x) => x.id === s.id)) {
      stickies.value = [...stickies.value, s]
    }
  })
  ws.on('sticky.updated', (e) => {
    const u = e.data as unknown as Partial<StickyRow> & { id: string }
    if (!_scopedToActive(u.space_id)) return
    stickies.value = stickies.value.map((x) =>
      x.id === u.id ? { ...x, ...u } : x,
    )
  })
  ws.on('sticky.deleted', (e) => {
    const { id, space_id } = e.data as { id: string; space_id?: string | null }
    if ((space_id ?? null) === null) trackHouseholdSticky(id, false)
    if (!_scopedToActive(space_id)) return
    stickies.value = stickies.value.filter((x) => x.id !== id)
  })
}
