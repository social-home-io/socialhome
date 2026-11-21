/**
 * A space's sync status — does this household have everything from the
 * space? (``GET /api/spaces/{id}`` ``sync``, kept live by the
 * ``space.sync_state`` frame; members only, a follower gets ``null``.)
 *
 * The server computes the state from the last answer of the household
 * hosting the space (``SpaceSyncStatusService``); this module only words
 * it. Relative times go through ``utils/relativeTime`` in the UI language.
 */
import { useEffect, useState } from 'preact/hooks'
import { isOne, t } from '@/i18n/i18n'
import { localSinceTime, relativeDocsTime } from '@/utils/relativeTime'
import type { SpaceSyncPageWire, SpaceSyncWire } from '@/types'
import { ws } from '@/ws'

/** ``state``; ``last_ok_at`` — UTC ISO 8601 of the host's last "you're up
 *  to date", or ``null``; ``waiting_to_send`` — our own items of the space
 *  still waiting to go out. */
export type SpaceSync = SpaceSyncWire
export type SpaceSyncState = SpaceSyncWire['state']

function waitingText(n: number): string {
  return isOne(n) ? t('space.sync.waiting_one') : t('space.sync.waiting', { n: String(n) })
}

/** Our copy was compared with the host's newest signed page and
 *  differs (v_95) — the server reads it ``behind``. */
function needsSync(sync: SpaceSync): boolean {
  return sync.state === 'behind' && sync.page?.match === false
}

function needsSyncText(sync: SpaceSync): string {
  const n = sync.page?.differing_buckets ?? 0
  return isOne(n)
    ? t('space.sync.chip.needs_sync_one')
    : t('space.sync.chip.needs_sync', { n: String(n) })
}

/** The state's own words, without the waiting count. */
function stateText(sync: SpaceSync): string | null {
  switch (sync.state) {
    case 'catching_up':
      return t('space.sync.state.catching_up')
    case 'behind':
      if (needsSync(sync)) return needsSyncText(sync)
      return sync.last_ok_at
        ? t('space.sync.chip.behind', { time: relativeDocsTime(sync.last_ok_at) })
        : t('space.sync.state.behind')
    case 'host_offline':
      return sync.last_ok_at
        ? t('space.sync.chip.host_offline', { time: localSinceTime(sync.last_ok_at) })
        : t('space.sync.state.host_offline')
    case 'never':
      return t('space.sync.state.never')
    default:
      return null
  }
}

/** The header chip's text, or ``null`` when there is nothing to say: up
 *  to date with nothing waiting to send. */
export function syncChipLabel(sync: SpaceSync | null | undefined): string | null {
  if (!sync) return null
  const parts: string[] = []
  const state = stateText(sync)
  if (state) parts.push(state)
  if (sync.waiting_to_send > 0) parts.push(waitingText(sync.waiting_to_send))
  return parts.length ? parts.join(' · ') : null
}

/** The settings' heading for the state (always one). */
export function syncStateLabel(state: SpaceSyncState): string {
  switch (state) {
    case 'up_to_date': return t('space.sync.state.up_to_date')
    case 'catching_up': return t('space.sync.state.catching_up')
    case 'behind': return t('space.sync.state.behind')
    case 'host_offline': return t('space.sync.state.host_offline')
    case 'never': return t('space.sync.state.never')
  }
}

/** The settings' heading: the state, or — compared with the newest
 *  signed page — "Up to date · checked against page N" / "Needs sync". */
export function syncHeading(sync: SpaceSync): string {
  if (needsSync(sync)) return t('space.sync.state.needs_sync')
  if (sync.state === 'up_to_date' && sync.page?.match === true) {
    return t('space.sync.page_checked', { n: String(sync.page.seq) })
  }
  return syncStateLabel(sync.state)
}

/** :func:`syncStateBody`, worded for a compare that found a difference. */
export function syncBody(sync: SpaceSync, hostedHere: boolean): string {
  if (!hostedHere && needsSync(sync)) return t('space.sync.body.needs_sync')
  return syncStateBody(sync.state, hostedHere)
}

/** One sentence on what the state means for the reader. */
export function syncStateBody(state: SpaceSyncState, hostedHere: boolean): string {
  if (hostedHere) return t('space.sync.hosted_here')
  switch (state) {
    case 'up_to_date': return t('space.sync.body.up_to_date')
    case 'catching_up': return t('space.sync.body.catching_up')
    case 'behind': return t('space.sync.body.behind')
    case 'host_offline': return t('space.sync.body.host_offline')
    case 'never': return t('space.sync.body.never')
  }
}

/** "Last up to date 5 min ago", or ``null`` when never. */
export function syncLastOkLine(sync: SpaceSync): string | null {
  return sync.last_ok_at
    ? t('space.sync.last_ok', { time: relativeDocsTime(sync.last_ok_at) })
    : null
}

export function syncWaitingLine(sync: SpaceSync): string | null {
  return sync.waiting_to_send > 0 ? waitingText(sync.waiting_to_send) : null
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
  }
  const page = parsePage(r.page)
  if (page) sync.page = page
  return sync
}

function parsePage(raw: unknown): SpaceSyncPageWire | null {
  if (!raw || typeof raw !== 'object') return null
  const p = raw as Record<string, unknown>
  if (typeof p.seq !== 'number' || typeof p.at !== 'string') return null
  return {
    seq: p.seq,
    at: p.at,
    match: typeof p.match === 'boolean' ? p.match : null,
    differing_buckets: typeof p.differing_buckets === 'number' && p.differing_buckets >= 0
      ? Math.floor(p.differing_buckets) : null,
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
