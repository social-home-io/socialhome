/**
 * A space's sync status in plain words — "is anything missing?" and "did
 * my post go out?" (``GET /api/spaces/{id}`` ``sync``, kept live by the
 * ``space.sync_state`` frame; members only, a follower gets ``null`` and
 * sees none of this).
 *
 * The server computes the state (``SpaceSyncStatusService``) and the one
 * warning, ``may_be_missing`` ("Some posts may be missing", the DM gap's
 * wording); this module only picks the words. One :type:`SyncView` per
 * status drives every surface: the header chip, the Spaces list's dot,
 * the settings' "Is everything here?" section and its admin Details.
 * Plumbing (pages, parts, outbox counts) stays in those Details.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { isOne, t } from '@/i18n/i18n'
import { connections } from '@/store/connections'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { SpaceSyncPageWire, SpaceSyncWire } from '@/types'
import { ws } from '@/ws'

export type SpaceSync = SpaceSyncWire
export type SpaceSyncState = SpaceSyncWire['state']

/** What the member is told:
 *  * ``here`` — Everything's here (a space hosted here too);
 *  * ``latest`` — Getting the latest… (a stream arriving, or one round
 *    that didn't bring everything: not worth a warning yet);
 *  * ``missing`` — Some posts may be missing (the one warning);
 *  * ``away`` — the host household is away (neutral, settings only);
 *  * ``preparing`` — Getting the space ready (nothing arrived yet). */
export type SyncView = 'here' | 'latest' | 'missing' | 'away' | 'preparing'

export function syncView(sync: SpaceSync, hostedHere = false): SyncView {
  if (hostedHere) return 'here'
  if (sync.may_be_missing) return 'missing'
  switch (sync.state) {
    case 'up_to_date': return 'here'
    case 'catching_up':
    case 'behind': return 'latest'
    case 'host_offline': return 'away'
    case 'never': return 'preparing'
  }
}

/** The icon beside every state's words (never colour alone). */
export const SYNC_ICON: Record<SyncView, string> = {
  here: '✓',
  latest: '↻',
  missing: '⚠',
  away: '☾',
  preparing: '⏳',
}

/** How long "Getting the latest…" waits before the chip shows it: a
 *  normal round ends well before, and nobody needs to see it. */
export const CATCHING_UP_CHIP_DELAY_MS = 10_000

/** The header chip's kind: ``missing`` (warning), ``preparing`` (first
 *  join), ``latest`` only while a stream has been arriving for
 *  :data:`CATCHING_UP_CHIP_DELAY_MS` (*slow*) — else none. An away host
 *  and a single failed round show no chip at all. */
export type ChipKind = 'missing' | 'preparing' | 'latest'

export function syncChipKind(sync: SpaceSync | null | undefined, slow: boolean): ChipKind | null {
  if (!sync) return null
  if (sync.may_be_missing) return 'missing'
  if (sync.state === 'never') return 'preparing'
  if (sync.state === 'catching_up' && slow) return 'latest'
  return null
}

export function syncChipLabel(kind: ChipKind): string {
  switch (kind) {
    case 'missing': return t('space.sync.missing')
    case 'preparing': return t('space.sync.chip.preparing')
    case 'latest': return t('space.sync.chip.latest')
  }
}

/** ``true`` once *on* has held for *delayMs* without a break — and
 *  ``false`` the moment it ends (no flicker for short stretches). */
export function useHeldFor(on: boolean, delayMs: number): boolean {
  const [held, setHeld] = useState(false)
  useEffect(() => {
    if (!on) { setHeld(false); return }
    const timer = setTimeout(() => setHeld(true), delayMs)
    return () => clearTimeout(timer)
  }, [on, delayMs])
  return on && held
}

/** The host household's name — the space's owner as a paired household
 *  knows it — else "the host". */
export function syncHostName(ownerInstanceId: string | null | undefined): string | null {
  if (!ownerInstanceId) return null
  return connections.value.find(c => c.instance_id === ownerInstanceId)?.display_name || null
}

const host = (name: string | null) => name || t('space.sync.host_fallback')

/** The settings' heading. */
export function syncHeading(view: SyncView, hostName: string | null): string {
  switch (view) {
    case 'here': return t('space.sync.heading.here')
    case 'latest': return t('space.sync.heading.latest')
    case 'missing': return t('space.sync.missing')
    case 'away': return hostName
      ? t('space.sync.heading.away', { host: hostName })
      : t('space.sync.heading.away_unknown')
    case 'preparing': return t('space.sync.heading.preparing')
  }
}

/** The settings' sentence under the heading. A ``{time}`` left in
 *  ``text`` is the caller's to fill from ``time`` — as a ``<time>`` with
 *  the full date — in the ``relative`` ("5 min ago") or ``since`` ("8:03",
 *  "Mon 18:40") shape (``utils/relativeTime``). */
export interface SyncBody {
  text: string
  time: { iso: string; style: 'relative' | 'since' } | null
}

export function syncBody(
  view: SyncView, sync: SpaceSync, hostName: string | null, hostedHere: boolean,
): SyncBody {
  if (hostedHere) return { text: t('space.sync.hosted_here'), time: null }
  const at = sync.last_ok_at
  const name = host(hostName)
  switch (view) {
    case 'here': return at
      ? { text: t('space.sync.body.here', { host: name, time: '{time}' }), time: { iso: at, style: 'relative' } }
      : { text: t('space.sync.body.here_no_time', { host: name }), time: null }
    case 'latest': return { text: t('space.sync.body.latest', { host: name }), time: null }
    case 'missing': return { text: t('space.sync.body.missing'), time: null }
    case 'away': return at
      ? { text: t('space.sync.body.away', { time: '{time}' }), time: { iso: at, style: 'since' } }
      : { text: t('space.sync.body.away_no_time'), time: null }
    case 'preparing': return { text: t('space.sync.body.preparing', { host: name }), time: null }
  }
}

/** The admin Details' lines (page, parts, signer, outbox, last sync). */
export function syncDetailLines(sync: SpaceSync): string[] {
  const lines: string[] = []
  const page = sync.page
  if (page) {
    const seq = String(page.seq)
    if (page.match === true) {
      lines.push(t('space.sync.details.page_match', { n: seq, time: relativeDocsTime(page.at) }))
    } else if (page.differing_buckets != null && page.differing_buckets > 0) {
      lines.push(isOne(page.differing_buckets)
        ? t('space.sync.details.page_differs_one', { page: seq })
        : t('space.sync.details.page_differs', { n: String(page.differing_buckets), page: seq }))
    } else {
      lines.push(t('space.sync.details.page_unchecked', { n: seq }))
    }
    if (page.match === false && page.missing) {
      lines.push(isOne(page.missing)
        ? t('space.sync.details.page_missing_one')
        : t('space.sync.details.page_missing', { n: String(page.missing) }))
    }
    if (page.signer_role) {
      lines.push(page.signer_role === 'owner'
        ? t('space.sync.details.signed_owner')
        : t('space.sync.details.signed_admin'))
    }
    if (page.conflict) lines.push(t('space.sync.details.conflict'))
  }
  if (sync.waiting_to_send > 0) {
    lines.push(isOne(sync.waiting_to_send)
      ? t('space.sync.waiting_one')
      : t('space.sync.waiting', { n: String(sync.waiting_to_send) }))
  }
  if (sync.last_ok_at) {
    lines.push(t('space.sync.details.last_ok', { time: relativeDocsTime(sync.last_ok_at) }))
  }
  return lines
}

/** Narrow an untrusted frame / response body to a :type:`SpaceSync`. */
export function parseSpaceSync(raw: unknown): SpaceSync | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const states: SpaceSyncState[] = ['up_to_date', 'catching_up', 'behind', 'host_offline', 'never']
  if (!states.includes(r.state as SpaceSyncState)) return null
  const sync: SpaceSync = {
    state: r.state as SpaceSyncState,
    last_ok_at: typeof r.last_ok_at === 'string' ? r.last_ok_at : null,
    waiting_to_send: typeof r.waiting_to_send === 'number' && r.waiting_to_send > 0
      ? Math.floor(r.waiting_to_send) : 0,
    may_be_missing: r.may_be_missing === true,
    unsent_posts: Array.isArray(r.unsent_posts)
      ? r.unsent_posts.filter((p): p is string => typeof p === 'string')
      : [],
  }
  const page = parsePage(r.page)
  if (page) sync.page = page
  return sync
}

const count = (v: unknown): number | null =>
  typeof v === 'number' && v >= 0 ? Math.floor(v) : null

function parsePage(raw: unknown): SpaceSyncPageWire | null {
  if (!raw || typeof raw !== 'object') return null
  const p = raw as Record<string, unknown>
  if (typeof p.seq !== 'number' || typeof p.at !== 'string') return null
  return {
    seq: p.seq,
    at: p.at,
    match: typeof p.match === 'boolean' ? p.match : null,
    missing: count(p.missing),
    differing_buckets: count(p.differing_buckets),
    signer_role: typeof p.signer_role === 'string' ? p.signer_role : null,
    conflict: p.conflict === true,
  }
}

/** The space's sync status: *initial* (from the space detail) until a
 *  ``space.sync_state`` frame for it says otherwise. */
export function useSpaceSync(spaceId: string, initial: unknown): SpaceSync | null {
  const [sync, setSync] = useState<SpaceSync | null>(() => parseSpaceSync(initial))
  useEffect(() => { setSync(parseSpaceSync(initial)) }, [spaceId, initial])
  useEffect(() => ws.on('space.sync_state', (e) => {
    const d = e.data as { space_id?: string; sync?: unknown }
    if (d.space_id !== spaceId) return
    const next = parseSpaceSync(d.sync)
    if (next) setSync(next)
  }), [spaceId])
  return sync
}

/** What to announce: the warning's words when the status turns INTO
 *  "Some posts may be missing" (never on first render, never on leaving
 *  it), else ``''`` — for a ``role="status"`` region. */
export function useMissingAnnouncement(missing: boolean): string {
  const prev = useRef<boolean | null>(null)
  const [said, setSaid] = useState('')
  useEffect(() => {
    if (prev.current === false && missing) setSaid(t('space.sync.missing'))
    else if (!missing) setSaid('')
    prev.current = missing
  }, [missing])
  return said
}
